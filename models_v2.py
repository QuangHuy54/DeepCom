import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import math
import random
from transformers import PegasusTokenizer, PegasusForConditionalGeneration

import config
import utils


def init_rnn_wt(rnn):
    for names in rnn._all_weights:
        for name in names:
            if name.startswith('weight_'):
                wt = getattr(rnn, name)
                wt.data.uniform_(-config.init_uniform_mag, config.init_uniform_mag)
            elif name.startswith('bias_'):
                # set forget bias to 1
                bias = getattr(rnn, name)
                n = bias.size(0)
                start, end = n // 4, n // 2
                bias.data.fill_(0.)
                bias.data[start:end].fill_(1.)


def init_linear_wt(linear):
    """
    initialize the weight and bias(if) of the given linear layer
    :param linear: linear layer
    :return:
    """
    linear.weight.data.normal_(std=config.init_normal_std)
    if linear.bias is not None:
        linear.bias.data.normal_(std=config.init_normal_std)


def init_wt_normal(wt):
    """
    initialize the given weight following the normal distribution
    :param wt: weight to be normal initialized
    :return:
    """
    wt.data.normal_(std=config.init_normal_std)


def init_wt_uniform(wt):
    """
    initialize the given weight following the uniform distribution
    :param wt: weight to be uniform initialized
    :return:
    """
    wt.data.uniform_(-config.init_uniform_mag, config.init_uniform_mag)


class Encoder(nn.Module):
    """
    Encoder for both code and ast
    """

    def __init__(self, vocab_size):
        super(Encoder, self).__init__()
        self.hidden_size = config.hidden_size
        self.num_directions = 2

        # vocab_size: config.code_vocab_size for code encoder, size of sbt vocabulary for ast encoder
        self.embedding = nn.Embedding(vocab_size, config.embedding_dim)
        self.gru = nn.GRU(config.embedding_dim, self.hidden_size,bidirectional=True)

        init_wt_normal(self.embedding.weight)
        init_rnn_wt(self.gru)

    def forward(self, inputs: torch.Tensor, seq_lens: torch.Tensor) -> (torch.Tensor, torch.Tensor):
        """

        :param inputs: sorted by length in descending order, [T, B]
        :param seq_lens: should be in descending order
        :return: outputs: [T, B, H]
                hidden: [2, B, H]
        """
        embedded = self.embedding(inputs)   # [T, B, embedding_dim]
        packed = pack_padded_sequence(embedded, seq_lens, enforce_sorted=False)
        outputs, hidden = self.gru(packed)
        outputs, _ = pad_packed_sequence(outputs)  # [T, B, 2*H]
        outputs = outputs[:, :, :self.hidden_size] + outputs[:, :, self.hidden_size:]

        # outputs: [T, B, H]
        # hidden: [2, B, H]
        return outputs, hidden

    def init_hidden(self, batch_size):
        return torch.zeros(self.num_directions, batch_size, self.hidden_size, device=config.device)


class ReduceHidden(nn.Module):

    def __init__(self, hidden_size=config.hidden_size):
        super(ReduceHidden, self).__init__()
        self.hidden_size = hidden_size
        self.linear = nn.Linear(2 * self.hidden_size, self.hidden_size)

        init_linear_wt(self.linear)

    def forward(self, code_hidden, ast_hidden):
        """

        :param code_hidden: hidden state of code encoder, [1, B, H]
        :param ast_hidden: hidden state of ast encoder, [1, B, H]
        :return: [1, B, H]
        """
        hidden = torch.cat((code_hidden, ast_hidden), dim=2)
        hidden = self.linear(hidden)
        hidden = F.relu(hidden)
        return hidden


