# -*- coding: utf-8 -*-

import time
import sys
import argparse
import random
import copy
import torch
import gc
import cPickle as pickle
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from utils.metric import get_ner_fmeasure
from model.seqmodel import SeqModel
from utils.data import Data

seed_num = 42
random.seed(seed_num)
torch.manual_seed(seed_num)
np.random.seed(seed_num)


def predict_check(pred_variable, gold_variable, mask_variable):
    """
        input:
            pred_variable (batch_size, sent_len): pred tag result, in numpy format
            gold_variable (batch_size, sent_len): gold result variable
            mask_variable (batch_size, sent_len): mask variable
    """
    pred = pred_variable.cpu().data.numpy()
    gold = gold_variable.cpu().data.numpy()
    mask = mask_variable.cpu().data.numpy()
    overlaped = (pred == gold)
    right_token = np.sum(overlaped * mask)
    total_token = mask.sum()
    # print("right: %s, total: %s"%(right_token, total_token))
    return right_token, total_token


def recover_label(pred_variable, gold_variable, mask_variable, label_alphabet, word_recover):
    """
        input:
            pred_variable (batch_size, sent_len): pred tag result
            gold_variable (batch_size, sent_len): gold result variable
            mask_variable (batch_size, sent_len): mask variable
    """

    pred_variable = pred_variable[word_recover]
    gold_variable = gold_variable[word_recover]
    mask_variable = mask_variable[word_recover]
    batch_size = gold_variable.size(0)
    seq_len = gold_variable.size(1)
    mask = mask_variable.cpu().data.numpy()
    pred_tag = pred_variable.cpu().data.numpy()
    gold_tag = gold_variable.cpu().data.numpy()
    batch_size = mask.shape[0]
    pred_label = []
    gold_label = []
    for idx in range(batch_size):
        pred = [label_alphabet.get_instance(pred_tag[idx][idy]) for idy in range(seq_len) if mask[idx][idy] != 0]
        gold = [label_alphabet.get_instance(gold_tag[idx][idy]) for idy in range(seq_len) if mask[idx][idy] != 0]
        # print "p:",pred, pred_tag.tolist()
        # print "g:", gold, gold_tag.tolist()
        assert (len(pred) == len(gold))
        pred_label.append(pred)
        gold_label.append(gold)
    return pred_label, gold_label


def recover_nbest_label(pred_variable, mask_variable, label_alphabet, word_recover):
    """
        input:
            pred_variable (batch_size, sent_len, nbest): pred tag result
            mask_variable (batch_size, sent_len): mask variable
            word_recover (batch_size)
        output:
            nbest_pred_label list: [batch_size, nbest, each_seq_len]
    """
    # print "word recover:", word_recover.size()
    # exit(0)
    pred_variable = pred_variable[word_recover]
    mask_variable = mask_variable[word_recover]
    batch_size = pred_variable.size(0)
    seq_len = pred_variable.size(1)
    # print pred_variable.size()
    nbest = pred_variable.size(2)
    mask = mask_variable.cpu().data.numpy()
    pred_tag = pred_variable.cpu().data.numpy()
    batch_size = mask.shape[0]
    pred_label = []
    for idx in range(batch_size):
        pred = []
        for idz in range(nbest):
            each_pred = [label_alphabet.get_instance(pred_tag[idx][idy][idz]) for idy in range(seq_len) if
                         mask[idx][idy] != 0]
            pred.append(each_pred)
        pred_label.append(pred)
    return pred_label


def lr_decay(optimizer, epoch, decay_rate, init_lr):
    lr = init_lr / (1 + decay_rate * epoch)
    print " Learning rate is setted as:", lr
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return optimizer


