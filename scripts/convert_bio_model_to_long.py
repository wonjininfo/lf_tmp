import logging
import os
import math
from dataclasses import dataclass, field
# from transformers import RobertaForMaskedLM, RobertaTokenizerFast
from transformers import BertForMaskedLM, BertTokenizerFast # BertTokenizerFast
from transformers import AutoTokenizer, AutoModel
from transformers import RobertaForMaskedLM, RobertaTokenizerFast
from transformers import TextDataset, DataCollatorForLanguageModeling, Trainer
from transformers import TrainingArguments, HfArgumentParser
from transformers.modeling_longformer import LongformerSelfAttention

import torch
import pdb

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class BertLongSelfAttention(LongformerSelfAttention):
    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
    ):
        return super().forward(hidden_states, attention_mask=attention_mask, output_attentions=output_attentions)


class BertLongForMaskedLM(BertForMaskedLM):
    def __init__(self, config):
        super().__init__(config)
        for i, layer in enumerate(self.bert.encoder.layer):
            # replace the `modeling_bert.BertSelfAttention` object with `LongformerSelfAttention`
            layer.attention.self = BertLongSelfAttention(config, layer_id=i)


def create_long_model(save_model_to, attention_window, max_pos):
    model = BertForMaskedLM.from_pretrained("microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract")
    config = model.config
    tokenizer = BertTokenizerFast.from_pretrained("microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract", model_max_length=max_pos)
    #tokenizer = RobertaTokenizerFast.from_pretrained('roberta-base', model_max_length=max_pos)
    #pdb.set_trace()
    # extend position embeddings
    tokenizer.model_max_length = max_pos
    tokenizer.init_kwargs['model_max_length'] = max_pos
    current_max_pos, embed_size = model.bert.embeddings.position_embeddings.weight.shape
    #max_pos += 2  # NOTE: RoBERTa has positions 0,1 reserved, so embedding size is max position + 2
    config.max_position_embeddings = max_pos
    assert max_pos > current_max_pos
    # allocate a larger position embedding matrix
    new_pos_embed = model.bert.embeddings.position_embeddings.weight.new_empty(max_pos, embed_size)
    model.bert.embeddings.register_buffer("position_ids",torch.arange(config.max_position_embeddings).expand((1, -1)),)
    
    # copy position embeddings over and over to initialize the new position embeddings
    k = 0
    step = current_max_pos 
    while k < max_pos - 1:
        new_pos_embed[k:(k + step)] = model.bert.embeddings.position_embeddings.weight
        k += step
    model.bert.embeddings.position_embeddings.weight.data = new_pos_embed

    # replace the `modeling_bert.BertSelfAttention` object with `LongformerSelfAttention`
    config.attention_window = [attention_window] * config.num_hidden_layers
    for i, layer in enumerate(model.bert.encoder.layer):
        longformer_self_attn = LongformerSelfAttention(config, layer_id=i)
        longformer_self_attn.query = layer.attention.self.query
        longformer_self_attn.key = layer.attention.self.key
        longformer_self_attn.value = layer.attention.self.value

        longformer_self_attn.query_global = layer.attention.self.query
        longformer_self_attn.key_global = layer.attention.self.key
        longformer_self_attn.value_global = layer.attention.self.value

        layer.attention.self = longformer_self_attn

    logger.info(f'saving model to {save_model_to}')
    model.save_pretrained(save_model_to)
    tokenizer.save_pretrained(save_model_to)
    #pdb.set_trace()
    return model, tokenizer


def copy_proj_layers(model):
    for i, layer in enumerate(model.bert.encoder.layer):
        layer.attention.self.query_global = layer.attention.self.query
        layer.attention.self.key_global = layer.attention.self.key
        layer.attention.self.value_global = layer.attention.self.value
    return model


def pretrain_and_evaluate(args, model, tokenizer, eval_only, model_path):
    if tokenizer.model_max_length > 1e8:
        val_dataset = TextDataset(tokenizer=tokenizer,
                                  file_path=args.val_datapath,
                                  block_size=512)
        logger.info(f'[WARNING] tokenizer.model_max_length > 10^8: {tokenizer.model_max_length} setting the value as 512 instead.')
    else:
        val_dataset = TextDataset(tokenizer=tokenizer,
                                  file_path=args.val_datapath,
                                  block_size=tokenizer.model_max_length) #  The `max_len` attribute has been deprecated 
    
    if eval_only:
        train_dataset = val_dataset
    else:
        logger.info(f'Loading and tokenizing training data is usually slow: {args.train_datapath}')
        if tokenizer.model_max_length > 1e8:
            train_dataset = TextDataset(tokenizer=tokenizer,
                                    file_path=args.train_datapath,
                                    block_size=512)
            logger.info(f'[WARNING] tokenizer.model_max_length > 10^8: {tokenizer.model_max_length} setting the value as 512 instead.')
        else:
            train_dataset = TextDataset(tokenizer=tokenizer,
                                    file_path=args.train_datapath,
                                    block_size=tokenizer.model_max_length)

    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=True, mlm_probability=0.15)
    
    trainer = Trainer(model=model, args=args, data_collator=data_collator,
                      train_dataset=train_dataset, eval_dataset=val_dataset, prediction_loss_only=True,)

    eval_loss = trainer.evaluate()
    #pdb.set_trace()
    eval_loss = eval_loss['eval_loss']
    logger.info(f'Initial eval bpc: {eval_loss/math.log(2)}')
    
    if not eval_only:
        trainer.train(model_path=model_path)
        trainer.save_model()

        eval_loss = trainer.evaluate()
        eval_loss = eval_loss['eval_loss']
        logger.info(f'Eval bpc after pretraining: {eval_loss/math.log(2)}')