class Attention(nn.Module):

    def __init__(self, hidden_size=config.hidden_size):
        super(Attention, self).__init__()
        self.hidden_size = hidden_size

        self.attn = nn.Linear(2 * self.hidden_size, self.hidden_size)
        self.v = nn.Parameter(torch.rand(self.hidden_size), requires_grad=True)   # [H]
        stdv = 1. / math.sqrt(self.v.size(0))
        self.v.data.normal_(mean=0, std=stdv)

    def forward(self, hidden, encoder_outputs):
        """
        forward the net
        :param hidden: the last hidden state of encoder, [1, B, H]
        :param encoder_outputs: [T, B, H]
        :return: softmax scores, [B, 1, T]
        """
        time_step, batch_size, _ = encoder_outputs.size()
        h = hidden.repeat(time_step, 1, 1).transpose(0, 1)  # [B, T, H]
        encoder_outputs = encoder_outputs.transpose(0, 1)   # [B, T, H]
        attn_energies = self.score(h, encoder_outputs)      # [B, T]
        return F.softmax(attn_energies, dim=1).unsqueeze(1)

    def score(self, hidden, encoder_outputs):
        """
        calculate the attention scores of each word
        :param hidden: [B, T, H]
        :param encoder_outputs: [B, T, H]
        :return: energy: scores of each word in a batch, [B, T]
        """
        # after cat: [B, T, 2*H]
        # after attn: [B, T, H]
        # energy: [B, T, H]
        energy = F.relu(self.attn(torch.cat([hidden, encoder_outputs], dim=2)))
        energy = energy.transpose(1, 2)     # [B, H, T]
        v = self.v.repeat(encoder_outputs.size(0), 1).unsqueeze(1)      # [B, 1, H]
        energy = torch.bmm(v, energy)   # [B, 1, T]
        return energy.squeeze(1)


class Decoder(nn.Module):

    def __init__(self, vocab_size, hidden_size=config.hidden_size):
        super(Decoder, self).__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(vocab_size, config.embedding_dim)
        self.dropout = nn.Dropout(config.decoder_dropout_rate)
        self.code_attention = Attention()
        self.ast_attention = Attention()
        self.gru = nn.GRU(config.embedding_dim + self.hidden_size, self.hidden_size)
        self.out = nn.Linear(2 * self.hidden_size, config.nl_vocab_size)

        init_wt_normal(self.embedding.weight)
        init_rnn_wt(self.gru)
        init_linear_wt(self.out)

    def forward(self, inputs: torch.Tensor, last_hidden: torch.Tensor,
                code_outputs: torch.Tensor, ast_outputs: torch.Tensor) \
            -> (torch.Tensor, torch.Tensor, torch.Tensor):
        """
        forward the net
        :param inputs: word input of current time step, [B]
        :param last_hidden: last decoder hidden state, [1, B, H]
        :param code_outputs: outputs of code encoder, [T, B, H]
        :param ast_outputs: outputs of ast encoder, [T, B, H]
        :return: output: [B, nl_vocab_size]
                hidden: [1, B, H]
                attn_weights: [B, 1, T]
        """
        embedded = self.embedding(inputs).unsqueeze(0)      # [1, B, embedding_dim]
        embedded = self.dropout(embedded)

        code_attn_weights = self.code_attention(last_hidden, code_outputs)  # [B, 1, T]
        code_context = code_attn_weights.bmm(code_outputs.transpose(0, 1))  # [B, 1, H]
        code_context = code_context.transpose(0, 1)     # [1, B, H]

        ast_attn_weights = self.ast_attention(last_hidden, ast_outputs)  # [B, 1, T]
        ast_context = ast_attn_weights.bmm(ast_outputs.transpose(0, 1))     # [B, 1, H]
        ast_context = ast_context.transpose(0, 1)   # [1, B, H]

        context = code_context + ast_context    # [1, B, H]

        rnn_input = torch.cat([embedded, context], dim=2)   # [1, B, embedding_dim + H]
        outputs, hidden = self.gru(rnn_input, last_hidden)  # [1, B, H] for both
        outputs = outputs.squeeze(0)    # [B, H]
        context = context.squeeze(0)    # [B, H]
        outputs = self.out(torch.cat([outputs, context], 1))    # [B, nl_vocab_size]
        outputs = F.log_softmax(outputs, dim=1)     # [B, nl_vocab_size]
        return outputs, hidden, code_attn_weights, ast_attn_weights