def evaluate(data, model, name, nbest=None):
    if name == "train":
        instances = data.train_Ids
    elif name == "dev":
        instances = data.dev_Ids
    elif name == 'test':
        instances = data.test_Ids
    elif name == 'raw':
        instances = data.raw_Ids
    else:
        print "Error: wrong evaluate name,", name
    right_token = 0
    whole_token = 0
    nbest_pred_results = []
    pred_scores = []
    pred_results = []
    gold_results = []
    ## set model in eval model
    model.eval()
    batch_size = data.HP_batch_size
    start_time = time.time()
    train_num = len(instances)
    total_batch = train_num // batch_size + 1
    for batch_id in range(total_batch):
        start = batch_id * batch_size
        end = (batch_id + 1) * batch_size
        if end > train_num:
            end = train_num
        instance = instances[start:end]
        if not instance:
            continue
        batch_word, batch_features, batch_wordlen, batch_wordrecover, batch_char, batch_charlen, batch_charrecover, batch_label, batch_trans, trans_seq_lengths, trans_seq_recover, mask = batchify_with_label(
            instance, data.HP_gpu, True)
        if nbest:
            scores, nbest_tag_seq = model.decode_nbest(batch_word, batch_features, batch_wordlen, batch_char,
                                                       batch_charlen, batch_charrecover, mask, nbest, batch_trans,
                                                       trans_seq_lengths, trans_seq_recover)
            nbest_pred_result = recover_nbest_label(nbest_tag_seq, mask, data.label_alphabet, batch_wordrecover)
            nbest_pred_results += nbest_pred_result
            pred_scores += scores[batch_wordrecover].cpu().data.numpy().tolist()
            ## select the best sequence to evalurate
            tag_seq = nbest_tag_seq[:, :, 0]
        else:
            tag_seq = model(batch_word, batch_features, batch_wordlen, batch_char, batch_charlen, batch_charrecover,
                            mask, batch_trans, trans_seq_lengths, trans_seq_recover)
        # print "tag:",tag_seq
        pred_label, gold_label = recover_label(tag_seq, batch_label, mask, data.label_alphabet, batch_wordrecover)
        pred_results += pred_label
        gold_results += gold_label
    decode_time = time.time() - start_time
    speed = len(instances) / decode_time
    acc, p, r, f = get_ner_fmeasure(gold_results, pred_results, data.tagScheme)
    if nbest:
        return speed, acc, p, r, f, nbest_pred_results, pred_scores
    return speed, acc, p, r, f, pred_results, pred_scores


