###############
# (later than) pytorch version: 2.0.1+cu117
# RuntimeError: Output 0 of BackwardHookFunctionBackward is a view and is being modified inplace. This view was created inside a custom Function (or because an input was returned as-is) and the autograd logic to handle view+inplace would override the custom backward associated with the custom Function, leading to incorrect gradients. This behavior is forbidden. You can fix this by cloning the output of the custom Function.

# Code root: /home/yusheng.su/.cache/huggingface/modules/transformers_modules/LLM360/CrystalCoder/a8c07fe67eb9ceb39acd5768c812d07dfc256015/modeling_crystalcoder.py

## (1)
# #Modify: in modeling_crystalcoder.py Line: 1030
## change from: hidden_states *= torch.tensor(some_scaling_factor, device=hidden_states.device)
## to: hidden_states = hidden_states * torch.tensor(some_scaling_factor, device=hidden_states.device)

## (2)
# #Modify: in modeling_crystalcoder.py Line: line 1299
##change from: lm_logits *= torch.tensor(float(self.output_logits_scale), dtype=lm_logits.dtype, device=lm_logits.device)
##to: lm_logits = lm_logits * torch.tensor(float(self.output_logits_scale), dtype=lm_logits.dtype, device=lm_logits.device
###############

# split into the layer to calculate w's bp and fp
# Given input, output; grad_output, grad_input, dw (value), 

################
#Fix issue:
#Note (ask to revise it for me): Another thing is the indices in subsample, the current one is probably okay, but ideally we want to pick [i for i in range(matrix_original.size(0)) if i % (reduction_factor * 2) < 2], e.g., when reduction_factor=2, the indices would be [0, 1, 4, 5, 8, 9, ...]
################


from ast import mod
import copy
#import imp
from operator import is_
from pickletools import optimize
from re import I, sub
from xml.etree.ElementTree import TreeBuilder
from sympy import O
import torch
from torch import nn, optim, threshold
import torch.nn.functional as F
import torch.backends.cuda as cuda
from torch.utils.data import DataLoader, IterableDataset

import wandb
from tqdm import tqdm
import bitsandbytes as bnb

from datasets import load_dataset
from transformers import GPTNeoXForCausalLM
from transformers.models.gpt_neox.modeling_gpt_neox import GPTNeoXAttention
from transformers import AutoConfig, AutoModel, AutoTokenizer, AutoModelForCausalLM, get_scheduler
import argparse
import os
from multiprocessing import cpu_count
import shutil
import math

from accelerate import Accelerator
import os
import argparse

from torch.distributions import Normal, kl_divergence
import torch.nn.functional as F

import re
import yaml
from accelerate import FullyShardedDataParallelPlugin, DistributedType
import inspect
from collections import defaultdict

# split data, 1:9
# add ppl

PROJECT="mup_training_2024_07_13"
ENTITY="mbzuai-llm"



class DatasetWrapper(IterableDataset):
    def __init__(self, max_tokens, cache_dir):
        ###
        #Tune code
        '''
        self.tokenizer = AutoTokenizer.from_pretrained("EleutherAI/pythia-70m")
        '''
        self.tokenizer = AutoTokenizer.from_pretrained(
            "LLM360/CrystalCoder",
            revision="CrystalCoder_phase1_checkpoint_055500",
            trust_remote_code=True
        )
        self.max_tokens = max_tokens
        self.cache_dir = cache_dir
        # pre-load the dataset --> pre-training data needed: 
        # self.dataset = load_dataset("Open-Orca/SlimOrca-Dedup",
        #         split="train",
        #         cache_dir=self.cache_dir
        # )
        ###########
        self.dataset = load_dataset("iankur/SlimPajama-1B",
            split="train",
            cache_dir=self.cache_dir
        )
        ###########
        self.train_size = int(0.9 * len(self.dataset))
        self.eval_size = len(self.dataset) - self.train_size
    def __train_size__(self):
        return self.train_size
    def __eval_size__(self):
        return self.eval_size

    # #### Instruction Tuning ########
    # def __iter__(self):
    #     # 90%: train, 10%: test
    #     train_dataset = self.dataset.select(range(self.train_size))
    #     valid_dataset = self.dataset.select(range(self.train_size, len(self.dataset)))
    #     self.tokenizer.pad_token = self.tokenizer.eos_token
    #     #for sample in train_dataset:
    #     for sample in valid_dataset:
    #         instruction_system = sample['conversations'][0]["value"]
    #         instruction_human = sample['conversations'][1]["value"]
    #         response = sample['conversations'][2]["value"]
    #         input_ = instruction_system + "\n" + instruction_human + "\n" + response
    #         tokens = self.tokenizer(input_, return_tensors='pt', max_length=self.max_tokens, padding="max_length", truncation=True).input_ids
    #         tokens = tokens.reshape(tokens.shape[0]*tokens.shape[1])
    #         yield tokens

    
    # #### Pre-training ########
    def __iter__(self):
        # 90%: train, 10%: test
        train_dataset = self.dataset.select(range(self.train_size))
        valid_dataset = self.dataset.select(range(self.train_size, len(self.dataset)))
        self.tokenizer.pad_token = self.tokenizer.eos_token
        for sample in valid_dataset:
            tokens = sample['text']
            tokens = self.tokenizer(tokens, return_tensors='pt', max_length=self.max_tokens, padding="max_length", truncation=True).input_ids
            tokens = tokens.reshape(tokens.shape[0]*tokens.shape[1])
            yield tokens


