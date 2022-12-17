import argparse
import os

import torch
from torch.nn import functional as F
import torch.nn as nn
import numpy as np
import math
from tqdm import tqdm
from str2bool import str2bool

from datetime import datetime

from utils import (
    occupy_mem_new,
    save_hparams,
    KGDataset,
    collate_fn,
    get_batch_loader,
    DisBatcher,
    GenBatcher,
)
from metrics import (
    bleu_metric,
    distinct_metric,
    f1_metric
)
from model.util import sequence_loss, weighted_sequence_loss
from transformers import GPT2PreTrainedModel, GPT2Model, GPT2Config

class GPT2Summ(GPT2PreTrainedModel):
    '''succeed from GPT2PreTraninedModel which has implemented the 'generate' func'''

    def __init__(self, tokenizer, gpt2_config, segment=True):
        config = GPT2Config.from_pretrained(gpt2_config)
        super(GPT2Summ, self).__init__(config)
        self.transformer = GPT2Model.from_pretrained(gpt2_config)
        self.transformer.resize_token_embeddings(len(tokenizer))
        self.user_id = [tokenizer.convert_tokens_to_ids('<user1>'),
                        tokenizer.convert_tokens_to_ids('<user2>')]
        self.know_id = tokenizer.convert_tokens_to_ids('<knowledge>')
        self.segment = segment

        self.lm_head = nn.Linear(config.n_embd, len(tokenizer), bias=False)
        self.config.vocab_size = len(tokenizer)
        self.tie_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, **kwargs):
        token_type_ids = []
        for i in range(input_ids.size(0)):
            ids = input_ids[i].tolist()
            type_ids = []
            last_special_token = self.know_id
            for j in range(len(ids)):
                if ids[j] in ([self.know_id] + self.user_id):
                    type_ids.append(ids[j])
                    last_special_token = ids[j]
                else:
                    type_ids.append(last_special_token)
            token_type_ids.append(type_ids)
        token_type_ids = torch.tensor(token_type_ids).type_as(input_ids)

        # only last token for inputs_ids if past is defined in kwargs
        if "past" in kwargs and kwargs["past"]:
            input_ids = input_ids[:, -1].unsqueeze(-1)
            token_type_ids = token_type_ids[:, -1].unsqueeze(-1)

        if self.segment:
            inputs = {"input_ids": input_ids, "token_type_ids": token_type_ids}
        else:
            inputs = {"input_ids": input_ids}
        inputs.update(kwargs)
        return inputs

    def forward(self, input_ids, past=None, attention_mask=None, token_type_ids=None):
        transformer_outputs = self.transformer(input_ids, past=past, token_type_ids=token_type_ids)
        hidden_states = transformer_outputs[0]
        lm_logits = self.lm_head(hidden_states)
        return (lm_logits,) + transformer_outputs[1:]

    def batch_decode(self, input_ids, max_len, min_len, early_stopping, beam_size,
                     repetition_penalty, eos_id, length_penalty, no_repeat_ngram_size):
        # new-version
        output_sequences = self.generate(
            input_ids=input_ids,
            max_length=input_ids.size(1) + max_len,
            min_length=input_ids.size(1) + min_len,
            do_sample=False,
            early_stopping=early_stopping,
            num_beams=beam_size,
            repetition_penalty=repetition_penalty,
            pad_token_id=0,
            # pad_token_id=None,
            eos_token_id=eos_id,
            length_penalty=length_penalty,
            no_repeat_ngram_size=no_repeat_ngram_size,
        )

        return output_sequences

    def old_batch_decode(self, input_ids, max_len, eos_id, start_id):
        # new-version
        output_sequences = self.generate(
            input_ids=input_ids,
            max_length=input_ids.size(1) + max_len,
            do_sample=False,
            # early_stopping=True,
            num_beams=1,
            repetition_penalty=1.0,
            pad_token_id=0,
            eos_token_id=eos_id,
            length_penalty=1.0,
            # no_repeat_ngram_size=3, #
            # decoder_start_token_id=start_id, #
        )

        return output_sequences

def load_gen_net(tokenizer, segment, gpt2_config, gen_pretrain_file, load=True, cuda=True):
    gen = GPT2Summ(tokenizer=tokenizer, gpt2_config=gpt2_config, segment=segment)

    if load:
        print("Restoring all non-adagrad variables from {}...".format(gen_pretrain_file))
        state_dict = torch.load(gen_pretrain_file)['state_dict']
        gen.load_state_dict(state_dict)
    if cuda:
        gen = gen.cuda()
    return gen

