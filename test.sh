# obtain origin data first
# download model paramters of GPT2 and BERT


# obtain test data
# python preprocess.py --in_file /apdcephfs/share_916081/visionshao/Exp4Dialogue/data/wizard_of_wikipedia/tes_random_split.json --out_file wizard_of_wikipedia/data/test_seen.jsonl
# python preprocess.py --in_file /apdcephfs/share_916081/visionshao/Exp4Dialogue/data/wizard_of_wikipedia/test_topic_split.json --out_file wizard_of_wikipedia/data/test_unseen.json

python evaluate.py --eval_batch_size 2 --gpu_list 0 --exp_name test