def batchify_with_label(input_batch_list, gpu, volatile_flag=False):
    """
        input: list of words, chars and labels, various length. [[words,chars, labels],[words,chars,labels],...]
            words: word ids for one sentence. (batch_size, sent_len) 
            chars: char ids for on sentences, various length. (batch_size, sent_len, each_word_length)
        output:
            zero padding for word and char, with their batch length
            word_seq_tensor: (batch_size, max_sent_len) Variable
            word_seq_lengths: (batch_size,1) Tensor
            char_seq_tensor: (batch_size*max_sent_len, max_word_len) Variable
            char_seq_lengths: (batch_size*max_sent_len,1) Tensor
            char_seq_recover: (batch_size*max_sent_len,1)  recover char sequence order 
            label_seq_tensor: (batch_size, max_sent_len)
            mask: (batch_size, max_sent_len) 
    """
    batch_size = len(input_batch_list)
    # print batch_size
    # print input_batch_list
    words = [sent[0] for sent in input_batch_list]
    features = [np.asarray(sent[1]) for sent in input_batch_list]
    feature_num = len(features[0][0])
    chars = [sent[2] for sent in input_batch_list]
    trans = [sent[3] for sent in input_batch_list]
    labels = [sent[4] for sent in input_batch_list]

    # print "words:{}".format(words)
    # print "chars:{}".format(chars)
    # print"trans:{}".format(trans)
    # print "labels:{}".format(labels)
    # exit(0)

    word_seq_lengths = torch.LongTensor(map(len, words))
    # print "word_seq_lengths:{}".format(word_seq_lengths)
    max_seq_len = word_seq_lengths.max()
    # print "max_seq_len:{}".format(max_seq_len)
    word_seq_tensor = autograd.Variable(torch.zeros((batch_size, max_seq_len)), volatile=volatile_flag).long()
    label_seq_tensor = autograd.Variable(torch.zeros((batch_size, max_seq_len)), volatile=volatile_flag).long()
    feature_seq_tensors = []
    for idx in range(feature_num):
        feature_seq_tensors.append(
            autograd.Variable(torch.zeros((batch_size, max_seq_len)), volatile=volatile_flag).long())
    mask = autograd.Variable(torch.zeros((batch_size, max_seq_len)), volatile=volatile_flag).byte()
    for idx, (seq, label, seqlen) in enumerate(zip(words, labels, word_seq_lengths)):
        word_seq_tensor[idx, :seqlen] = torch.LongTensor(seq)
        label_seq_tensor[idx, :seqlen] = torch.LongTensor(label)
        mask[idx, :seqlen] = torch.Tensor([1] * seqlen)
        for idy in range(feature_num):
            feature_seq_tensors[idy][idx, :seqlen] = torch.LongTensor(features[idx][:, idy])

    # print "word_seq_tensor befor reordering:{}".format(word_seq_tensor)
    # print "label_seq_tensor befor reordering:{}".format(label_seq_tensor)

    word_seq_lengths, word_perm_idx = word_seq_lengths.sort(0, descending=True)
    # print "word_seq_lengths after func sort:{}".format(word_seq_lengths)
    # print "word_perm_idx after func sort:{}".format(word_perm_idx)
    word_seq_tensor = word_seq_tensor[word_perm_idx]
    # print "word_seq_tensor reorder:{}".format(word_seq_tensor)
    for idx in range(feature_num):
        feature_seq_tensors[idx] = feature_seq_tensors[idx][word_perm_idx]

    label_seq_tensor = label_seq_tensor[word_perm_idx]
    mask = mask[word_perm_idx]
    # print "label_seq_tensor:{}".format(label_seq_tensor)
    # print "mask:{}".format(mask)

    ### deal with char
    # pad_chars (batch_size, max_seq_len)
    pad_chars = [chars[idx] + [[0]] * (max_seq_len - len(chars[idx])) for idx in range(len(chars))]
    # print "pad_chars:{}".format(pad_chars)
    length_list = [map(len, pad_char) for pad_char in pad_chars]
    # print "length_list:{}".format(length_list)
    max_word_len = max(map(max, length_list))
    # print "max_word_len:{}".format(max_word_len)
    char_seq_tensor = autograd.Variable(torch.zeros((batch_size, max_seq_len, max_word_len)),
                                        volatile=volatile_flag).long()
    char_seq_lengths = torch.LongTensor(length_list)
    # print "char_seq_lengths:{}".format(char_seq_lengths)
    for idx, (seq, seqlen) in enumerate(zip(pad_chars, char_seq_lengths)):
        for idy, (word, wordlen) in enumerate(zip(seq, seqlen)):
            # print len(word), wordlen
            char_seq_tensor[idx, idy, :wordlen] = torch.LongTensor(word)

    # print "char_seq_tensor:{}".format(char_seq_tensor)
    char_seq_tensor = char_seq_tensor[word_perm_idx].view(batch_size * max_seq_len, -1)
    # print "char_seq_tensor after reorder：{}".format(char_seq_tensor)
    char_seq_lengths = char_seq_lengths[word_perm_idx].view(batch_size * max_seq_len, )
    # print "char_seq_lengths after reorder：{}".format(char_seq_lengths)
    char_seq_lengths, char_perm_idx = char_seq_lengths.sort(0, descending=True)
    # print "char_seq_lengths:{}".format(char_seq_lengths)
    # print "char_perm_idx:{}".format(char_perm_idx)
    char_seq_tensor = char_seq_tensor[char_perm_idx]
    # print "char_seq_tensor:{}".format(char_seq_tensor)
    _, char_seq_recover = char_perm_idx.sort(0, descending=False)
    # print "char_seq_recover:{}".format(char_seq_recover)
    _, word_seq_recover = word_perm_idx.sort(0, descending=False)
    # print "word_seq_recover:{}".format(word_seq_recover)

    ### deal with trans
    pad_trans = [trans[idx] + [[0]] * (max_seq_len - len(trans[idx])) for idx in range(len(trans))]
    # print "pad_trans:{}".format(pad_trans)
    trans_length_list = [map(len, pdd_tran) for pdd_tran in pad_trans]
    # print "trans_length_list:{}".format(trans_length_list)
    max_tran_len = max(map(max, trans_length_list))
    # print "max_tran_len:{}".format(max_tran_len)
    trans_seq_tensor = autograd.Variable(torch.zeros((batch_size, max_seq_len, max_tran_len)),
                                         volatile=volatile_flag).long()
    trans_seq_lengths = torch.LongTensor(trans_length_list)
    for idx, (seq, seqlen) in enumerate(zip(pad_trans, trans_seq_lengths)):
        for idy, (tran, tranlen) in enumerate(zip(seq, seqlen)):
            trans_seq_tensor[idx, idy, :tranlen] = torch.LongTensor(tran)

    # print "trans_seq_tensor:{}".format(trans_seq_tensor)
    trans_seq_tensor = trans_seq_tensor[word_perm_idx].view(batch_size * max_seq_len, -1)
    # print "trans_seq_tensor:{}".format(trans_seq_tensor)
    trans_seq_lengths = trans_seq_lengths[word_perm_idx].view(batch_size * max_seq_len, )
    # print "trans_seq_lengths:{}".format(trans_seq_lengths)
    trans_seq_lengths, trans_perm_idx = trans_seq_lengths.sort(0, descending=True)
    trans_seq_tensor = trans_seq_tensor[trans_perm_idx]
    _, trans_seq_recover = trans_perm_idx.sort(0, descending=False)

    if gpu:
        word_seq_tensor = word_seq_tensor.cuda()
        for idx in range(feature_num):
            feature_seq_tensors[idx] = feature_seq_tensors[idx].cuda()
        word_seq_lengths = word_seq_lengths.cuda()
        word_seq_recover = word_seq_recover.cuda()
        label_seq_tensor = label_seq_tensor.cuda()
        char_seq_tensor = char_seq_tensor.cuda()
        char_seq_recover = char_seq_recover.cuda()
        trans_seq_tensor = trans_seq_tensor.cuda()
        trans_seq_recover = trans_seq_recover.cuda()
        mask = mask.cuda()
    return word_seq_tensor, feature_seq_tensors, word_seq_lengths, word_seq_recover, \
           char_seq_tensor, char_seq_lengths, char_seq_recover, \
           label_seq_tensor, \
           trans_seq_tensor, trans_seq_lengths, trans_seq_recover, mask