class LargerModel:
    def __init__(self, parser):
        self.llm = parser.llm
        self.max_tokens = parser.max_tokens
        self.grad = 64
        self.step = 0
        self.learning_rate = parser.learning_rate
        self.weight_decay = parser.weight_decay
        self.batch_size = parser.batch_size
        self.cache_dir = parser.cache_dir
        self.cpus = parser.cpus
        #self.device = parser.device
        self.target_dir = parser.target_dir
        self.revision = parser.revision
        self.distill_model_config = parser.distill_model_config
        self.model = AutoModelForCausalLM.from_pretrained(
            self.llm,
            revision = self.revision,
            cache_dir = self.cache_dir,
            trust_remote_code=True
        )

    def forward(self, x, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        return z

    def hidden(self, x, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        return z.hidden_states

    def next_token_prediction_loss(self, x, y):
        z = self.model(x).logits
        y = y.reshape(-1)
        z = z.view(-1, z.shape[-1])
        loss = F.cross_entropy(z, y)
        return loss

    def hidden_and_loss(self, x, y, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        y = y.reshape(-1)
        y_prime = z.logits.view(-1, z.logits.shape[-1])
        loss = F.cross_entropy(y_prime, y)
        return z, loss
    
    def logit_and_loss(self, x, y, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        y = y.reshape(-1)
        y_prime = z.logits.view(-1, z.logits.shape[-1])
        loss = F.cross_entropy(y_prime, y)
        return z.logits, loss

    def logit_and_hidden_and_loss(self, x, y, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        y = y.reshape(-1)
        y_prime = z.logits.view(-1, z.logits.shape[-1])
        loss = F.cross_entropy(y_prime, y)
        return z.logits, z.hidden_states, loss
    

# 1 D
def _subsample_embeddings(matrix_original, matrix_target, reduction_factor):
    #print(embeddings.shape)
    indices = torch.arange(0, matrix_original.size(0), reduction_factor)
    out_dim = int(indices.shape[0])
    target_d0 = int(matrix_target.shape[0])
    if out_dim == target_d0:
        pass
    else:
        indices = indices[:target_d0]
    subsampled_matrix = matrix_original[indices]
    return subsampled_matrix.data
def _subsample_and_scale(matrix_original, matrix_target, reduction_factor):
    #print(matrix.shape)
    indices = torch.arange(0, matrix_original.size(0), reduction_factor)
    #subsampled_matrix = matrix_original[indices][:, indices] * self.reduction_factor
    out_dim = int(indices.shape[0])
    target_d0, target_d1 = int(matrix_target.shape[0]), int(matrix_target.shape[1])
    if out_dim == target_d0:
        pass
    else:
        indices = indices[:target_d0]
    if out_dim == target_d1:
        pass
    else:
        indices = indices[:target_d1]
    subsampled_matrix = matrix_original[indices, :][:, indices] * reduction_factor
    return subsampled_matrix.data
def _subsample_embeddings_dim(matrix_original, matrix_target, reduction_factor):
    # Determine which dimension is larger
    #if matrix.size(0) < matrix.size(1):
    # Subsample only along the larger dimension
    indices_0 = torch.arange(0, matrix_original.size(0), reduction_factor)
    indices_1 = torch.arange(0, matrix_original.size(1), reduction_factor)
    out_dim_0 = int(indices_0.shape[0])
    out_dim_1 = int(indices_1.shape[0])
    target_d0, target_d1 = int(matrix_target.shape[0]), int(matrix_target.shape[1])
    if out_dim_0 == target_d0:
        pass
    else:
        indices_0 = indices_0[:target_d0]
    if out_dim_1 == target_d1:
        pass
    else:
        indices_1 = indices_1[:target_d1]
    subsampled_matrix = matrix_original[indices_0, :][: ,indices_1]
    return subsampled_matrix.data
def _subsample_embeddings_dim0(matrix_original, matrix_target, reduction_factor):
    indices = torch.arange(0, matrix_original.size(0), reduction_factor)
    out_dim_0 = int(indices.shape[0])
    target_d0 = int(matrix_target.shape[0])
    if out_dim_0 == target_d0:
        pass
    else:
        indices = indices[:target_d0]
    subsampled_matrix = matrix_original[indices, :]
    return subsampled_matrix.data
def _subsample_embeddings_dim1(matrix_original, matrix_target, reduction_factor):
    indices = torch.arange(0, matrix_original.size(1), reduction_factor)
    out_dim_1 = int(indices.shape[0])
    target_d1 = int(matrix_target.shape[1])
    if out_dim_1 == target_d1:
        pass
    else:
        indices = indices[:target_d1]
    subsampled_matrix = matrix_original[:, indices]
    return subsampled_matrix.data
def _subsample_embeddings_dimlast(matrix_original, matrix_target, reduction_factor):
    device = matrix_target.get_device() 
    indices = torch.arange(0, matrix_original.size(-1), reduction_factor).to(device)
    #indices = torch.arange(0, matrix_original.size(-1), reduction_factor)
    out_dim_1 = int(indices.shape[0])
    target_d1 = int(matrix_target.shape[-1])
    if out_dim_1 == target_d1:
       pass
    else:
        indices = indices[:target_d1].to(device)
    #subsampled_matrix = matrix_original[:, :, indices]
    #matrix_original = matrix_original.to(device)
    subsampled_matrix = matrix_original.to(device).index_select(dim=-1, index=indices)
    # if subsampled_matrix.shape == matrix_target.shape:
    #     pass
    # else:
    #     if torch.numel(subsampled_matrix.shape) == torch.numel(matrix_target.shape):
    #         subsampled_matrix = subsampled_matrix.squeeze() 
    #     else:
    #         raise ValueError(f"Error file: distill_llm.py, Invalid number: line 215+-")
    return subsampled_matrix.data


# make the smaller model 
class SmallerModel:
    def __init__(self, parser):
        self.llm = parser.llm
        self.max_tokens = parser.max_tokens
        self.grad = 64
        self.step = 0
        self.learning_rate = parser.learning_rate
        self.weight_decay = parser.weight_decay
        self.batch_size = parser.batch_size
        self.cache_dir = parser.cache_dir
        self.cpus = parser.cpus
        #self.device = parser.device
        self.target_dir = parser.target_dir
        self.revision = parser.revision
        self.distill_model_config = parser.distill_model_config
        self.reduction_factor = parser.reduction_factor
        # self.model = AutoModelForCausalLM.from_pretrained(
        #     self.llm,
        #     revision = self.revision,
        #     cache_dir = self.cache_dir,
        #     trust_remote_code=True
        # )


        self.model_config = AutoConfig.from_pretrained(self.llm, trust_remote_code=True)
        # self.model_config.rotary_dim = int(self.model_config.rotary_dim / self.reduction_factor)
        self.model_config.n_embd = int(self.model_config.n_embd / self.reduction_factor)
        self.model_config.n_inner = int(self.model_config.n_inner / self.reduction_factor)

        self.model = AutoModelForCausalLM.from_config(
            self.model_config,
            trust_remote_code=True
        )

        model_copy = copy.deepcopy(self.model)


        
        # downsampling the weights
        self.reduce() # ask --> cannot pre-define the framework config

        # print("======")
        # print(model_copy.transformer.wte.weight.data[0][:10])
        # print(self.model.transformer.wte.weight.data[0][:10])
        # print("======")

        # for (name_original, param_original), (name, param) in zip(model_copy.named_parameters(), self.model.named_parameters()): 
        #     print(name_original, torch.equal(param_original, param)) 
        # exit()
        

    def reduce(self):
        # # Create a copy of the state_dict for modifications

        model_original = AutoModelForCausalLM.from_pretrained(
            self.llm,
            revision = self.revision,
            cache_dir = self.cache_dir,
            trust_remote_code=True
        )
        # If there are 10 param, could I use index to assign orinnt out the specific param instead of use for loop?
        state_dict = self.model.state_dict()
        for (name_original, param_original), (name, param) in zip(model_original.named_parameters(), self.model.named_parameters()):
            if param.dim() == 2:
                # 2D weight matrices
                if param.size(0) == param.size(1):
                    # Subsample and scale square matrices
                    #param.data = self._subsample_and_scale(param_original, param)
                    #state_dict[name] = _subsample_and_scale(param_original, param, self.reduction_factor)
                    param.data = _subsample_and_scale(param_original, param, self.reduction_factor)
                else:
                    # Handle rectangular matrices by subsampling only the larger dimension
                    if "wte" in name:
                        #param.data = self._subsample_embeddings_dim1(param_original, param)
                        #self.model.state_dict[name] = _subsample_embeddings_dim1(param_original, param, self.reduction_factor)
                        param.data = _subsample_embeddings_dim1(param_original, param, self.reduction_factor)
                    else:
                        #param.data = self._subsample_embeddings_dim(param_original, param)
                        #self.model.state_dict[name] = _subsample_embeddings_dim(param_original, param, self.reduction_factor)
                        param.data = _subsample_embeddings_dim(param_original, param, self.reduction_factor)
            else:
                # embedding, bias, .... (1D)
                #param.data = self._subsample_embeddings(param_original, param)
                #state_dict[name] = _subsample_embeddings(param_original, param, self.reduction_factor)
                param.data = _subsample_embeddings(param_original, param, self.reduction_factor)
        
        del model_original

    def forward(self, x, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        return z
    
    def hidden(self, x, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        return z.hidden_states

    def next_token_prediction_loss(self, x, y):
        z = self.model(x).logits
        y = y.reshape(-1)
        z = z.view(-1, z.shape[-1])
        loss = F.cross_entropy(z, y)
        #threshold = 10
        #clipped_loss = torch.clamp(loss, min=None, max=threshold)
        #print(clipped_loss)
        #return clipped_loss
        return loss

    def hidden_and_loss(self, x, y, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        y = y.reshape(-1)
        y_prime = z.logits.view(-1, z.logits.shape[-1])
        loss = F.cross_entropy(y_prime, y)
        return z, loss

    def logit_and_loss(self, x, y, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        y = y.reshape(-1)
        y_prime = z.logits.view(-1, z.logits.shape[-1])
        loss = F.cross_entropy(y_prime, y)
        return z.logits, loss

    
    def logit_and_hidden_and_loss(self, x, y, output_hidden_states):
        #x = x.to(self.model.device)
        z = self.model(x, output_hidden_states=output_hidden_states)
        y = y.reshape(-1)
        y_prime = z.logits.view(-1, z.logits.shape[-1])
        loss = F.cross_entropy(y_prime, y)
        return z.logits, z.hidden_states, loss


class Distiller:
    #def __init__(self, parser, larger_model, smaller_model, rank):
    def __init__(self, parser, larger_model, smaller_model):

        #self.max_tokens = 2**13
        self.llm = parser.llm
        self.max_tokens = parser.max_tokens
        self.grad_step = parser.grad_step
        self.step = 0
        self.learning_rate = parser.learning_rate
        self.weight_decay = parser.weight_decay
        self.batch_size = parser.batch_size
        self.cache_dir = parser.cache_dir
        self.cpus = parser.cpus
        #self.device = parser.device
        self.target_dir = parser.target_dir
        self.revision = parser.revision
        self.distill_model_config = parser.distill_model_config

        self.dataset = DatasetWrapper(self.max_tokens, self.cache_dir)

        self.larger_model = larger_model
        self.smaller_model = smaller_model
        self.tokenizer = self.dataset.tokenizer
        self.loader = DataLoader(
            self.dataset,
            batch_size=self.batch_size,
            num_workers=self.cpus,
        )
        self.eval_max_batch = math.ceil(self.loader.dataset.__eval_size__()/self.batch_size)
        self.train_max_batch = math.ceil(self.loader.dataset.__train_size__()/self.batch_size)

        #Save hook inf.
        self.smaller_hook_forward_dict = {}
        self.smaller_hook_backward_dict = {}
        self.larger_hook_forward_dict = {}
        self.larger_hook_backward_dict = {}

        self.larger_forward_hook_list = []
        self.smaller_forward_hook_list = []
        self.larger_backward_hook_list = []
        self.smaller_backward_hook_list = []
        #config = AutoConfig.from_pretrained('LLM360/CrystalCoder', trust_remote_code=True)
        #config.save_pretrained('../distill-crystalcoder-config')

        #loss
        self.smaller_backward_loss = 0
        self.smaller_forward_loss = 0

        self.training_config_dir = parser.training_config_dir 
        #self.rank = rank



        '''
        self.opt = bnb.optim.Lion(
            params=self.smaller_model.parameters(),
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
            betas=(0.9, 0.95),
            optim_bits=8,
            #fused=True,
        )
        '''

        self.opt = optim.AdamW(self.smaller_model.model.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay)

        ###
        '''
        - device_placement (default: True): Automatically place the model and data on the right device (CPU or GPU).
        - split_batches (default: False): Automatically split batches between devices in data parallelism.
        - fp16 (default: False): Use automatic mixed precision training (floating-point 16). This is a simple way to use mixed precision without the need to configure it manually.
        - cpu (default: False): Force the use of CPU even if GPUs are available. Useful for debugging or when GPU resources are not desired.
        - deepspeed_plugin (default: None): Configuration for using DeepSpeed with Accelerator, which allows for efficient training on multiple GPUs, achieving high performance and reduced memory usage.
        - mixed_precision (default: "no"): Set the mixed precision policy. Options are "no", "fp16", and "bf16". This specifies whether to use mixed precision and which type. "fp16" and "bf16" refer to half-precision and bfloat16 precision, respectively.
        - _custom_ddp_plugin (not typically used by end users): Allows for a custom Distributed Data Parallel (DDP) plugin. This is more advanced usage for custom distributed training setups.
        - log_with (default: ["tqdm"]): Choose the libraries to use for logging progress. By default, it uses tqdm, but other loggers can be configured.
        - logging_dir (default: None): The directory to save logs to if you're using a logger that writes to files.
        - dispatch_batches (default: False): This argument is for internal use, concerning how batches are distributed across devices.
        - zero3 (default: False): Enable ZeRO Stage 3 optimization with DeepSpeed, which dramatically reduces memory usage at scale.
        - cpu_offload (default: False): Whether to offload parts of the model or computations to the CPU, usually in combination with DeepSpeed to save GPU memory.
        - gradient_accumulation_steps (default: 1): Number of steps to accumulate gradients before updating model parameters, which can be useful for effectively increasing the batch size without increasing the memory consumption.
        '''
        ###


        training_config = self.load_and_filter_config(self.training_config_dir)

        
        #training_plugin = FullyShardedDataParallelPlugin(**training_config)
         
        # # I can change to fsdp
        # self.accelerator = Accelerator(
        #     gradient_accumulation_steps=self.grad_step,
        #     #mixed_precision = 'fp8',
        #     mixed_precision = 'bf16',
        #     fsdp_plugin = training_config.get('fsdp_plugin'),
        #     #megatron_lm_plugin = ,
        #     #deepspeed_plugin = ,
        # )

        #print(self.grad_step)
        #exit()
        
        self.accelerator = Accelerator(
            fsdp_plugin = training_config.get('fsdp_plugin'),
            mixed_precision = training_config.get('mixed_precision'),
            gradient_accumulation_steps = self.grad_step,
        )
        
        # self.accelerator = Accelerator(training_config)
        
        #print("Number of GPUs:", self.accelerator.state.num_processes)
        #exit()

        #print(self.accelerator)
        
        #import pdb; pdb.set_trace()
        
        self.device = self.accelerator.device

         
        #self.is_local_main_process = self.accelerator.is_local_main_process 

        #self.smaller_model.model = self.smaller_model.model.to(self.device)
        #self.larger_model.model = self.larger_model.model.to(self.device) 

        '''
        self.larger_model.model, self.smaller_model.model, self.opt, self.loader= self.accelerator.prepare(
            self.larger_model.model, self.smaller_model.model, self.opt, self.loader
        )
        '''
        
        ##### Fix the schduler here !!!!!
        # lr_scheduler = get_scheduler(
        #     "linear",
        #     optimizer=optimizer,
        #     num_warmup_steps=0,
        #     num_training_steps=num_training_steps
        # )
        
        self.larger_model, self.smaller_model, self.opt, self.loader= self.accelerator.prepare(
            self.larger_model, self.smaller_model, self.opt, self.loader
        )

        #### Show paprameters:
        self.show_params(self.larger_model.model)
        self.show_params(self.smaller_model.model)

    def load_and_filter_config(self, training_config_dir):

        with open(training_config_dir, 'r') as training_config_file:
            training_config = yaml.safe_load(training_config_file)
            
        if training_config.get('distributed_type') == 'FSDP':
            default_fsdp_parameters = inspect.signature(FullyShardedDataParallelPlugin.__init__).parameters
            fsdp_args = {}
            for param_name, param in default_fsdp_parameters.items():
                config_key = f"fsdp_{param_name}"
                if config_key in training_config['fsdp_config']:
                    fsdp_args[param_name] = training_config['fsdp_config'][config_key]
                elif param.default is not inspect.Parameter.empty:
                    # If not assigned, use the default value
                    fsdp_args[param_name] = param.default
            fsdp_plugin = FullyShardedDataParallelPlugin(**fsdp_args)
            del training_config["fsdp_config"]
            training_config["fsdp_plugin"] = fsdp_plugin
        else:
            raise ValueError(f"Invalid distributed type: Set the corresponding config processing: around Line 530")
        
        return training_config 
        

    def show_params(self, model):
        '''
        CrystalCoderLMHeadModel(
        (transformer): CrystalCoderModel(
            (wte): Embedding(32032, 4096)
            (drop): Dropout(p=0.0, inplace=False)
            (h): ModuleList(
                (0-31): 32 x CrystalCoderBlock(
                    (ln_1): LayerNorm((4096,), eps=1e-05, elementwise_affine=True)
                    (attn): CrystalCoderAttention(
                        (c_attn): Conv1D()
                        (c_proj): Conv1D()
                        (attn_dropout): Dropout(p=0.0, inplace=False)
                        (resid_dropout): Dropout(p=0.0, inplace=False)
                    )
                    (ln_2): LayerNorm((4096,), eps=1e-05, elementwise_affine=True)
                    (mlp): CrystalCoderMLP(
                        (c_fc): Conv1D()
                        (c_fc2): Conv1D()
                        (c_proj): Conv1D()
                        (act): SwiGLUActivation()
                        (dropout): Dropout(p=0.0, inplace=False)
                    )
                )
            )
            (ln_f): LayerNorm((4096,), eps=1e-05, elementwise_affine=True)
        )
        (lm_head): Linear(in_features=4096, out_features=32032, bias=False)
        )
        '''


        #if self.rank == 0:
        #if self.is_local_main_process:
        if self.accelerator.is_local_main_process:
            print()
            print("=============Model Para================")
            params = sum(p.numel() for p in model.parameters() if p.requires_grad)
            emb_params = list(model.transformer.wte.parameters())
            emb_params += list(model.lm_head.parameters())
            emb_params = sum(p.numel() for p in emb_params if p.requires_grad)
            trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
            print("Params:", params - emb_params)
            print("Params (incl. embeddings):", params)
            print("Trainable params:", trainable_params)
            print("========================================")

        '''
        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=lm_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
            cross_attentions=transformer_outputs.cross_attentions,
        )
        '''

    def compute_mean_loss(self, large_hidden_states, small_hidden_states):
        # print(large_hidden_states.shape) #torch.Size([layers of the model, batch_size, seq_length, output_dim])
        # print(small_hidden_states.shape) #torch.Size([layers of the model, batch_size, seq_length, output_dim])
        mean_large = large_hidden_states.mean(dim=(1, 2, 3))
        mean_small = small_hidden_states.mean(dim=(1, 2, 3))
        #loss = nn.MSELoss(reduction="sum")(mean_large, mean_small)
        loss = self.calculate_loss(mean_large, mean_small)
        return loss


    def compute_std_loss(self, large_hidden_states, small_hidden_states):
        # Small constant to prevent division by zero in std computation
        epsilon = 1e-6
        #epsilon = 0
        std_large = large_hidden_states.std(dim=(1, 2, 3)) #+ epsilon
        std_small = small_hidden_states.std(dim=(1, 2, 3)) #+ epsilon
        #torch.clamp: prevent std becomes 0 
        std_large = torch.clamp(std_large, min=epsilon)
        std_small = torch.clamp(std_small, min=epsilon)
        #kl_div = kl_divergence(std_large, std_small).sum()
        #loss = nn.MSELoss(reduction="sum")(std_large, std_small)
        loss = self.calculate_loss(std_large, std_small)
        return loss


    def layerwise_hidden_loss(self, output_large, output_small):
        #large_hidden_states = output_large.hidden_states
        #large_hidden_states = torch.stack(large_hidden_states)
        large_hidden_states = torch.stack(output_large)
        #small_hidden_states = output_small.hidden_states
        #small_hidden_states = torch.stack(small_hidden_states)
        small_hidden_states = torch.stack(output_small)

        mean_loss = self.compute_mean_loss(large_hidden_states, small_hidden_states)
        std_loss = self.compute_std_loss(large_hidden_states, small_hidden_states)
        #print(f"mean_loss: {mean_loss}, std_loss: {std_loss}")
        loss = mean_loss + std_loss
        #loss = self.calculate_loss(large_hidden_states, small_hidden_states)
        return loss

    def logits_loss(self, large_logit, small_logit):
        #large_logits = output_large.logits
        #small_logits = output_small.logits
        loss = self.calculate_loss(large_logit, small_logit)
        return loss

    def calculate_loss(self, y_prime, y):
        threshold = 256  
        loss = nn.MSELoss()(y_prime, y)
        
        if torch.isinf(loss):
            return torch.tensor(threshold, dtype=loss.dtype, device=loss.device)
        # loss = torch.where(base_loss > threshold, 
        #                 threshold + torch.log1p(base_loss - threshold), 
        #                 base_loss) 
        return loss
   
    
    '''
    transformer.wte.weight torch.Size([32032, 1024])
    transformer.h.0.ln_1.weight torch.Size([1024])
    transformer.h.0.ln_1.bias torch.Size([1024])
    transformer.h.0.attn.c_attn.weight torch.Size([1024, 3072])
    transformer.h.0.attn.c_attn.bias torch.Size([3072])
    transformer.h.0.attn.c_proj.weight torch.Size([1024, 1024])
    transformer.h.0.attn.c_proj.bias torch.Size([1024])
    transformer.h.0.ln_2.weight torch.Size([1024])
    transformer.h.0.ln_2.bias torch.Size([1024])
    transformer.h.0.mlp.c_fc.weight torch.Size([1024, 2730])
    transformer.h.0.mlp.c_fc.bias torch.Size([2730])
    transformer.h.0.mlp.c_fc2.weight torch.Size([1024, 2730])
    transformer.h.0.mlp.c_fc2.bias torch.Size([2730])
    transformer.h.0.mlp.c_proj.weight torch.Size([2730, 1024])
    transformer.h.0.mlp.c_proj.bias torch.Size([1024])

    transformer.wte 1
    transformer.drop 1
    transformer.h.0.ln_1 1
    transformer.h.0.attn.c_attn 1
    transformer.h.0.attn.attn_dropout 1
    transformer.h.0.attn.c_proj 1
    transformer.h.0.attn.resid_dropout 1
    transformer.h.0.attn 2
    transformer.h.0.ln_2 1
    transformer.h.0.mlp.c_fc2 1
    transformer.h.0.mlp.c_fc 1
    transformer.h.0.mlp.act 1
    transformer.h.0.mlp.c_proj 1
    transformer.h.0.mlp.dropout 1
    transformer.h.0.mlp 1
    transformer.h.0 2
    ''' 
    

    def remove_hook(self, hook_list):
        for hook in hook_list:
            hook.remove()
        hook_list.clear()

    
    def set_requires_grad(self, model, value):
        for name, param in model.named_parameters():
            param.requires_grad = value
    
    def forward_pre_hook(self, module_name, model_name, is_modifiy):
        
        if model_name == "larger": 
            def f_hook(module, input):
                pattern = r"transformer\.h\.\d{1,2}$"
                if re.match(pattern, module_name):
                    self.larger_hook_forward_dict[module_name] = input
                return input
            return f_hook 
        
        elif model_name == "smaller":
            if is_modifiy == True: 
                def f_hook(module, input):
                    pattern = r"transformer\.h\.\d{1,2}$"
                    if re.match(pattern, module_name):
                        # downsample
                        modified_input = self.larger_hook_forward_dict[module_name][0]
                        input[0].data = _subsample_embeddings_dimlast(modified_input, input[0], self.smaller_model.reduction_factor)  
                    return input 
                return f_hook 
            else:
                def f_hook(module, input):
                    return input 
                return f_hook 
        
        
        
    def register_pre_forward_hook(self, model, model_name, is_modifiy):
        total_hook_list = []
        for module_name, module in model.named_modules():
            hook = module.register_forward_pre_hook(self.forward_pre_hook(module_name, model_name, is_modifiy))
            total_hook_list.append(hook)
        if model_name == "smaller":
            self.smaller_forward_hook_list = total_hook_list
        elif model_name == "larger":
            self.larger_forward_hook_list = total_hook_list
        else:
            raise ValueError(f"Error file: distill_llm.py, Invalid number: line 567+-")
         

    def get_grad_norm_per_parameters(self, model):
        grad_norms = {}
        for name, param in model.named_parameters():
            if param.grad is not None:
                grad_norms[name] = torch.norm(param.grad.detach(), 2).item()
        return grad_norms

    def get_avg_grad_norm_per_module(self, model):
        grad_norms = defaultdict(list)
        for name, param in model.named_parameters():
            if param.grad is not None:
                #layer_name = name.split('.')[2]
                layer_name = name
                grad_norms[layer_name].append(torch.norm(param.grad.detach(), 2).item())
        avg_grad_norms = {layer: sum(norms) / len(norms) for layer, norms in grad_norms.items()}
        return avg_grad_norms


    def get_grad_norm_per_layer(self, model):
        grad_norms = {}
        for name, module in model.named_modules():
            if isinstance(module, nn.TransformerEncoderLayer) or isinstance(module, nn.TransformerDecoderLayer):
                layer_grad_norm = 0
                for param in module.parameters():
                    if param.grad is not None:
                        layer_grad_norm += torch.norm(param.grad.detach(), 2).item() ** 2
                grad_norms[name] = math.sqrt(layer_grad_norm)
        return grad_norms


    def get_avg_grad_norm_per_layer(self, model):
        grad_norms = {}
        pattern = r"^transformer\.h\.\d{1,2}(?:\..+)?$"
        for name, module in model.named_modules():
            layer_grad_norm = 0
            layer_name = ""
            # print(name)
            if name == "transformer.wte": 
                layer_name = name
            elif re.match(pattern, name):
                layer_name = '.'.join(name.split('.')[:3])
            else:
                continue
            for param in module.parameters():
                if param.grad is not None:
                    #layer_grad_norm += torch.norm(param.grad.detach(), 2).item() ** 2
                    if layer_name not in grad_norms:
                        grad_norms[layer_name] = [torch.norm(param.grad.detach(), 2).item()]
                    else:
                        grad_norms[layer_name].append(torch.norm(param.grad.detach(), 2).item())
        avg_grad_norms = {layer: sum(norms) / len(norms) for layer, norms in grad_norms.items()}
        return avg_grad_norms
     
         
    def distill(self):
        #Currently logged in as: yusheng-su (mbzuai-llm). Use `wandb login --relogin` to force relogin
        if self.accelerator.is_local_main_process:
            target_log = "../log/distill_loss"
            if os.path.isdir(target_log+"/wandb"):
                # delete dir
                shutil.rmtree(target_log+"/wandb")
            #create a new one
            os.makedirs(target_log+"/wandb")
            
            wandb.init(
                project=PROJECT,
                entity=ENTITY,
                #notes=socket.gethostname(),
                name="training_log",
                dir=target_log,
                job_type="training",
                reinit=True
            )


        if self.accelerator.is_local_main_process:
            prog = tqdm(self.loader, bar_format="{l_bar}{bar:10}{r_bar}{bar:-10b}")
        else:
            prog = self.loader 

        total_loss = 0

        max_i = 0
        stop_batch = self.train_max_batch
        accumulated_loss = 0.0

        self.larger_model.model.to(self.device)
        self.smaller_model.model.to(self.device)
        self.larger_model.model.eval()
        self.smaller_model.model.train()

        if self.accelerator.is_local_main_process:
            print()
            print("lr:{}".format(self.learning_rate))
        #loss_1 = 0
        #loss_2 = 0


        total_loss = 0
        for i, batch in enumerate(prog):

            # if self.accelerator.is_local_main_process:
            #     torch.cuda.empty_cache()
            #     print(f"2nd: {torch.cuda.memory_summary(device=None, abbreviated=False)}")

            
            with self.accelerator.accumulate(self.smaller_model):
                loss = 0
                
                self.step = i + 1
                batch = batch.to(self.device)
                x, y = batch[:, :-1], batch[:, 1:]
                #output_large = self.larger_model.forward(x)
                
                
                ###start from here: 
                with torch.no_grad():
                    # Since we do not require gradient calculations or parameter updates for self.larger_model,
                    # operations are wrapped in torch.no_grad() to improve performance and reduce memory usage.
                    
                    ## Collect larger_llm's hidden states
                    self.register_pre_forward_hook(self.larger_model.model, "larger", False) 
                    larger_logit, larger_autoregressive_loss = self.larger_model.logit_and_loss(x, y, True)
                    
                    ## Collect smaller_llm's hidden states (downsamples)
                    self.register_pre_forward_hook(self.smaller_model.model, "smaller", True) 
                    smaller_hidden_state_downsampled = self.smaller_model.hidden(x, True)
                    self.remove_hook(self.larger_forward_hook_list)
                    self.remove_hook(self.smaller_forward_hook_list)
                    self.larger_hook_forward_dict.clear()
                    self.smaller_hook_forward_dict.clear()
             
                #### forward layer loss
                smaller_logit, smaller_hidden_state, smaller_autoregressive_loss = self.smaller_model.logit_and_hidden_and_loss(x, y, True)
                #### layer-wise loss
                layerwise_hidden_loss = self.layerwise_hidden_loss(smaller_hidden_state, smaller_hidden_state_downsampled)
                #### logits loss
                logits_loss = self.logits_loss(larger_logit, smaller_logit)
                loss = smaller_autoregressive_loss + logits_loss + layerwise_hidden_loss/100 

                self.accelerator.backward(loss)

                
                # Show training log: 
                # If I set sync_gradients == True: I did not need self.accelerator.sync_gradients here (but the training would be slower)
                if self.accelerator.sync_gradients:
                    #accelerator.clip_grad_norm_(model.parameters(), max_norm=1.0) 
                    if self.accelerator.is_local_main_process:
                        prog.set_description(f"current loss: {loss.item():.3f}")
                        #prog.set_description(f"average total_loss: {total_loss.item()/self.step:.3f}")
                        loss_dict =  {
                                "smaller_autoregressive_loss": smaller_autoregressive_loss.item(),
                                "logits_loss": logits_loss.item(),
                                "layerwise_hidden_loss": layerwise_hidden_loss.item(),
                                "current loss": loss.item(),
                        }
                        avg_grad_norms = self.get_avg_grad_norm_per_layer(self.smaller_model.model)
                        layer_names = {f"layer_{i}": layer for i, (layer, _) in enumerate(avg_grad_norms.items(), start=1)}
                        grad_norm_dict = {f"grad_norms/{layer}": norm for layer, norm in avg_grad_norms.items()}
                        wandb.log({**loss_dict, **grad_norm_dict}, step=i)
                    
                # Update the llm 
                self.opt.step()
                #lr_scheduler.step()
                self.opt.zero_grad()


                # Save the checkpoint
                #if self.step % 1 == 0 and self.accelerator.is_main_process:
                if self.step % 5000 == 0:
                    self.accelerator.wait_for_everyone()
                    unwrapped_model = self.accelerator.unwrap_model(self.smaller_model.model)
                    checkpoint = {
                        'global_step': self.step,
                        'model_state_dict': unwrapped_model.state_dict(),
                        'optimizer_state_dict': self.opt.state_dict(),
                    }
                    save_directory = self.target_dir + "/distill_training"
                    if not os.path.exists(save_directory):
                        os.makedirs(save_directory)
                    save_path = os.path.join(save_directory, f'checkpoint_step_{self.step}.pt')
                    self.accelerator.save(checkpoint, save_path)
                    print(f"Saved checkpoint at step {self.step}")
                


        self.accelerator.wait_for_everyone()
        unwrapped_model = self.accelerator.unwrap_model(self.smaller_model.model)
        checkpoint = {
            'global_step': self.step,
            'model_state_dict': unwrapped_model.state_dict(),
            'optimizer_state_dict': self.opt.state_dict(),
        }
        save_directory = self.target_dir + "/distill_training"
        if not os.path.exists(save_directory):
            os.makedirs(save_directory)
        save_path = os.path.join(save_directory, f'checkpoint_step_{self.step}.pt')
        self.accelerator.save(checkpoint, save_path)
        print(f"Saved checkpoint at step {self.step}")
    



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--llm', type=str, default="LLM360/CrystalCoder", help='llm')
    parser.add_argument('--max_tokens', type=int, default=1024, help='max_length')
    parser.add_argument('--learning_rate', type=float, default=3e-5, help='learning_rate')
    parser.add_argument('--weight_decay', type=float, default=0, help='weight_decay')
    parser.add_argument('--batch_size', type=int, default=16, help='batch_size')
    parser.add_argument('--target_dir', type=str, default=os.getcwd()+"/../checkpoint/LLM360/CrystalCoder/CrystalCoder_phase1_checkpoint_055500", help='target_dir')
    parser.add_argument('--cache_dir', type=str, default=os.getcwd()+"/../cache", help='cache_dir')
    parser.add_argument('--cpus', type=int, default = cpu_count(), help='cpus')
    parser.add_argument('--device', type=str, default = "cuda", help='device')
    parser.add_argument('--revision', type=str, default = "CrystalCoder_phase1_checkpoint_055500", help='revision')
    parser.add_argument('--distill_model_config', type=str, default = "", help='distill_model_config')
    parser.add_argument('--grad_step', type=int, default=64, help='grad steps')
    parser.add_argument('--reduction_factor', type=int, default=4, help='reduction_factor')
    parser.add_argument('--training_config_dir', type=str, default=os.getcwd()+"/../config/default_config.yaml", help='training_config')


    args = parser.parse_args()
    #args.checkpoint = os.getcwd()+"/../checkpoint/" + args.checkpoint
    
    #os.environ["ACCELERATE_CONFIG_FILE"] = args.training_config_dir

    smaller_model = SmallerModel(args)
    larger_model = LargerModel(args)
    #distiller = Distiller(args, larger_model, smaller_model, rank)
    distiller = Distiller(args, larger_model, smaller_model)
    distiller.distill()


if __name__ == "__main__":
    main()


# 1.75 B




# y-bias

'''
transformer.wte.weight torch.Size([32032, 1024])
transformer.h.0.ln_1.weight torch.Size([1024])
transformer.h.0.ln_1.bias torch.Size([1024])
transformer.h.0.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.0.attn.c_attn.bias torch.Size([3072])
transformer.h.0.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.0.attn.c_proj.bias torch.Size([1024])
transformer.h.0.ln_2.weight torch.Size([1024])
transformer.h.0.ln_2.bias torch.Size([1024])
transformer.h.0.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.0.mlp.c_fc.bias torch.Size([2730])
transformer.h.0.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.0.mlp.c_fc2.bias torch.Size([2730])
transformer.h.0.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.0.mlp.c_proj.bias torch.Size([1024])
transformer.h.1.ln_1.weight torch.Size([1024])
transformer.h.1.ln_1.bias torch.Size([1024])
transformer.h.1.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.1.attn.c_attn.bias torch.Size([3072])
transformer.h.1.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.1.attn.c_proj.bias torch.Size([1024])
transformer.h.1.ln_2.weight torch.Size([1024])
transformer.h.1.ln_2.bias torch.Size([1024])
transformer.h.1.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.1.mlp.c_fc.bias torch.Size([2730])
transformer.h.1.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.1.mlp.c_fc2.bias torch.Size([2730])
transformer.h.1.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.1.mlp.c_proj.bias torch.Size([1024])
transformer.h.2.ln_1.weight torch.Size([1024])
transformer.h.2.ln_1.bias torch.Size([1024])
transformer.h.2.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.2.attn.c_attn.bias torch.Size([3072])
transformer.h.2.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.2.attn.c_proj.bias torch.Size([1024])
transformer.h.2.ln_2.weight torch.Size([1024])
transformer.h.2.ln_2.bias torch.Size([1024])
transformer.h.2.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.2.mlp.c_fc.bias torch.Size([2730])
transformer.h.2.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.2.mlp.c_fc2.bias torch.Size([2730])
transformer.h.2.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.2.mlp.c_proj.bias torch.Size([1024])
transformer.h.3.ln_1.weight torch.Size([1024])
transformer.h.3.ln_1.bias torch.Size([1024])
transformer.h.3.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.3.attn.c_attn.bias torch.Size([3072])
transformer.h.3.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.3.attn.c_proj.bias torch.Size([1024])
transformer.h.3.ln_2.weight torch.Size([1024])
transformer.h.3.ln_2.bias torch.Size([1024])
transformer.h.3.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.3.mlp.c_fc.bias torch.Size([2730])
transformer.h.3.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.3.mlp.c_fc2.bias torch.Size([2730])
transformer.h.3.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.3.mlp.c_proj.bias torch.Size([1024])
transformer.h.4.ln_1.weight torch.Size([1024])
transformer.h.4.ln_1.bias torch.Size([1024])
transformer.h.4.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.4.attn.c_attn.bias torch.Size([3072])
transformer.h.4.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.4.attn.c_proj.bias torch.Size([1024])
transformer.h.4.ln_2.weight torch.Size([1024])
transformer.h.4.ln_2.bias torch.Size([1024])
transformer.h.4.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.4.mlp.c_fc.bias torch.Size([2730])
transformer.h.4.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.4.mlp.c_fc2.bias torch.Size([2730])
transformer.h.4.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.4.mlp.c_proj.bias torch.Size([1024])
transformer.h.5.ln_1.weight torch.Size([1024])
transformer.h.5.ln_1.bias torch.Size([1024])
transformer.h.5.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.5.attn.c_attn.bias torch.Size([3072])
transformer.h.5.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.5.attn.c_proj.bias torch.Size([1024])
transformer.h.5.ln_2.weight torch.Size([1024])
transformer.h.5.ln_2.bias torch.Size([1024])
transformer.h.5.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.5.mlp.c_fc.bias torch.Size([2730])
transformer.h.5.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.5.mlp.c_fc2.bias torch.Size([2730])
transformer.h.5.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.5.mlp.c_proj.bias torch.Size([1024])
transformer.h.6.ln_1.weight torch.Size([1024])
transformer.h.6.ln_1.bias torch.Size([1024])
transformer.h.6.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.6.attn.c_attn.bias torch.Size([3072])
transformer.h.6.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.6.attn.c_proj.bias torch.Size([1024])
transformer.h.6.ln_2.weight torch.Size([1024])
transformer.h.6.ln_2.bias torch.Size([1024])
transformer.h.6.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.6.mlp.c_fc.bias torch.Size([2730])
transformer.h.6.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.6.mlp.c_fc2.bias torch.Size([2730])
transformer.h.6.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.6.mlp.c_proj.bias torch.Size([1024])
transformer.h.7.ln_1.weight torch.Size([1024])
transformer.h.7.ln_1.bias torch.Size([1024])
transformer.h.7.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.7.attn.c_attn.bias torch.Size([3072])
transformer.h.7.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.7.attn.c_proj.bias torch.Size([1024])
transformer.h.7.ln_2.weight torch.Size([1024])
transformer.h.7.ln_2.bias torch.Size([1024])
transformer.h.7.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.7.mlp.c_fc.bias torch.Size([2730])
transformer.h.7.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.7.mlp.c_fc2.bias torch.Size([2730])
transformer.h.7.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.7.mlp.c_proj.bias torch.Size([1024])
transformer.h.8.ln_1.weight torch.Size([1024])
transformer.h.8.ln_1.bias torch.Size([1024])
transformer.h.8.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.8.attn.c_attn.bias torch.Size([3072])
transformer.h.8.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.8.attn.c_proj.bias torch.Size([1024])
transformer.h.8.ln_2.weight torch.Size([1024])
transformer.h.8.ln_2.bias torch.Size([1024])
transformer.h.8.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.8.mlp.c_fc.bias torch.Size([2730])
transformer.h.8.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.8.mlp.c_fc2.bias torch.Size([2730])
transformer.h.8.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.8.mlp.c_proj.bias torch.Size([1024])
transformer.h.9.ln_1.weight torch.Size([1024])
transformer.h.9.ln_1.bias torch.Size([1024])
transformer.h.9.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.9.attn.c_attn.bias torch.Size([3072])
transformer.h.9.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.9.attn.c_proj.bias torch.Size([1024])
transformer.h.9.ln_2.weight torch.Size([1024])
transformer.h.9.ln_2.bias torch.Size([1024])
transformer.h.9.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.9.mlp.c_fc.bias torch.Size([2730])
transformer.h.9.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.9.mlp.c_fc2.bias torch.Size([2730])
transformer.h.9.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.9.mlp.c_proj.bias torch.Size([1024])
transformer.h.10.ln_1.weight torch.Size([1024])
transformer.h.10.ln_1.bias torch.Size([1024])
transformer.h.10.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.10.attn.c_attn.bias torch.Size([3072])
transformer.h.10.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.10.attn.c_proj.bias torch.Size([1024])
transformer.h.10.ln_2.weight torch.Size([1024])
transformer.h.10.ln_2.bias torch.Size([1024])
transformer.h.10.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.10.mlp.c_fc.bias torch.Size([2730])
transformer.h.10.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.10.mlp.c_fc2.bias torch.Size([2730])
transformer.h.10.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.10.mlp.c_proj.bias torch.Size([1024])
transformer.h.11.ln_1.weight torch.Size([1024])
transformer.h.11.ln_1.bias torch.Size([1024])
transformer.h.11.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.11.attn.c_attn.bias torch.Size([3072])
transformer.h.11.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.11.attn.c_proj.bias torch.Size([1024])
transformer.h.11.ln_2.weight torch.Size([1024])
transformer.h.11.ln_2.bias torch.Size([1024])
transformer.h.11.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.11.mlp.c_fc.bias torch.Size([2730])
transformer.h.11.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.11.mlp.c_fc2.bias torch.Size([2730])
transformer.h.11.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.11.mlp.c_proj.bias torch.Size([1024])
transformer.h.12.ln_1.weight torch.Size([1024])
transformer.h.12.ln_1.bias torch.Size([1024])
transformer.h.12.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.12.attn.c_attn.bias torch.Size([3072])
transformer.h.12.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.12.attn.c_proj.bias torch.Size([1024])
transformer.h.12.ln_2.weight torch.Size([1024])
transformer.h.12.ln_2.bias torch.Size([1024])
transformer.h.12.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.12.mlp.c_fc.bias torch.Size([2730])
transformer.h.12.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.12.mlp.c_fc2.bias torch.Size([2730])
transformer.h.12.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.12.mlp.c_proj.bias torch.Size([1024])
transformer.h.13.ln_1.weight torch.Size([1024])
transformer.h.13.ln_1.bias torch.Size([1024])
transformer.h.13.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.13.attn.c_attn.bias torch.Size([3072])
transformer.h.13.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.13.attn.c_proj.bias torch.Size([1024])
transformer.h.13.ln_2.weight torch.Size([1024])
transformer.h.13.ln_2.bias torch.Size([1024])
transformer.h.13.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.13.mlp.c_fc.bias torch.Size([2730])
transformer.h.13.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.13.mlp.c_fc2.bias torch.Size([2730])
transformer.h.13.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.13.mlp.c_proj.bias torch.Size([1024])
transformer.h.14.ln_1.weight torch.Size([1024])
transformer.h.14.ln_1.bias torch.Size([1024])
transformer.h.14.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.14.attn.c_attn.bias torch.Size([3072])
transformer.h.14.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.14.attn.c_proj.bias torch.Size([1024])
transformer.h.14.ln_2.weight torch.Size([1024])
transformer.h.14.ln_2.bias torch.Size([1024])
transformer.h.14.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.14.mlp.c_fc.bias torch.Size([2730])
transformer.h.14.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.14.mlp.c_fc2.bias torch.Size([2730])
transformer.h.14.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.14.mlp.c_proj.bias torch.Size([1024])
transformer.h.15.ln_1.weight torch.Size([1024])
transformer.h.15.ln_1.bias torch.Size([1024])
transformer.h.15.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.15.attn.c_attn.bias torch.Size([3072])
transformer.h.15.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.15.attn.c_proj.bias torch.Size([1024])
transformer.h.15.ln_2.weight torch.Size([1024])
transformer.h.15.ln_2.bias torch.Size([1024])
transformer.h.15.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.15.mlp.c_fc.bias torch.Size([2730])
transformer.h.15.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.15.mlp.c_fc2.bias torch.Size([2730])
transformer.h.15.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.15.mlp.c_proj.bias torch.Size([1024])
transformer.h.16.ln_1.weight torch.Size([1024])
transformer.h.16.ln_1.bias torch.Size([1024])
transformer.h.16.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.16.attn.c_attn.bias torch.Size([3072])
transformer.h.16.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.16.attn.c_proj.bias torch.Size([1024])
transformer.h.16.ln_2.weight torch.Size([1024])
transformer.h.16.ln_2.bias torch.Size([1024])
transformer.h.16.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.16.mlp.c_fc.bias torch.Size([2730])
transformer.h.16.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.16.mlp.c_fc2.bias torch.Size([2730])
transformer.h.16.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.16.mlp.c_proj.bias torch.Size([1024])
transformer.h.17.ln_1.weight torch.Size([1024])
transformer.h.17.ln_1.bias torch.Size([1024])
transformer.h.17.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.17.attn.c_attn.bias torch.Size([3072])
transformer.h.17.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.17.attn.c_proj.bias torch.Size([1024])
transformer.h.17.ln_2.weight torch.Size([1024])
transformer.h.17.ln_2.bias torch.Size([1024])
transformer.h.17.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.17.mlp.c_fc.bias torch.Size([2730])
transformer.h.17.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.17.mlp.c_fc2.bias torch.Size([2730])
transformer.h.17.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.17.mlp.c_proj.bias torch.Size([1024])
transformer.h.18.ln_1.weight torch.Size([1024])
transformer.h.18.ln_1.bias torch.Size([1024])
transformer.h.18.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.18.attn.c_attn.bias torch.Size([3072])
transformer.h.18.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.18.attn.c_proj.bias torch.Size([1024])
transformer.h.18.ln_2.weight torch.Size([1024])
transformer.h.18.ln_2.bias torch.Size([1024])
transformer.h.18.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.18.mlp.c_fc.bias torch.Size([2730])
transformer.h.18.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.18.mlp.c_fc2.bias torch.Size([2730])
transformer.h.18.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.18.mlp.c_proj.bias torch.Size([1024])
transformer.h.19.ln_1.weight torch.Size([1024])
transformer.h.19.ln_1.bias torch.Size([1024])
transformer.h.19.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.19.attn.c_attn.bias torch.Size([3072])
transformer.h.19.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.19.attn.c_proj.bias torch.Size([1024])
transformer.h.19.ln_2.weight torch.Size([1024])
transformer.h.19.ln_2.bias torch.Size([1024])
transformer.h.19.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.19.mlp.c_fc.bias torch.Size([2730])
transformer.h.19.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.19.mlp.c_fc2.bias torch.Size([2730])
transformer.h.19.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.19.mlp.c_proj.bias torch.Size([1024])
transformer.h.20.ln_1.weight torch.Size([1024])
transformer.h.20.ln_1.bias torch.Size([1024])
transformer.h.20.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.20.attn.c_attn.bias torch.Size([3072])
transformer.h.20.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.20.attn.c_proj.bias torch.Size([1024])
transformer.h.20.ln_2.weight torch.Size([1024])
transformer.h.20.ln_2.bias torch.Size([1024])
transformer.h.20.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.20.mlp.c_fc.bias torch.Size([2730])
transformer.h.20.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.20.mlp.c_fc2.bias torch.Size([2730])
transformer.h.20.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.20.mlp.c_proj.bias torch.Size([1024])
transformer.h.21.ln_1.weight torch.Size([1024])
transformer.h.21.ln_1.bias torch.Size([1024])
transformer.h.21.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.21.attn.c_attn.bias torch.Size([3072])
transformer.h.21.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.21.attn.c_proj.bias torch.Size([1024])
transformer.h.21.ln_2.weight torch.Size([1024])
transformer.h.21.ln_2.bias torch.Size([1024])
transformer.h.21.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.21.mlp.c_fc.bias torch.Size([2730])
transformer.h.21.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.21.mlp.c_fc2.bias torch.Size([2730])
transformer.h.21.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.21.mlp.c_proj.bias torch.Size([1024])
transformer.h.22.ln_1.weight torch.Size([1024])
transformer.h.22.ln_1.bias torch.Size([1024])
transformer.h.22.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.22.attn.c_attn.bias torch.Size([3072])
transformer.h.22.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.22.attn.c_proj.bias torch.Size([1024])
transformer.h.22.ln_2.weight torch.Size([1024])
transformer.h.22.ln_2.bias torch.Size([1024])
transformer.h.22.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.22.mlp.c_fc.bias torch.Size([2730])
transformer.h.22.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.22.mlp.c_fc2.bias torch.Size([2730])
transformer.h.22.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.22.mlp.c_proj.bias torch.Size([1024])
transformer.h.23.ln_1.weight torch.Size([1024])
transformer.h.23.ln_1.bias torch.Size([1024])
transformer.h.23.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.23.attn.c_attn.bias torch.Size([3072])
transformer.h.23.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.23.attn.c_proj.bias torch.Size([1024])
transformer.h.23.ln_2.weight torch.Size([1024])
transformer.h.23.ln_2.bias torch.Size([1024])
transformer.h.23.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.23.mlp.c_fc.bias torch.Size([2730])
transformer.h.23.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.23.mlp.c_fc2.bias torch.Size([2730])
transformer.h.23.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.23.mlp.c_proj.bias torch.Size([1024])
transformer.h.24.ln_1.weight torch.Size([1024])
transformer.h.24.ln_1.bias torch.Size([1024])
transformer.h.24.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.24.attn.c_attn.bias torch.Size([3072])
transformer.h.24.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.24.attn.c_proj.bias torch.Size([1024])
transformer.h.24.ln_2.weight torch.Size([1024])
transformer.h.24.ln_2.bias torch.Size([1024])
transformer.h.24.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.24.mlp.c_fc.bias torch.Size([2730])
transformer.h.24.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.24.mlp.c_fc2.bias torch.Size([2730])
transformer.h.24.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.24.mlp.c_proj.bias torch.Size([1024])
transformer.h.25.ln_1.weight torch.Size([1024])
transformer.h.25.ln_1.bias torch.Size([1024])
transformer.h.25.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.25.attn.c_attn.bias torch.Size([3072])
transformer.h.25.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.25.attn.c_proj.bias torch.Size([1024])
transformer.h.25.ln_2.weight torch.Size([1024])
transformer.h.25.ln_2.bias torch.Size([1024])
transformer.h.25.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.25.mlp.c_fc.bias torch.Size([2730])
transformer.h.25.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.25.mlp.c_fc2.bias torch.Size([2730])
transformer.h.25.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.25.mlp.c_proj.bias torch.Size([1024])
transformer.h.26.ln_1.weight torch.Size([1024])
transformer.h.26.ln_1.bias torch.Size([1024])
transformer.h.26.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.26.attn.c_attn.bias torch.Size([3072])
transformer.h.26.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.26.attn.c_proj.bias torch.Size([1024])
transformer.h.26.ln_2.weight torch.Size([1024])
transformer.h.26.ln_2.bias torch.Size([1024])
transformer.h.26.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.26.mlp.c_fc.bias torch.Size([2730])
transformer.h.26.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.26.mlp.c_fc2.bias torch.Size([2730])
transformer.h.26.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.26.mlp.c_proj.bias torch.Size([1024])
transformer.h.27.ln_1.weight torch.Size([1024])
transformer.h.27.ln_1.bias torch.Size([1024])
transformer.h.27.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.27.attn.c_attn.bias torch.Size([3072])
transformer.h.27.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.27.attn.c_proj.bias torch.Size([1024])
transformer.h.27.ln_2.weight torch.Size([1024])
transformer.h.27.ln_2.bias torch.Size([1024])
transformer.h.27.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.27.mlp.c_fc.bias torch.Size([2730])
transformer.h.27.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.27.mlp.c_fc2.bias torch.Size([2730])
transformer.h.27.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.27.mlp.c_proj.bias torch.Size([1024])
transformer.h.28.ln_1.weight torch.Size([1024])
transformer.h.28.ln_1.bias torch.Size([1024])
transformer.h.28.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.28.attn.c_attn.bias torch.Size([3072])
transformer.h.28.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.28.attn.c_proj.bias torch.Size([1024])
transformer.h.28.ln_2.weight torch.Size([1024])
transformer.h.28.ln_2.bias torch.Size([1024])
transformer.h.28.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.28.mlp.c_fc.bias torch.Size([2730])
transformer.h.28.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.28.mlp.c_fc2.bias torch.Size([2730])
transformer.h.28.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.28.mlp.c_proj.bias torch.Size([1024])
transformer.h.29.ln_1.weight torch.Size([1024])
transformer.h.29.ln_1.bias torch.Size([1024])
transformer.h.29.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.29.attn.c_attn.bias torch.Size([3072])
transformer.h.29.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.29.attn.c_proj.bias torch.Size([1024])
transformer.h.29.ln_2.weight torch.Size([1024])
transformer.h.29.ln_2.bias torch.Size([1024])
transformer.h.29.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.29.mlp.c_fc.bias torch.Size([2730])
transformer.h.29.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.29.mlp.c_fc2.bias torch.Size([2730])
transformer.h.29.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.29.mlp.c_proj.bias torch.Size([1024])
transformer.h.30.ln_1.weight torch.Size([1024])
transformer.h.30.ln_1.bias torch.Size([1024])
transformer.h.30.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.30.attn.c_attn.bias torch.Size([3072])
transformer.h.30.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.30.attn.c_proj.bias torch.Size([1024])
transformer.h.30.ln_2.weight torch.Size([1024])
transformer.h.30.ln_2.bias torch.Size([1024])
transformer.h.30.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.30.mlp.c_fc.bias torch.Size([2730])
transformer.h.30.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.30.mlp.c_fc2.bias torch.Size([2730])
transformer.h.30.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.30.mlp.c_proj.bias torch.Size([1024])
transformer.h.31.ln_1.weight torch.Size([1024])
transformer.h.31.ln_1.bias torch.Size([1024])
transformer.h.31.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.31.attn.c_attn.bias torch.Size([3072])
transformer.h.31.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.31.attn.c_proj.bias torch.Size([1024])
transformer.h.31.ln_2.weight torch.Size([1024])
transformer.h.31.ln_2.bias torch.Size([1024])
transformer.h.31.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.31.mlp.c_fc.bias torch.Size([2730])
transformer.h.31.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.31.mlp.c_fc2.bias torch.Size([2730])
transformer.h.31.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.31.mlp.c_proj.bias torch.Size([1024])
transformer.ln_f.weight torch.Size([1024])
transformer.ln_f.bias torch.Size([1024])
'''


'''
transformer.wte 1
transformer.drop 1
transformer.h.0.ln_1 1
transformer.h.0.attn.c_attn 1
transformer.h.0.attn.attn_dropout 1
transformer.h.0.attn.c_proj 1
transformer.h.0.attn.resid_dropout 1
transformer.h.0.attn 2
transformer.h.0.ln_2 1
transformer.h.0.mlp.c_fc2 1
transformer.h.0.mlp.c_fc 1
transformer.h.0.mlp.act 1
transformer.h.0.mlp.c_proj 1
transformer.h.0.mlp.dropout 1
transformer.h.0.mlp 1
transformer.h.0 2


transformer.wte.weight torch.Size([32032, 1024])
transformer.h.0.ln_1.weight torch.Size([1024])
transformer.h.0.ln_1.bias torch.Size([1024])
transformer.h.0.attn.c_attn.weight torch.Size([1024, 3072])
transformer.h.0.attn.c_attn.bias torch.Size([3072])
transformer.h.0.attn.c_proj.weight torch.Size([1024, 1024])
transformer.h.0.attn.c_proj.bias torch.Size([1024])
transformer.h.0.ln_2.weight torch.Size([1024])
transformer.h.0.ln_2.bias torch.Size([1024])
transformer.h.0.mlp.c_fc.weight torch.Size([1024, 2730])
transformer.h.0.mlp.c_fc.bias torch.Size([2730])
transformer.h.0.mlp.c_fc2.weight torch.Size([1024, 2730])
transformer.h.0.mlp.c_fc2.bias torch.Size([2730])
transformer.h.0.mlp.c_proj.weight torch.Size([2730, 1024])
transformer.h.0.mlp.c_proj.bias torch.Size([1024])
'''

'''
transformer.wte 1
transformer.drop 1
transformer.h.0.ln_1 1
transformer.h.0.attn.c_attn 1
transformer.h.0.attn.attn_dropout 1
transformer.h.0.attn.c_proj 1
transformer.h.0.attn.resid_dropout 1
transformer.h.0.attn 2
transformer.h.0.ln_2 1
transformer.h.0.mlp.c_fc2 1
transformer.h.0.mlp.c_fc 1
transformer.h.0.mlp.act 1
transformer.h.0.mlp.c_proj 1
transformer.h.0.mlp.dropout 1
transformer.h.0.mlp 1
transformer.h.0 2
transformer.h.1.ln_1 1
transformer.h.1.attn.c_attn 1
transformer.h.1.attn.attn_dropout 1
transformer.h.1.attn.c_proj 1
transformer.h.1.attn.resid_dropout 1
transformer.h.1.attn 2
transformer.h.1.ln_2 1
transformer.h.1.mlp.c_fc2 1
transformer.h.1.mlp.c_fc 1
transformer.h.1.mlp.act 1
transformer.h.1.mlp.c_proj 1
transformer.h.1.mlp.dropout 1
transformer.h.1.mlp 1
transformer.h.1 2
transformer.h.2.ln_1 1
transformer.h.2.attn.c_attn 1
transformer.h.2.attn.attn_dropout 1
transformer.h.2.attn.c_proj 1
transformer.h.2.attn.resid_dropout 1
transformer.h.2.attn 2
transformer.h.2.ln_2 1
transformer.h.2.mlp.c_fc2 1
transformer.h.2.mlp.c_fc 1
transformer.h.2.mlp.act 1
transformer.h.2.mlp.c_proj 1
transformer.h.2.mlp.dropout 1
transformer.h.2.mlp 1
transformer.h.2 2
transformer.h.3.ln_1 1
transformer.h.3.attn.c_attn 1
transformer.h.3.attn.attn_dropout 1
transformer.h.3.attn.c_proj 1
transformer.h.3.attn.resid_dropout 1
transformer.h.3.attn 2
transformer.h.3.ln_2 1
transformer.h.3.mlp.c_fc2 1
transformer.h.3.mlp.c_fc 1
transformer.h.3.mlp.act 1
transformer.h.3.mlp.c_proj 1
transformer.h.3.mlp.dropout 1
transformer.h.3.mlp 1
transformer.h.3 2
transformer.h.4.ln_1 1
transformer.h.4.attn.c_attn 1
transformer.h.4.attn.attn_dropout 1
transformer.h.4.attn.c_proj 1
transformer.h.4.attn.resid_dropout 1
transformer.h.4.attn 2
transformer.h.4.ln_2 1
transformer.h.4.mlp.c_fc2 1
transformer.h.4.mlp.c_fc 1
transformer.h.4.mlp.act 1
transformer.h.4.mlp.c_proj 1
transformer.h.4.mlp.dropout 1
transformer.h.4.mlp 1
transformer.h.4 2
transformer.h.5.ln_1 1
transformer.h.5.attn.c_attn 1
transformer.h.5.attn.attn_dropout 1
transformer.h.5.attn.c_proj 1
transformer.h.5.attn.resid_dropout 1
transformer.h.5.attn 2
transformer.h.5.ln_2 1
transformer.h.5.mlp.c_fc2 1
transformer.h.5.mlp.c_fc 1
transformer.h.5.mlp.act 1
transformer.h.5.mlp.c_proj 1
transformer.h.5.mlp.dropout 1
transformer.h.5.mlp 1
transformer.h.5 2
transformer.h.6.ln_1 1
transformer.h.6.attn.c_attn 1
transformer.h.6.attn.attn_dropout 1
transformer.h.6.attn.c_proj 1
transformer.h.6.attn.resid_dropout 1
transformer.h.6.attn 2
transformer.h.6.ln_2 1
transformer.h.6.mlp.c_fc2 1
transformer.h.6.mlp.c_fc 1
transformer.h.6.mlp.act 1
transformer.h.6.mlp.c_proj 1
transformer.h.6.mlp.dropout 1
transformer.h.6.mlp 1
transformer.h.6 2
transformer.h.7.ln_1 1
transformer.h.7.attn.c_attn 1
transformer.h.7.attn.attn_dropout 1
transformer.h.7.attn.c_proj 1
transformer.h.7.attn.resid_dropout 1
transformer.h.7.attn 2
transformer.h.7.ln_2 1
transformer.h.7.mlp.c_fc2 1
transformer.h.7.mlp.c_fc 1
transformer.h.7.mlp.act 1
transformer.h.7.mlp.c_proj 1
transformer.h.7.mlp.dropout 1
transformer.h.7.mlp 1
transformer.h.7 2
transformer.h.8.ln_1 1
transformer.h.8.attn.c_attn 1
transformer.h.8.attn.attn_dropout 1
transformer.h.8.attn.c_proj 1
transformer.h.8.attn.resid_dropout 1
transformer.h.8.attn 2
transformer.h.8.ln_2 1
transformer.h.8.mlp.c_fc2 1
transformer.h.8.mlp.c_fc 1
transformer.h.8.mlp.act 1
transformer.h.8.mlp.c_proj 1
transformer.h.8.mlp.dropout 1
transformer.h.8.mlp 1
transformer.h.8 2
transformer.h.9.ln_1 1
transformer.h.9.attn.c_attn 1
transformer.h.9.attn.attn_dropout 1
transformer.h.9.attn.c_proj 1
transformer.h.9.attn.resid_dropout 1
transformer.h.9.attn 2
transformer.h.9.ln_2 1
transformer.h.9.mlp.c_fc2 1
transformer.h.9.mlp.c_fc 1
transformer.h.9.mlp.act 1
transformer.h.9.mlp.c_proj 1
transformer.h.9.mlp.dropout 1
transformer.h.9.mlp 1
transformer.h.9 2
transformer.h.10.ln_1 1
transformer.h.10.attn.c_attn 1
transformer.h.10.attn.attn_dropout 1
transformer.h.10.attn.c_proj 1
transformer.h.10.attn.resid_dropout 1
transformer.h.10.attn 2
transformer.h.10.ln_2 1
transformer.h.10.mlp.c_fc2 1
transformer.h.10.mlp.c_fc 1
transformer.h.10.mlp.act 1
transformer.h.10.mlp.c_proj 1
transformer.h.10.mlp.dropout 1
transformer.h.10.mlp 1
transformer.h.10 2
transformer.h.11.ln_1 1
transformer.h.11.attn.c_attn 1
transformer.h.11.attn.attn_dropout 1
transformer.h.11.attn.c_proj 1
transformer.h.11.attn.resid_dropout 1
transformer.h.11.attn 2
transformer.h.11.ln_2 1
transformer.h.11.mlp.c_fc2 1
transformer.h.11.mlp.c_fc 1
transformer.h.11.mlp.act 1
transformer.h.11.mlp.c_proj 1
transformer.h.11.mlp.dropout 1
transformer.h.11.mlp 1
transformer.h.11 2
transformer.h.12.ln_1 1
transformer.h.12.attn.c_attn 1
transformer.h.12.attn.attn_dropout 1
transformer.h.12.attn.c_proj 1
transformer.h.12.attn.resid_dropout 1
transformer.h.12.attn 2
transformer.h.12.ln_2 1
transformer.h.12.mlp.c_fc2 1
transformer.h.12.mlp.c_fc 1
transformer.h.12.mlp.act 1
transformer.h.12.mlp.c_proj 1
transformer.h.12.mlp.dropout 1
transformer.h.12.mlp 1
transformer.h.12 2
transformer.h.13.ln_1 1
transformer.h.13.attn.c_attn 1
transformer.h.13.attn.attn_dropout 1
transformer.h.13.attn.c_proj 1
transformer.h.13.attn.resid_dropout 1
transformer.h.13.attn 2
transformer.h.13.ln_2 1
transformer.h.13.mlp.c_fc2 1
transformer.h.13.mlp.c_fc 1
transformer.h.13.mlp.act 1
transformer.h.13.mlp.c_proj 1
transformer.h.13.mlp.dropout 1
transformer.h.13.mlp 1
transformer.h.13 2
transformer.h.14.ln_1 1
transformer.h.14.attn.c_attn 1
transformer.h.14.attn.attn_dropout 1
transformer.h.14.attn.c_proj 1
transformer.h.14.attn.resid_dropout 1
transformer.h.14.attn 2
transformer.h.14.ln_2 1
transformer.h.14.mlp.c_fc2 1
transformer.h.14.mlp.c_fc 1
transformer.h.14.mlp.act 1
transformer.h.14.mlp.c_proj 1
transformer.h.14.mlp.dropout 1
transformer.h.14.mlp 1
transformer.h.14 2
transformer.h.15.ln_1 1
transformer.h.15.attn.c_attn 1
transformer.h.15.attn.attn_dropout 1
transformer.h.15.attn.c_proj 1
transformer.h.15.attn.resid_dropout 1
transformer.h.15.attn 2
transformer.h.15.ln_2 1
transformer.h.15.mlp.c_fc2 1
transformer.h.15.mlp.c_fc 1
transformer.h.15.mlp.act 1
transformer.h.15.mlp.c_proj 1
transformer.h.15.mlp.dropout 1
transformer.h.15.mlp 1
transformer.h.15 2
transformer.h.16.ln_1 1
transformer.h.16.attn.c_attn 1
transformer.h.16.attn.attn_dropout 1
transformer.h.16.attn.c_proj 1
transformer.h.16.attn.resid_dropout 1
transformer.h.16.attn 2
transformer.h.16.ln_2 1
transformer.h.16.mlp.c_fc2 1
transformer.h.16.mlp.c_fc 1
transformer.h.16.mlp.act 1
transformer.h.16.mlp.c_proj 1
transformer.h.16.mlp.dropout 1
transformer.h.16.mlp 1
transformer.h.16 2
transformer.h.17.ln_1 1
transformer.h.17.attn.c_attn 1
transformer.h.17.attn.attn_dropout 1
transformer.h.17.attn.c_proj 1
transformer.h.17.attn.resid_dropout 1
transformer.h.17.attn 2
transformer.h.17.ln_2 1
transformer.h.17.mlp.c_fc2 1
transformer.h.17.mlp.c_fc 1
transformer.h.17.mlp.act 1
transformer.h.17.mlp.c_proj 1
transformer.h.17.mlp.dropout 1
transformer.h.17.mlp 1
transformer.h.17 2
transformer.h.18.ln_1 1
transformer.h.18.attn.c_attn 1
transformer.h.18.attn.attn_dropout 1
transformer.h.18.attn.c_proj 1
transformer.h.18.attn.resid_dropout 1
transformer.h.18.attn 2
transformer.h.18.ln_2 1
transformer.h.18.mlp.c_fc2 1
transformer.h.18.mlp.c_fc 1
transformer.h.18.mlp.act 1
transformer.h.18.mlp.c_proj 1
transformer.h.18.mlp.dropout 1
transformer.h.18.mlp 1
transformer.h.18 2
transformer.h.19.ln_1 1
transformer.h.19.attn.c_attn 1
transformer.h.19.attn.attn_dropout 1
transformer.h.19.attn.c_proj 1
transformer.h.19.attn.resid_dropout 1
transformer.h.19.attn 2
transformer.h.19.ln_2 1
transformer.h.19.mlp.c_fc2 1
transformer.h.19.mlp.c_fc 1
transformer.h.19.mlp.act 1
transformer.h.19.mlp.c_proj 1
transformer.h.19.mlp.dropout 1
transformer.h.19.mlp 1
transformer.h.19 2
transformer.h.20.ln_1 1
transformer.h.20.attn.c_attn 1
transformer.h.20.attn.attn_dropout 1
transformer.h.20.attn.c_proj 1
transformer.h.20.attn.resid_dropout 1
transformer.h.20.attn 2
transformer.h.20.ln_2 1
transformer.h.20.mlp.c_fc2 1
transformer.h.20.mlp.c_fc 1
transformer.h.20.mlp.act 1
transformer.h.20.mlp.c_proj 1
transformer.h.20.mlp.dropout 1
transformer.h.20.mlp 1
transformer.h.20 2
transformer.h.21.ln_1 1
transformer.h.21.attn.c_attn 1
transformer.h.21.attn.attn_dropout 1
transformer.h.21.attn.c_proj 1
transformer.h.21.attn.resid_dropout 1
transformer.h.21.attn 2
transformer.h.21.ln_2 1
transformer.h.21.mlp.c_fc2 1
transformer.h.21.mlp.c_fc 1
transformer.h.21.mlp.act 1
transformer.h.21.mlp.c_proj 1
transformer.h.21.mlp.dropout 1
transformer.h.21.mlp 1
transformer.h.21 2
transformer.h.22.ln_1 1
transformer.h.22.attn.c_attn 1
transformer.h.22.attn.attn_dropout 1
transformer.h.22.attn.c_proj 1
transformer.h.22.attn.resid_dropout 1
transformer.h.22.attn 2
transformer.h.22.ln_2 1
transformer.h.22.mlp.c_fc2 1
transformer.h.22.mlp.c_fc 1
transformer.h.22.mlp.act 1
transformer.h.22.mlp.c_proj 1
transformer.h.22.mlp.dropout 1
transformer.h.22.mlp 1
transformer.h.22 2
transformer.h.23.ln_1 1
transformer.h.23.attn.c_attn 1
transformer.h.23.attn.attn_dropout 1
transformer.h.23.attn.c_proj 1
transformer.h.23.attn.resid_dropout 1
transformer.h.23.attn 2
transformer.h.23.ln_2 1
transformer.h.23.mlp.c_fc2 1
transformer.h.23.mlp.c_fc 1
transformer.h.23.mlp.act 1
transformer.h.23.mlp.c_proj 1
transformer.h.23.mlp.dropout 1
transformer.h.23.mlp 1
transformer.h.23 2
transformer.h.24.ln_1 1
transformer.h.24.attn.c_attn 1
transformer.h.24.attn.attn_dropout 1
transformer.h.24.attn.c_proj 1
transformer.h.24.attn.resid_dropout 1
transformer.h.24.attn 2
transformer.h.24.ln_2 1
transformer.h.24.mlp.c_fc2 1
transformer.h.24.mlp.c_fc 1
transformer.h.24.mlp.act 1
transformer.h.24.mlp.c_proj 1
transformer.h.24.mlp.dropout 1
transformer.h.24.mlp 1
transformer.h.24 2
transformer.h.25.ln_1 1
transformer.h.25.attn.c_attn 1
transformer.h.25.attn.attn_dropout 1
transformer.h.25.attn.c_proj 1
transformer.h.25.attn.resid_dropout 1
transformer.h.25.attn 2
transformer.h.25.ln_2 1
transformer.h.25.mlp.c_fc2 1
transformer.h.25.mlp.c_fc 1
transformer.h.25.mlp.act 1
transformer.h.25.mlp.c_proj 1
transformer.h.25.mlp.dropout 1
transformer.h.25.mlp 1
transformer.h.25 2
transformer.h.26.ln_1 1
transformer.h.26.attn.c_attn 1
transformer.h.26.attn.attn_dropout 1
transformer.h.26.attn.c_proj 1
transformer.h.26.attn.resid_dropout 1
transformer.h.26.attn 2
transformer.h.26.ln_2 1
transformer.h.26.mlp.c_fc2 1
transformer.h.26.mlp.c_fc 1
transformer.h.26.mlp.act 1
transformer.h.26.mlp.c_proj 1
transformer.h.26.mlp.dropout 1
transformer.h.26.mlp 1
transformer.h.26 2
transformer.h.27.ln_1 1
transformer.h.27.attn.c_attn 1
transformer.h.27.attn.attn_dropout 1
transformer.h.27.attn.c_proj 1
transformer.h.27.attn.resid_dropout 1
transformer.h.27.attn 2
transformer.h.27.ln_2 1
transformer.h.27.mlp.c_fc2 1
transformer.h.27.mlp.c_fc 1
transformer.h.27.mlp.act 1
transformer.h.27.mlp.c_proj 1
transformer.h.27.mlp.dropout 1
transformer.h.27.mlp 1
transformer.h.27 2
transformer.h.28.ln_1 1
transformer.h.28.attn.c_attn 1
transformer.h.28.attn.attn_dropout 1
transformer.h.28.attn.c_proj 1
transformer.h.28.attn.resid_dropout 1
transformer.h.28.attn 2
transformer.h.28.ln_2 1
transformer.h.28.mlp.c_fc2 1
transformer.h.28.mlp.c_fc 1
transformer.h.28.mlp.act 1
transformer.h.28.mlp.c_proj 1
transformer.h.28.mlp.dropout 1
transformer.h.28.mlp 1
transformer.h.28 2
transformer.h.29.ln_1 1
transformer.h.29.attn.c_attn 1
transformer.h.29.attn.attn_dropout 1
transformer.h.29.attn.c_proj 1
transformer.h.29.attn.resid_dropout 1
transformer.h.29.attn 2
transformer.h.29.ln_2 1
transformer.h.29.mlp.c_fc2 1
transformer.h.29.mlp.c_fc 1
transformer.h.29.mlp.act 1
transformer.h.29.mlp.c_proj 1
transformer.h.29.mlp.dropout 1
transformer.h.29.mlp 1
transformer.h.29 2
transformer.h.30.ln_1 1
transformer.h.30.attn.c_attn 1
transformer.h.30.attn.attn_dropout 1
transformer.h.30.attn.c_proj 1
transformer.h.30.attn.resid_dropout 1
transformer.h.30.attn 2
transformer.h.30.ln_2 1
transformer.h.30.mlp.c_fc2 1
transformer.h.30.mlp.c_fc 1
transformer.h.30.mlp.act 1
transformer.h.30.mlp.c_proj 1
transformer.h.30.mlp.dropout 1
transformer.h.30.mlp 1
transformer.h.30 2
transformer.h.31.ln_1 1
transformer.h.31.attn.c_attn 1
transformer.h.31.attn.attn_dropout 1
transformer.h.31.attn.c_proj 1
transformer.h.31.attn.resid_dropout 1
transformer.h.31.attn 2
transformer.h.31.ln_2 1
transformer.h.31.mlp.c_fc2 1
transformer.h.31.mlp.c_fc 1
transformer.h.31.mlp.act 1
transformer.h.31.mlp.c_proj 1
transformer.h.31.mlp.dropout 1
transformer.h.31.mlp 1
transformer.h.31 2
transformer.ln_f 1
transformer 3
lm_head 1
 3
'''