class Model(nn.Module):
    def __init__(self, code_vocab_size, ast_vocab_size, nl_vocab_size,
                 model_file_path=None, model_state_dict=None, is_eval=False):
        super(Model, self).__init__()

        self.code_vocab_size = code_vocab_size
        self.ast_vocab_size = ast_vocab_size
        self.is_eval = is_eval

        self.code_encoder = Encoder(self.code_vocab_size)
        self.ast_encoder = Encoder(self.ast_vocab_size)
        self.reduce_hidden = ReduceHidden()
        
        # Load Pegasus
        self.tokenizer = PegasusTokenizer.from_pretrained("google/pegasus-large")
        self.pegasus_model = PegasusForConditionalGeneration.from_pretrained("google/pegasus-large")

        if config.use_cuda:
            self.code_encoder = self.code_encoder.cuda()
            self.ast_encoder = self.ast_encoder.cuda()
            self.reduce_hidden = self.reduce_hidden.cuda()
            self.pegasus_model = self.pegasus_model.cuda()

        if model_file_path:
            state = torch.load(model_file_path, map_location=torch.device('cuda' if config.use_cuda else 'cpu'))
            self.set_state_dict(state)

        if model_state_dict:
            self.set_state_dict(model_state_dict)

        if is_eval:
            self.code_encoder.eval()
            self.ast_encoder.eval()
            self.reduce_hidden.eval()
            self.pegasus_model.eval()

    def forward(self, batch, batch_size, nl_vocab, is_test=False):
        code_batch, code_seq_lens, ast_batch, ast_seq_lens, nl_batch, nl_seq_lens = batch

        code_outputs, code_hidden = self.code_encoder(code_batch, code_seq_lens)
        ast_outputs, ast_hidden = self.ast_encoder(ast_batch, ast_seq_lens)

        code_hidden = code_hidden[0] + code_hidden[1]
        code_hidden = code_hidden.unsqueeze(0)
        ast_hidden = ast_hidden[0] + ast_hidden[1]
        ast_hidden = ast_hidden.unsqueeze(0)
        decoder_hidden = self.reduce_hidden(code_hidden, ast_hidden)

        if is_test:
            return code_outputs, ast_outputs, decoder_hidden

        # Tạo đầu vào cho Pegasus từ đầu ra của encoder
        combined_outputs = torch.cat((code_outputs, ast_outputs), dim=0)  # [2*T, B, H]
        combined_outputs = combined_outputs.permute(1, 0, 2)  # [B, 2*T, H]

        # Chuyển combined_outputs thành dạng tensor Pegasus có thể sử dụng
        attention_mask = torch.ones(combined_outputs.shape[:2], dtype=torch.long, device=config.device)
        
        # Pegasus generate
        summaries = self.pegasus_model.generate(
            inputs_embeds=combined_outputs,
            attention_mask=attention_mask,
            max_length=config.max_decode_steps,
            num_beams=config.num_beams
        )

        # Decode summaries thành văn bản
        decoder_outputs = [self.tokenizer.decode(summary, skip_special_tokens=True) for summary in summaries]

        # Chuyển đổi các văn bản tóm tắt trở lại thành tensor
        decoder_outputs = torch.tensor([self.tokenizer.encode(output, padding='max_length', max_length=config.max_decode_steps, truncation=True) for output in decoder_outputs], device=config.device)
        
        return decoder_outputs

    def set_state_dict(self, state_dict):
        self.code_encoder.load_state_dict(state_dict["code_encoder"])
        self.ast_encoder.load_state_dict(state_dict["ast_encoder"])
        self.reduce_hidden.load_state_dict(state_dict["reduce_hidden"])
        self.pegasus_model.load_state_dict(state_dict["pegasus_model"])