@dataclass
class ModelArgs:
    attention_window: int = field(default=512, metadata={"help": "Size of attention window"})
    max_pos: int = field(default=4096, metadata={"help": "Maximum position"})

parser = HfArgumentParser((TrainingArguments, ModelArgs,))


training_args, model_args = parser.parse_args_into_dataclasses(look_for_args_file=False, args=[
    '--output_dir', 'tmp',
    '--warmup_steps', '500',
    '--learning_rate', '0.00003',
    '--weight_decay', '0.01',
    '--adam_epsilon', '1e-6',
    '--max_steps', '3000',
    '--logging_steps', '500',
    '--save_steps', '500',
    '--max_grad_norm', '5.0',
    '--per_gpu_eval_batch_size', '8',
    '--per_gpu_train_batch_size', '2',  # 32GB gpu with fp32
    '--gradient_accumulation_steps', '32',
    '--evaluate_during_training',
    '--do_train',
    '--do_eval',
])
training_args.val_datapath = '/hdd2/wonjinlf/github/longformer/wikitext-103-raw/wiki.valid.raw'
training_args.train_datapath = '/hdd2/wonjinlf/github/longformer/wikitext-103-raw/wiki.train.raw'
#training_args.val_datapath = 'wikitext-103-raw/wiki.valid.raw'
#training_args.train_datapath = 'wikitext-103-raw/wiki.train.raw'

# Choose GPU
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "0"


## Put it all together
# 1) Evaluating PubMedBERT on MLM to establish a baseline. Validation bpc = 2.536 which is higher than the bpc values in table 6 here because wikitext103 is harder than our pretraining corpus.
bert_base = BertForMaskedLM.from_pretrained("microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract")
# roberta_base_tokenizer = RobertaTokenizerFast.from_pretrained('PubMedBERT')
tokenizer = BertTokenizerFast.from_pretrained("microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract")
logger.info('Evaluating PubMedBERT (seqlen: 512) for refernece ...')
pretrain_and_evaluate(training_args, bert_base, tokenizer, eval_only=True, model_path=None)



# 2) As descriped in create_long_model, convert a PubMedBERT model into PubMedBERT-4096 which is an instance of RobertaLong, then save it to the disk.
model_path = f'{training_args.output_dir}/PubMedBERT-{model_args.max_pos}'
if not os.path.exists(model_path):
    os.makedirs(model_path)

logger.info(f'Converting PubMedBERT into PubMedBERT-{model_args.max_pos}')
model, tokenizer = create_long_model(
    save_model_to=model_path, attention_window=model_args.attention_window, max_pos=model_args.max_pos)


# 3) Load PubMedBERT-4096 from the disk. This model works for long sequences even without pretraining. If you don't want to pretrain, you can stop here and start finetuning your PubMedBERT\-4096 on downstream tasks 
logger.info(f'Loading the model from {model_path}')
tokenizer = BertTokenizerFast.from_pretrained(model_path)
model = BertLongForMaskedLM.from_pretrained(model_path)


# 4) Pretrain PubMedBERT\-4096 for 3k steps, each steps has 2^18 tokens. Notes:
logger.info(f'Pretraining PubMedBERT-{model_args.max_pos} ... ')

training_args.max_steps = 3   ## <<<<<<<<<<<<<<<<<<<<<<<< REMOVE THIS <<<<<<<<<<<<<<<<<<<<<<<<
training_args.per_gpu_train_batch_size = 1

pretrain_and_evaluate(training_args, model, tokenizer, eval_only=False, model_path=training_args.output_dir)

# 5) Copy global projection layers. MLM pretraining doesn't train global projections, so we need to call copy_proj_layers to copy the local projection layers to the global ones.
logger.info("5) Copy global projection layers. MLM pretraining doesn't train global projections, so we need to call copy_proj_layers to copy the local projection layers to the global ones.")

logger.info(f'Copying local projection layers into global projection layers ... ')
model = copy_proj_layers(model)
logger.info(f'Saving model to {model_path}')
model.save_pretrained(model_path)


logger.info(f'DONE!!!!')
