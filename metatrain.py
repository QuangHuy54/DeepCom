import torch
import torch.nn as nn
from torch.optim import Adam, lr_scheduler
from torch.utils.data import DataLoader
import os
import time
import threading
import matplotlib.pyplot as plt
import numpy as np
import utils
import config
import data
import models
import eval
from tqdm import tqdm
import learn2learn as l2l


class MetaTrain(object):

    def __init__(self, training_projects, validating_project, vocab_file_path=None, model_file_path=None):
        """

        :param vocab_file_path: tuple of code vocab, ast vocab, nl vocab, if given, build vocab by given path
        :param model_file_path:
        """

        self.training_projects = training_projects
        self.validating_project = validating_project
        self.config = config
        source_projects = training_projects + [validating_project]

        # dataset
        dataset_dir = "../dataset/"
        self.meta_datasets = {}
        for project in source_projects:
            self.meta_datasets[project]={
                "support": data.CodePtrDataset(code_path=os.path.join(dataset_dir,f'split/{project}/train.code'),
                                                ast_path=os.path.join(dataset_dir,f'split/{project}/train.sbt'),
                                                nl_path=os.path.join(dataset_dir,f'split/{project}/train.comment')),
                "query": data.CodePtrDataset(code_path=os.path.join(dataset_dir,f'split/{project}/valid.code'),
                                                ast_path=os.path.join(dataset_dir,f'split/{project}/valid.sbt'),
                                                nl_path=os.path.join(dataset_dir,f'split/{project}/valid.comment'))
            }
        
        self.meta_datasets_size = sum([(len(dataset['support']) + len(dataset['query'])) for dataset in self.meta_datasets.values()])

        self.meta_dataloaders = {}
        for project in source_projects:
            self.meta_dataloaders[project] = {
                'support': DataLoader(dataset=self.meta_datasets[project]['support'], batch_size=config.batch_size, shuffle=True,
                                           collate_fn=lambda *args: utils.unsort_collate_fn(args,
                                                                                            code_vocab=self.code_vocab,
                                                                                            ast_vocab=self.ast_vocab,
                                                                                            nl_vocab=self.nl_vocab)),
                'query': DataLoader(dataset=self.meta_datasets[project]['query'], batch_size=config.test_batch_size, shuffle=True, # đã sửa từ batch_size thành test_batch_size
                                           collate_fn=lambda *args: utils.unsort_collate_fn(args,
                                                                                            code_vocab=self.code_vocab,
                                                                                            ast_vocab=self.ast_vocab,
                                                                                            nl_vocab=self.nl_vocab))
            }

        # vocab
        self.code_vocab: utils.Vocab
        self.ast_vocab: utils.Vocab
        self.nl_vocab: utils.Vocab
        # load vocab from given path
        if vocab_file_path:
            code_vocab_path, ast_vocab_path, nl_vocab_path = vocab_file_path
            self.code_vocab = utils.load_vocab_pk(code_vocab_path)
            self.ast_vocab = utils.load_vocab_pk(ast_vocab_path)
            self.nl_vocab = utils.load_vocab_pk(nl_vocab_path)
        # new vocab
        # else:
        #     self.code_vocab = utils.Vocab('code_vocab')
        #     self.ast_vocab = utils.Vocab('ast_vocab')
        #     self.nl_vocab = utils.Vocab('nl_vocab')
        #     codes, asts, nls = self.meta_datasets.get_dataset()
        #     for code, ast, nl in zip(codes, asts, nls):
        #         self.code_vocab.add_sentence(code)
        #         self.ast_vocab.add_sentence(ast)
        #         self.nl_vocab.add_sentence(nl)

        #     self.origin_code_vocab_size = len(self.code_vocab)
        #     self.origin_nl_vocab_size = len(self.nl_vocab)

        #     # trim vocabulary
        #     self.code_vocab.trim(config.code_vocab_size)
        #     self.nl_vocab.trim(config.nl_vocab_size)
        #     # save vocabulary
        #     self.code_vocab.save(config.code_vocab_path)
        #     self.ast_vocab.save(config.ast_vocab_path)
        #     self.nl_vocab.save(config.nl_vocab_path)
        #     self.code_vocab.save_txt(config.code_vocab_txt_path)
        #     self.ast_vocab.save_txt(config.ast_vocab_txt_path)
        #     self.nl_vocab.save_txt(config.nl_vocab_txt_path)

        self.code_vocab_size = len(self.code_vocab)
        self.ast_vocab_size = len(self.ast_vocab)
        self.nl_vocab_size = len(self.nl_vocab)

        # model
        self.model = models.Model(code_vocab_size=self.code_vocab_size,
                                  ast_vocab_size=self.ast_vocab_size,
                                  nl_vocab_size=self.nl_vocab_size,
                                  model_file_path=model_file_path)
        self.params = list(self.model.code_encoder.parameters()) + \
            list(self.model.ast_encoder.parameters()) + \
            list(self.model.reduce_hidden.parameters()) + \
            list(self.model.decoder.parameters())

        # optimizer
        # self.optimizer = Adam([
        #     {'params': self.model.code_encoder.parameters(), 'lr': config.code_encoder_lr},
        #     {'params': self.model.ast_encoder.parameters(), 'lr': config.ast_encoder_lr},
        #     {'params': self.model.reduce_hidden.parameters(), 'lr': config.reduce_hidden_lr},
        #     {'params': self.model.decoder.parameters(), 'lr': config.decoder_lr},
            
        # ], betas=(0.9, 0.999), eps=1e-08, weight_decay=0, amsgrad=False)


        # if config.use_lr_decay:
        #     self.lr_scheduler = lr_scheduler.StepLR(self.optimizer,
        #                                             step_size=config.lr_decay_every,
        #                                             gamma=config.lr_decay_rate)

        # best score and model(state dict)
        self.min_loss: float = 1000
        self.best_model: dict = {}
        self.best_epoch_batch: (int, int) = (None, None)

        # eval instance
        # self.eval_instance = eval.Eval(self.get_cur_state_dict())

        # early stopping
        self.early_stopping = None
        if config.use_early_stopping:
            self.early_stopping = utils.EarlyStopping()

        config.model_dir = os.path.join(config.model_dir, utils.get_timestamp())
        if not os.path.exists(config.model_dir):
            os.makedirs(config.model_dir)

    def run_train(self):
        """
        start training
        """
        self.train_iter()
        return self.best_model

    def run_one_batch(self, model, batch, batch_size, criterion):
        """
        train one batch
        :param batch: get from collate_fn of corresponding dataloader
        :param batch_size: batch size
        :param criterion: loss function
        :return: avg loss
        """
        nl_batch = batch[4]

        decoder_outputs = model(batch, batch_size, self.nl_vocab)     # [T, B, nl_vocab_size]

        decoder_outputs = decoder_outputs.view(-1, config.nl_vocab_size)
        nl_batch = nl_batch.view(-1)

        loss = criterion(decoder_outputs, nl_batch)
        return loss

    def eval_one_batch(self, model, batch, batch_size, criterion):
        """
        train one batch
        :param batch: get from collate_fn of corresponding dataloader
        :param batch_size: batch size
        :param criterion: loss function
        :return: avg loss
        """
        with torch.no_grad():
            nl_batch = batch[4]

            decoder_outputs = model(batch, batch_size, self.nl_vocab)     # [T, B, nl_vocab_size]

            decoder_outputs = decoder_outputs.view(-1, config.nl_vocab_size)
            nl_batch = nl_batch.view(-1)

            loss = criterion(decoder_outputs, nl_batch)

            return loss

    def train_iter(self,train_steps=12000, inner_train_steps=4, 
              valid_steps=200, inner_valid_steps=4, 
              valid_every=2, eval_start=0, early_stop=50):
        best_losses= 0

        self.criterion = nn.NLLLoss(ignore_index=utils.get_pad_index(self.nl_vocab))

        self.maml = l2l.algorithms.MAML(self.model, lr=0.1, allow_nograd=True)
        self.optimizer = torch.optim.Adam(self.maml.parameters(), lr=config.learning_rate)

        print("DEBUG[PHONG]: entered train_iter, initialized.")

        for epoch in range(train_steps//valid_every):
            pbar = tqdm(range(valid_every))
            losses = []
            for iteration in pbar:
                print("DEBUG[PHONG]: entered first iteration.")
                self.optimizer.zero_grad()
                for project in self.training_projects:
                    print("DEBUG[PHONG]: entered first project.")
                    sup_batch, qry_batch = next(iter(self.meta_dataloaders[project]['support'])), next(iter(self.meta_dataloaders[project]['query']))
                    batch_size_sup = len(sup_batch[0][0])
                    batch_size_qry=len(qry_batch[0][0])

                    print("DEBUG[PHONG]: before cloning model.")
                    task_model = self.maml.clone()
                    print("DEBUG[PHONG]: cloned model.")
                    adaptation_loss=self.run_one_batch(task_model,sup_batch,batch_size_sup,self.criterion)
                    print("DEBUG[PHONG]: end one batch.")

                    # print(adaptation_loss.shape)
                    # print(adaptation_loss)
                    # TODO: lỗi ở đây
                    task_model.adapt(adaptation_loss) 

                    query_loss=self.run_one_batch(task_model,qry_batch,batch_size_qry,self.criterion)
                    print("DEBUG[PHONG]: after run one batch on qry.")
                    query_loss.backward()
                    print("DEBUG[PHONG]: after backward qry-loss.")
                    losses.append(query_loss.item())
                    
                self.optimizer.step()
                print("DEBUG[PHONG]: stepped optimizer.")
                pbar.set_description('Epoch = %d [loss=%.4f, min=%.4f, max=%.4f] %d' % (epoch, np.mean(losses), np.min(losses), np.max(losses), 1))

            # validation
            if epoch >= eval_start:
                self.valid_state_dict(state_dict=self.get_cur_state_dict(), epoch=epoch, batch=1)
                if config.use_early_stopping:
                    if self.early_stopping.early_stop:
                        break

        # save the best model
        if config.save_best_model:
            best_model_name = 'best_epoch-{}.pt'.format(
                self.best_epoch_batch[0])
            self.save_model(name=best_model_name, state_dict=self.best_model)

    def save_model(self, name=None, state_dict=None):
        """
        save current model
        :param name: if given, name the model file by given name, else by current time
        :param state_dict: if given, save the given state dict, else save current model
        :return:
        """
        if state_dict is None:
            state_dict = self.get_cur_state_dict()
        if name is None:
            model_save_path = os.path.join(config.model_dir, 'meta_model_{}.pt'.format(utils.get_timestamp()))
        else:
            model_save_path = os.path.join(config.model_dir, name)
        torch.save(state_dict, model_save_path)

    def save_check_point(self):
        pass

    def get_cur_state_dict(self) -> dict:
        """
        get current state dict of model
        :return:
        """
        state_dict = {
                'code_encoder': self.model.code_encoder.state_dict(),
                'ast_encoder': self.model.ast_encoder.state_dict(),
                'reduce_hidden': self.model.reduce_hidden.state_dict(),
                'decoder': self.model.decoder.state_dict(),
                'optimizer': self.optimizer.state_dict(),
            }
        return state_dict

    def valid_state_dict(self, state_dict, epoch, batch=-1):
        sup_batch, qry_batch = next(iter(self.meta_dataloaders[self.validating_project]['support'])), next(iter(self.meta_dataloaders[self.validating_project]['query']))
        batch_size_sup = len(sup_batch[0][0])
        batch_size_qry=len(qry_batch[0][0])
        task_model = self.maml.clone()
        adaptation_loss=self.run_one_batch(task_model,sup_batch,batch_size_sup,self.criterion)
        task_model.adapt(adaptation_loss)
        loss=self.eval_one_batch(task_model,qry_batch,batch_size_qry,self.criterion)

        if config.save_valid_model:
            model_name = 'meta_model_valid-loss-{:.4f}_epoch-{}_batch-{}.pt'.format(loss, epoch, batch)
            save_thread = threading.Thread(target=self.save_model, args=(model_name, state_dict))
            save_thread.start()

        if loss < self.min_loss:
            self.min_loss = loss
            self.best_model = state_dict
            self.best_epoch_batch = (epoch, batch)

        if config.use_early_stopping:
            self.early_stopping(loss)

