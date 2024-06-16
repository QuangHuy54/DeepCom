import os

import config
import train
import eval


def _train(testing_project,is_transfer,vocab_file_path=None, model_file_path=None,model_state_dict=None,num_of_data=-1):
    print('\nStarting the training process......\n')

    if vocab_file_path:
        code_vocab_path, ast_vocab_path, nl_vocab_path = vocab_file_path
        print('Vocabulary will be built by given file path.')
        print('\tsource code vocabulary path:\t', os.path.join(config.vocab_dir, code_vocab_path))
        print('\tast of code vocabulary path:\t', os.path.join(config.vocab_dir, ast_vocab_path))
        print('\tcode comment vocabulary path:\t', os.path.join(config.vocab_dir, nl_vocab_path))
    else:
        print('Vocabulary will be built according to dataset.')

    if model_file_path:
        print('Model will be built by given state dict file path:', os.path.join(config.model_dir, model_file_path))
    else:
        print('Model will be created by program.')

    print('\nInitializing the training environments......\n')
    if not is_transfer:
        train_instance = train.Train(vocab_file_path=vocab_file_path, model_file_path=model_file_path,code_path=f'../dataset_v2/original/{testing_project}/train_transfer.code'
                                    ,ast_path=f'../dataset_v2/original/{testing_project}/train_transfer.sbt',nl_path=f'../dataset_v2/original/{testing_project}/train_transfer.comment'
                                    ,code_valid_path=f'../dataset_v2/original/{testing_project}/valid_transfer.code',nl_valid_path=f'../dataset_v2/original/{testing_project}/valid_transfer.comment',
                                    ast_valid_path=f'../dataset_v2/original/{testing_project}/valid_transfer.sbt'
                                    ,num_of_data=num_of_data)
    else:
        train_instance = train.Train(vocab_file_path=vocab_file_path,code_path=f'../dataset_v2/original/{testing_project}/train.code'
                                    ,ast_path=f'../dataset_v2/original/{testing_project}/train.sbt',nl_path=f'../dataset_v2/original/{testing_project}/train.comment'
                                    ,code_valid_path=f'../dataset_v2/original/{testing_project}/valid_transfer.code',nl_valid_path=f'../dataset_v2/original/{testing_project}/valid_transfer.comment',
                                    ast_valid_path=f'../dataset_v2/original/{testing_project}/valid_transfer.sbt'
                                    ,model_state_dict=model_state_dict
                                    ,num_of_data=num_of_data,model_file_path=model_file_path,save_file=False)        
    print('Environments built successfully.\n')
    print('Size of train dataset:', train_instance.train_dataset_size)

    if config.validate_during_train:
        print('\nValidate every', config.validate_every, 'batches and each epoch.')
        print('Size of validation dataset:', train_instance.eval_instance.dataset_size)
        config.logger.info('Size of validation dataset: {}'.format(train_instance.eval_instance.dataset_size))

    print('\nStart training......\n')
    config.logger.info('Start training.')
    best_model = train_instance.run_train()
    print('\nTraining is done.')
    config.logger.info('Training is done.')

    # writer = SummaryWriter('runs/CodePtr')
    # for _, batch in enumerate(train_instance.train_dataloader):
    #     batch_size = len(batch[0][0])
    #     writer.add_graph(train_instance.model, (batch, batch_size, train_instance.nl_vocab))
    #     break
    # writer.close()

    return best_model


def _test(model,testing_project):
    print('\nInitializing the test environments......')
    test_instance = eval.Test(model,code_path=f'../dataset_v2/original/{testing_project}/valid.code',ast_path=f'../dataset_v2/original/{testing_project}/valid.sbt',nl_path=f'../dataset_v2/original/{testing_project}/valid.comment')
    print('Environments built successfully.\n')
    print('Size of test dataset:', test_instance.dataset_size)
    config.logger.info('Size of test dataset: {}'.format(test_instance.dataset_size))

    config.logger.info('Start Testing.')
    print('\nStart Testing......')
    test_instance.run_test()
    print('Testing is done.')


if __name__ == '__main__':
    testing_project='flink'
    path = "model/20240614_115800"
    dir_list = os.listdir(path)
    for file in dir_list:
        print(f'File name: ',file)
        best_model_dict2=_train(testing_project,is_transfer=True,vocab_file_path=(config.code_vocab_path, config.ast_vocab_path, config.nl_vocab_path),model_file_path=os.path.join(path,file),num_of_data=100)

        _test(best_model_dict2,testing_project)
    # _test(os.path.join('20240514_083750', 'best_epoch-1_batch-last.pt'))