def main(args):
    log_f = open("log.txt", "a")
    print("\nParameters:")
    for attr, value in sorted(vars(args).items()):
        print("{}={}".format(attr.upper(), value))
    print("")

    # Selecting wihch GPU to use
    # if not args.ms:
    #     occupy_mem_new(args.gpu_list.split(','), ratio=args.gpu_ratio, num_devices=args.n_device)
    # else:
    #     os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_list
    args.cuda = torch.cuda.is_available() and not args.no_cuda

    # Output directory for models and summaries
    out_dir = os.path.join(args.log, args.exp_name)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    print('Writing to {}\n'.format(out_dir), file=log_f)
    save_hparams(args, os.path.join(out_dir, 'hparams'))


    # Checkpoint directory
    checkpoint_dir = os.path.join(out_dir, 'checkpoints')
    checkpoint_prefix = os.path.join(checkpoint_dir, 'model')
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)


    # Build dataset
    time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print("Create training dataset begain... | %s " % time_str)

    train_dataset = KGDataset(args.train_file, max_knowledge=args.max_knowledge) # 74092
    print(len(train_dataset))
    test_seen_dataset = KGDataset(args.test_seen_file, max_knowledge=999) # 3865
    print(len(test_seen_dataset))
    test_unseen_dataset = KGDataset(args.test_unseen_file, max_knowledge=999) # 3924
    print(len(test_unseen_dataset))

    train_loader = get_batch_loader(train_dataset, collate_fn=collate_fn, batch_size=args.batch_size, is_test=False)
    test_seen_loader = get_batch_loader(test_seen_dataset, collate_fn=collate_fn, batch_size=args.eval_batch_size, is_test=True)
    test_unseen_loader = get_batch_loader(test_unseen_dataset, collate_fn=collate_fn, batch_size=args.eval_batch_size, is_test=True)

    time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print("Create training dataset end... | %s " % time_str)

    # Training
    gen_batcher = GenBatcher(args.knowledge_truncate, args.text_truncate, args.gpt2_truncate, args.gpt2_config, args.cuda)

    # Load model
    gen_model = load_gen_net(gen_batcher.tokenizer, args.segment, args.gpt2_config, args.gen_pretrain_file, args.load_gen, args.cuda)

    ce = lambda logit, target: F.cross_entropy(logit, target, reduce=False)
    gen_criterion = lambda logits, targets, weights: weighted_sequence_loss(logits, targets, weights, ce, pad_idx=-1)
    gen_optimizer = torch.optim.Adam(gen_model.parameters(), lr=args.lr)
    gen_schedule = torch.optim.lr_scheduler.ReduceLROnPlateau(gen_optimizer, 'min', verbose=True, factor=args.decay, min_lr=0, patience=0)
    # gen_schedule = torch.optim.lr_scheduler.ReduceLROnPlateau(gen_optimizer, 'max', verbose=True, factor=args.decay, min_lr=0, patience=0)

    def train_step(global_step):
        loss = 0.0
        curr_temp = max(args.init_temp * math.exp(-args.anneal_rate * global_step), args.min_temp)
        gen_model.train()
        for _ in range(args.accum_steps):
            knowledges, histories, users, responses = next(train_loader)
            # random
            for know in knowledges:
                np.random.shuffle(know)
            # filter
            weights = [1.0 for bi in range(len(knowledges))]
            lm_input, token_type_ids, lm_target = gen_batcher(knowledges, histories, users, responses, args.segment, True)
            gen_loss = gen_criterion(gen_model(lm_input, token_type_ids=token_type_ids)[0], lm_target, weights).mean()
            gen_loss = gen_loss / len(knowledges)
            gen_loss.backward()
            loss += gen_loss.item()

        grad_norm = torch.nn.utils.clip_grad_norm_(
            [p for p in gen_model.parameters() if p.requires_grad], args.clip)
        if grad_norm >= 1e2:
            print('WARNING: Exploding Gradients {:.2f}'.format(grad_norm))
        gen_optimizer.step()
        gen_model.zero_grad()

        if global_step % args.print_every == 0 and global_step != 0:
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print("Step: %d \t| loss: %.3f \t| threshold: %.3f \t| %s" % (global_step, loss, curr_temp, time_str))


    def dev_step(split, global_step):
        if split == 'test_seen':
            test_loader = test_seen_loader
        elif split == 'test_unseen':
            test_loader = test_unseen_loader
        else:
            raise ValueError

        # dis_model.eval()
        gen_model.eval()

        n_token, test_loss = 0, 0.0 # ppl
        test_hyp, test_ref = [], []
        count = 0
        with torch.no_grad():
            # for knowledges, histories, users, responses, knowledge_lens in test_loader:
            #     knowledges = [know.split('\n\n') for know in knowledges]
            #     histories = [his.split('\n\n') for his in histories]
            for knowledges, histories, users, responses in test_loader:
                weights = [1.0 for bi in range(len(knowledges))]

                random_knowledges = []
                for know in knowledges:
                    np.random.shuffle(know)
                    random_knowledges.append(know)
                dis_knowledges = random_knowledges
                # dis_knowledges = knowledges

                gen_args = gen_batcher(dis_knowledges, histories, users, responses, args.segment, True)
                loss = gen_criterion(gen_model(gen_args[0], token_type_ids=gen_args[1])[0], gen_args[2], weights)
                n_token += loss.size(0)
                test_loss += loss.sum().item()
                # print(math.exp(test_loss / n_token))
                for bi in range(len(dis_knowledges)):
                    dec_in = gen_batcher(dis_knowledges[bi:bi+1], histories[bi:bi+1], users[bi:bi+1], segment=args.segment, training=False)
                    # dec_out = gen_model.batch_decode(dec_in, args.decode_length, gen_batcher.eos_id, gen_batcher.user_id[1])
                    dec_out = gen_model.batch_decode(dec_in, args.max_length, args.min_length, args.early_stopping,
                                    args.beam_size, args.repetition_penalty, gen_batcher.eos_id,
                                    args.length_penalty, args.no_repeat_ngram_size)
                    dec_out = dec_out[0].tolist()[dec_in.size(1):]
                    _hyp = gen_batcher.tokenizer.decode(dec_out, skip_special_tokens=True, clean_up_tokenization_spaces=False)
                    # print(_hyp.replace("\n", " "))
                    # print("--------------------")
                    _ref = responses[bi]
                    test_hyp.append(_hyp)
                    test_ref.append(_ref)
                    # print(_hyp.replace("\n", " "))
                    # print(_ref)
                    # print("*********************************")
                    count += 1
                    if count % 1000 == 0:
                        print(count)

        with open(os.path.join(out_dir, '{}-decoded-iter-{}.txt'.format(split, global_step)), 'w', encoding="utf-8") as f:
            for _hyp, _ref in zip(test_hyp, test_ref):
                f.writelines('{} ||| {}\n'.format(_hyp, _ref))

        MeanLoss = test_loss / n_token
        b1, b2, b3, b4 = bleu_metric(test_hyp, test_ref)
        d1, d2 = distinct_metric(test_hyp)
        f1 = f1_metric(test_hyp, test_ref)

        time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print("**********************************")
        print("{} results..........".format(split))
        print('hypothesis: ', len(test_hyp))
        print("Step: %d \t| ppl: %.3f \t|  %s" % (global_step, math.exp(MeanLoss), time_str))
        print("BLEU-1/2/3/4: {:.4f}/{:.4f}/{:.4f}/{:.4f}".format(b1, b2, b3, b4))
        print("Distinct-1/2: {:.4f}/{:.4f}".format(d1, d2))
        print("F1: {:.4f}".format(f1))
        print("**********************************")

        return {'f1': f1, 'loss': MeanLoss, 'bleu1': b1, 'bleu2': b2, 'bleu3': b3, 'bleu4': b4, 'distinct1': d1, 'distinct2': d2}


    best_f1 = 0
    best_loss = 99999.9
    for i in tqdm(range(args.num_steps)):
        train_step(i + 1)
        if (i + 1) % args.valid_every == 0:
            test_seen_results = dev_step("test_seen", i + 1)  # test_random_split
            test_unseen_results = dev_step("test_unseen", i + 1)  # test_topic_split
            gen_schedule.step(test_seen_results['loss'])
            # gen_schedule.step(test_seen_results['f1'])

            # if test_seen_results["f1"] > best_f1:
            if test_seen_results['loss'] < best_loss:
                # best_f1 = test_seen_results["f1"]
                best_loss = test_seen_results['loss']
                # save_dict = {'state_dict': gen_model.state_dict()}
                # torch.save(save_dict, '{}-{}'.format(checkpoint_prefix, i + 1))
                gen_dict = {'state_dict': gen_model.state_dict()}
                torch.save(gen_dict, '{}-gen-best'.format(checkpoint_prefix))
                print("Saved model checkpoint to {}\n".format(checkpoint_prefix))
                with open(os.path.join(out_dir, 'results'), 'w', encoding='utf-8') as result_file:
                    result_file.write("The best model is on step {}\n".format(i+1))
                    result_file.write("test seen result: \n")
                    result_file.write(
                        "PPL: {:.4f}\nBLEU-1/2/3/4: {:.4f}/{:.4f}/{:.4f}/{:.4f}\nDistinct-1/2: {:.4f}/{:.4f}\nF1: {:.4f}\n".format(
                            math.exp(test_seen_results['loss']),
                            test_seen_results['bleu1'], test_seen_results['bleu2'],
                            test_seen_results['bleu3'], test_seen_results['bleu4'],
                            test_seen_results['distinct1'], test_seen_results['distinct2'],
                            test_seen_results['f1']))

                    result_file.write("test unseen result: \n")
                    result_file.write(
                        "PPL: {:.4f}\nBLEU-1/2/3/4: {:.4f}/{:.4f}/{:.4f}/{:.4f}\nDistinct-1/2: {:.4f}/{:.4f}\nF1: {:.4f}\n".format(
                            math.exp(test_unseen_results['loss']),
                            test_unseen_results['bleu1'], test_unseen_results['bleu2'],
                            test_unseen_results['bleu3'], test_unseen_results['bleu4'],
                            test_unseen_results['distinct1'], test_unseen_results['distinct2'],
                            test_unseen_results['f1']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Pre-training for Knowledge-Grounded Conversation'
    )

    # files
    parser.add_argument('--train_file', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/main/data_prepare/train.jsonl')
    parser.add_argument('--test_seen_file', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/main/data_prepare/test_seen.jsonl')
    parser.add_argument('--test_unseen_file', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/main/data_prepare/test_unseen.jsonl')

    # training scheme
    parser.add_argument('--batch_size', type=int, default=2)
    parser.add_argument('--eval_batch_size', type=int, default=4)
    parser.add_argument('--num_steps', type=int, default=100000)
    parser.add_argument('--accum_steps', type=int, default=32)
    parser.add_argument('--lr', type=float, default=5e-5)
    parser.add_argument('--decay', type=float, default=0.5)
    parser.add_argument('--clip', type=float, default=2.0)
    parser.add_argument('--init_temp', type=float, default=0.5)
    parser.add_argument('--min_temp', type=float, default=0.2)
    parser.add_argument('--anneal_rate', type=float, default=0.001)
    parser.add_argument('--curriculum', type=str, default='pseudo')

    parser.add_argument('--print_every', type=int, default=100)
    parser.add_argument('--valid_every', type=int, default=1000)

    # save
    parser.add_argument('--exp_name', type=str, default='1216_test')
    parser.add_argument('--log', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/GPT2/wizard_of_wikipedia/log')

    parser.add_argument('--seed', type=int, default=42)

    # pre-train
    # parser.add_argument('--dis_pretrain_file', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/KnowledGPT/ft_local/EMNLP_code/ks_pretrain/wizard_of_wikipedia/log/0305_extract/ckpt/ckpt-2.342443-2000')
    # parser.add_argument('--gen_pretrain_file', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/KnowledGPT/ft_local/EMNLP_code/ks_pretrain/wizard_of_wikipedia/log/0429_pseudo_generative/checkpoints/')
    parser.add_argument('--gen_pretrain_file', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/GPT2/wizard_of_wikipedia/log/1206_test/checkpoints/model-gen-best')
    parser.add_argument('--load_dis', type=str2bool, default=False)
    parser.add_argument('--load_gen', type=str2bool, default=False)

    # model
    parser.add_argument('--bert_config', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/GPT2/pretrain-models/bert_base_uncased')
    parser.add_argument('--gpt2_config', type=str, default='/apdcephfs/share_916081/visionshao/Exp4Dialogue/GPT2/pretrain-models/gpt2')

    parser.add_argument('--bert_truncate', type=int, default=64) # for bert
    parser.add_argument('--gpt2_truncate', type=int, default=256) # for gpt2
    parser.add_argument('--knowledge_truncate', type=int, default=64) # for gpt2
    parser.add_argument('--text_truncate', type=int, default=128) # for gpt2
    parser.add_argument('--segment', type=str2bool, default=True)

    parser.add_argument('--n_sent', type=int, default=1)
    parser.add_argument('--decode_length', type=int, default=30)
    parser.add_argument('--max_knowledge', type=int, default=32)
    parser.add_argument('--emb_dim', type=int, default=768)
    parser.add_argument('--lstm_hidden', type=int, default=256)
    parser.add_argument('--lstm_layer', type=int, default=1)

    parser.add_argument('--max_length', type=int, default=30)
    parser.add_argument('--min_length', type=int, default=15)
    parser.add_argument('--early_stopping', type=str2bool, default=False)
    parser.add_argument('--beam_size', type=int, default=1)
    parser.add_argument('--repetition_penalty', type=float, default=1.0)
    parser.add_argument('--length_penalty', type=float, default=1.0)
    parser.add_argument('--no_repeat_ngram_size', type=int, default=0)


    # gpu
    parser.add_argument('--gpu_list', type=str, default='0')
    parser.add_argument('--gpu_ratio', type=float, default=0.85)
    parser.add_argument('--n_device', type=int, default=1)
    parser.add_argument('--ms', type=str2bool, default=False)
    parser.add_argument('--no_cuda', type=str2bool, default=False)

    args = parser.parse_args()

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    np.random.seed(args.seed)
    # random.seed(args.seed)
    torch.backends.cudnn.deterministic = True


    main(args)
