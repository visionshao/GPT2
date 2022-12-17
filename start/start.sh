export PYTHONIOENCODING=utf-8
export https_proxy=http://star-proxy.oa.com:3128
export http_proxy=http://star-proxy.oa.com:3128

cd /apdcephfs/share_916081/visionshao/Exp4Dialogue/GPT2
save_dir=/apdcephfs/share_916081/visionshao/Exp4Dialogue/GPT2/wizard_of_wikipedia/log/1216_test
# /apdcephfs/share_916081/visionshao/DevTools/anaconda3/envs/knowledgeGPT/bin/python3 train_gen.py
/apdcephfs/share_916081/visionshao/DevTools/anaconda3/envs/knowledgeGPT/bin/python3 train_gen.py |& tee $save_dir/train.log