def train(data):
    print "Training model..."
    data.show_data_summary()
    save_data_name = data.model_dir + ".dset"
    data.save(save_data_name)
    model = SeqModel(data)
    if data.HP_gpu:
        model.cuda()

    if data.optimizer.lower() == "sgd":
        optimizer = optim.SGD(model.parameters(), lr=data.HP_lr, momentum=data.HP_momentum, weight_decay=data.HP_l2)
    elif data.optimizer.lower() == "adagrad":
        optimizer = optim.Adagrad(model.parameters(), lr=data.HP_lr, weight_decay=data.HP_l2)
    elif data.optimizer.lower() == "adadelta":
        optimizer = optim.Adadelta(model.parameters(), lr=data.HP_lr, weight_decay=data.HP_l2)
    elif data.optimizer.lower() == "rmsprop":
        optimizer = optim.RMSprop(model.parameters(), lr=data.HP_lr, weight_decay=data.HP_l2)
    elif data.optimizer.lower() == "adam":
        optimizer = optim.Adam(model.parameters(), lr=data.HP_lr, weight_decay=data.HP_l2)
    else:
        print("Optimizer illegal: %s" % (data.optimizer))
        exit(0)

    optimizer_wc = optim.SGD(model.word_hidden.wordrep.w.parameters(), lr=data.HP_lr, momentum=data.HP_momentum,
                             weight_decay=data.HP_l2)

    best_dev = -10
    ## start training
    for idx in range(data.HP_iteration):
        epoch_start = time.time()
        temp_start = epoch_start
        print("Epoch: %s/%s" % (idx, data.HP_iteration))
        if data.optimizer == "SGD":
            optimizer = lr_decay(optimizer, idx, data.HP_lr_decay, data.HP_lr)
            optimizer_wc = lr_decay(optimizer_wc, idx, data.HP_lr_decay, data.HP_lr)

        instance_count = 0
        sample_id = 0
        sample_loss = 0
        total_loss = 0
        right_token = 0
        whole_token = 0
        random.shuffle(data.train_Ids)
        ## set model in train model
        model.train()
        model.zero_grad()
        batch_size = data.HP_batch_size
        batch_id = 0
        train_num = len(data.train_Ids)
        total_batch = train_num // batch_size + 1
        for batch_id in range(total_batch):
            start = batch_id * batch_size
            end = (batch_id + 1) * batch_size
            if end > train_num:
                end = train_num
            instance = data.train_Ids[start:end]
            if not instance:
                continue
            batch_word, batch_features, batch_wordlen, batch_wordrecover, batch_char, batch_charlen, batch_charrecover, batch_label, batch_trans, trans_seq_lengths, trans_seq_recover, mask = batchify_with_label(
                instance, data.HP_gpu)
            instance_count += 1
            loss, tag_seq, wc_loss = model.neg_log_likelihood_loss(batch_word, batch_features, batch_wordlen,
                                                                   batch_char,
                                                                   batch_charlen, batch_charrecover, batch_label, mask,
                                                                   batch_trans, trans_seq_lengths, trans_seq_recover)
            right, whole = predict_check(tag_seq, batch_label, mask)
            right_token += right
            whole_token += whole
            sample_loss += loss.data[0]
            total_loss += loss.data[0]
            if end % 500 == 0:
                temp_time = time.time()
                temp_cost = temp_time - temp_start
                temp_start = temp_time
                print("     Instance: %s; Time: %.2fs; loss: %.4f; acc: %s/%s=%.4f" % (
                    end, temp_cost, sample_loss, right_token, whole_token, (right_token + 0.) / whole_token))
                sys.stdout.flush()
                sample_loss = 0
            for param in model.word_hidden.wordrep.w.parameters():
                param.requires_grad = False
            loss.backward(retain_graph=True)
            optimizer.step()
            model.zero_grad()
            for param in model.word_hidden.wordrep.w.parameters():
                param.requires_grad = True
            wc_loss.backward()
            optimizer_wc.step()
            model.zero_grad()
        temp_time = time.time()
        temp_cost = temp_time - temp_start
        print("     Instance: %s; Time: %.2fs; loss: %.4f; acc: %s/%s=%.4f" % (
            end, temp_cost, sample_loss, right_token, whole_token, (right_token + 0.) / whole_token))
        epoch_finish = time.time()
        epoch_cost = epoch_finish - epoch_start
        print("Epoch: %s training finished. Time: %.2fs, speed: %.2fst/s,  total loss: %s" % (
            idx, epoch_cost, train_num / epoch_cost, total_loss))
        # continue
        speed, acc, p, r, f, _, _ = evaluate(data, model, "dev")
        dev_finish = time.time()
        dev_cost = dev_finish - epoch_finish

        if data.seg:
            current_score = f
            print("Dev: time: %.2fs, speed: %.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f" % (
                dev_cost, speed, acc, p, r, f))
        else:
            current_score = acc
            print("Dev: time: %.2fs speed: %.2fst/s; acc: %.4f" % (dev_cost, speed, acc))

        if current_score > best_dev:
            if data.seg:
                print "Exceed previous best f score:", best_dev
            else:
                print "Exceed previous best acc score:", best_dev
            model_name = data.model_dir + '.' + str(idx) + ".model"
            print "Save current best model in file:", model_name
            torch.save(model.state_dict(), model_name)
            best_dev = current_score
            # ## decode test
        speed, acc, p, r, f, _, _ = evaluate(data, model, "test")
        test_finish = time.time()
        test_cost = test_finish - dev_finish
        if data.seg:
            print("Test: time: %.2fs, speed: %.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f" % (
                test_cost, speed, acc, p, r, f))
        else:
            print("Test: time: %.2fs, speed: %.2fst/s; acc: %.4f" % (test_cost, speed, acc))
        gc.collect()


def load_model_decode(data, name):
    print "Load Model from file: ", data.model_dir
    model = SeqModel(data)
    # load model need consider if the model trained in GPU and load in CPU, or vice versa
    if data.HP_gpu:
        model.load_state_dict(torch.load(data.load_model_dir, map_location='gpu'))
    else:
        model.load_state_dict(torch.load(data.load_model_dir, map_location='cpu'))

    print("Decode %s data, nbest: %s ..." % (name, data.nbest))
    start_time = time.time()
    speed, acc, p, r, f, pred_results, pred_scores = evaluate(data, model, name, data.nbest)
    end_time = time.time()
    time_cost = end_time - start_time
    if data.seg:
        print("%s: time:%.2fs, speed:%.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f" % (
            name, time_cost, speed, acc, p, r, f))
    else:
        print("%s: time:%.2fs, speed:%.2fst/s; acc: %.4f" % (name, time_cost, speed, acc))
    return pred_results, pred_scores


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Low Resource NER via Cross-lingual Knowledge-Transfer')
    # parser.add_argument('--status', choices=['train', 'decode'], help='update algorithm', default='train')
    parser.add_argument('--config', default="./demo.train.config", help='Configuration File')

    args = parser.parse_args()
    data = Data()
    data.read_config(args.config)
    status = data.status.lower()  # train or test
    data.HP_gpu = torch.cuda.is_available()
    print "Seed num:", seed_num

    if status == 'train':
        print("MODEL: train")

        # data.initial_feature_alphabets()
        data.build_alphabet(data.train_dir)
        # data.build_alphabet(data.dev_dir)
        # data.build_alphabet(data.test_dir)
        data.build_translation_alphabet(data.trans_dir)
        data.fix_alphabet()
        data.build_translation_dict(data.trans_dir)

        data.generate_instance('train')
        data.generate_instance('dev')
        data.generate_instance('test')
        data.build_pretrain_emb()
        # print data.word_alphabet.instance2index
        # print data.char_alphabet.instance2index
        # print data.label_alphabet.instance2index
        # print data.translation_alphabet.instance2index
        # print data.train_Ids
        # exit(0)
        # print data.train_texts
        train(data)
    elif status == 'decode':
        print("MODEL: decode")
        data.load(data.dset_dir)
        data.read_config(args.config)
        print data.raw_dir
        # exit(0) 
        data.show_data_summary()
        data.generate_instance('raw')
        print("nbest: %s" % (data.nbest))
        decode_results, pred_scores = load_model_decode(data, 'raw')
        if data.nbest:
            data.write_nbest_decoded_results(decode_results, pred_scores, 'raw')
        else:
            data.write_decoded_results(decode_results, 'raw')
    else:
        print "Invalid argument! Please use valid arguments! (train/test/decode)"