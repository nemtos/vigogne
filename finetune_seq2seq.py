#! /usr/bin/env python
# coding=utf-8

import logging
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import bitsandbytes as bnb
import datasets
import numpy as np
import torch
import transformers
from datasets import DatasetDict, load_dataset
from peft import LoraConfig, TaskType, get_peft_model, get_peft_model_state_dict, prepare_model_for_int8_training
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, HfArgumentParser, Seq2SeqTrainer, Seq2SeqTrainingArguments

from utils import print_trainable_parameters

logger = logging.getLogger(__name__)

IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "</s>"
DEFAULT_UNK_TOKEN = "</s>"


# PROMPT_DICT = {"prompt_input": "Instruction: {instruction} Input: {input}", "prompt_no_input": "Instruction: {instruction}"}
# French prompt for seq2seq
PROMPT_DICT = {"prompt_input": "Instruction: {instruction} Entrée: {input}", "prompt_no_input": "Instruction: {instruction}"}


def generate_prompt(example):
    return (
        PROMPT_DICT["prompt_input"].format_map(example)
        if example["input"]
        else PROMPT_DICT["prompt_no_input"].format_map(example)
    )


@dataclass
class ModelArguments:
    # Base model parameters
    model_name_or_path: Optional[str] = field(default=None)
    # LoRA parameters
    lora_r: int = field(default=8, metadata={"help": "Lora rank."})
    lora_alpha: int = field(default=16, metadata={"help": "Lora alpha."})
    lora_dropout: float = field(default=0.05, metadata={"help": "Lora dropout."})
    target_modules: List[str] = field(
        default_factory=lambda: ["q_proj", "v_proj"], metadata={"help": "Names of the modules to apply Lora to."}
    )


@dataclass
class DataArguments:
    train_file: Optional[str] = field(default=None, metadata={"help": "Path to the training file."})
    eval_file: Optional[str] = field(default=None, metadata={"help": "Path to the evaluation file."})
    max_source_length: Optional[int] = field(
        default=None, metadata={"help": "Maximum source length. Sequences will be right padded (and possibly truncated)."}
    )
    max_target_length: Optional[int] = field(
        default=None, metadata={"help": "Maximum target length. Sequences will be right padded (and possibly truncated)."}
    )
    model_max_source_length_percentile: Optional[int] = field(
        default=95, metadata={"help": "Percentile of the source length. Used to determin `max_source_length`."}
    )
    model_max_target_length_percentile: Optional[int] = field(
        default=95, metadata={"help": "Percentile of the target length. Used to determin `max_target_length`."}
    )
    preprocessing_num_workers: Optional[int] = field(
        default=None, metadata={"help": "The number of processes to use for the preprocessing."}
    )


@dataclass
class VigogneSeq2SeqTrainingArguments(Seq2SeqTrainingArguments):
    optim: str = field(default="adamw_torch", metadata={"help": "Optimizer to use."})


# Modified from: https://github.com/bofenghuang/stanford_alpaca/blob/eb5b171d9b103a12a8e14e0edca9cbc45fe1d512/train.py#L166-L182
# Almost same to transformers.DataCollatorForSeq2Seq
@dataclass
class DataCollatorForSupervisedDataset(object):
    """Collate examples for supervised fine-tuning."""

    tokenizer: transformers.PreTrainedTokenizer
    pad_to_multiple_of: Optional[int] = None

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        # dtype = torch.long
        # input_ids, labels = tuple([torch.LongTensor(instance[key]) for instance in instances] for key in ("input_ids", "labels"))
        input_ids, labels = tuple([instance[key] for instance in instances] for key in ("input_ids", "labels"))

        if self.pad_to_multiple_of is not None:
            max_input_length_index, max_input_length = max(
                enumerate([len(input_ids_) for input_ids_ in input_ids]), key=lambda x: x[1]
            )
            # n_input_padding = ((max_input_length // self.pad_to_multiple_of) + 1) * self.pad_to_multiple_of - max_input_length
            n_input_padding = (
                math.ceil(max_input_length / self.pad_to_multiple_of) * self.pad_to_multiple_of - max_input_length
            )
            # Pad the longest example to pad_to_multiple_of * N
            input_ids[max_input_length_index].extend([self.tokenizer.pad_token_id] * n_input_padding)

            max_label_length_index, max_label_length = max(enumerate([len(labels_) for labels_ in labels]), key=lambda x: x[1])
            # n_label_padding = ((max_label_length // self.pad_to_multiple_of) + 1) * self.pad_to_multiple_of - max_label_length
            n_label_padding = (
                math.ceil(max_label_length / self.pad_to_multiple_of) * self.pad_to_multiple_of - max_label_length
            )
            # Pad the longest example to pad_to_multiple_of * N
            labels[max_label_length_index].extend([IGNORE_INDEX] * n_label_padding)

        input_ids = [torch.LongTensor(input_ids_) for input_ids_ in input_ids]
        labels = [torch.LongTensor(labels_) for labels_ in labels]

        input_ids = torch.nn.utils.rnn.pad_sequence(input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id)
        labels = torch.nn.utils.rnn.pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)

        return dict(
            input_ids=input_ids,
            labels=labels,
            attention_mask=input_ids.ne(self.tokenizer.pad_token_id),
        )


# Copied from https://github.com/bofenghuang/stanford_alpaca/blob/eb5b171d9b103a12a8e14e0edca9cbc45fe1d512/train.py#L75-L95
def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: Dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.
    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg


def train():
    # HF parser
    parser = HfArgumentParser((ModelArguments, DataArguments, VigogneSeq2SeqTrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if training_args.should_log:
        # The default of training_args.log_level is passive, so we set log level at info here to have that default.
        transformers.utils.logging.set_verbosity_info()

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    # Set the verbosity to info of the Transformers logger (on main process only):
    logger.info(f"Model parameters {model_args}")
    logger.info(f"Training/evaluation parameters {training_args}")

    # todo: better handle
    device_map = "auto"
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    ddp = world_size != 1
    if ddp:
        device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}

    # Load model and tokenizer
    model = AutoModelForSeq2SeqLM.from_pretrained(
        model_args.model_name_or_path,
        load_in_8bit=True,
        device_map=device_map,
    )

    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        padding_side="right",
        use_fast=True,
    )

    # Freeze the model parameters
    # Cast the small parameters (e.g. layernorm) to fp32 for stability
    model = prepare_model_for_int8_training(model)

    lora_config = LoraConfig(
        r=model_args.lora_r,
        lora_alpha=model_args.lora_alpha,
        target_modules=model_args.target_modules,
        lora_dropout=model_args.lora_dropout,
        bias="none",
        task_type=TaskType.SEQ_2_SEQ_LM,
    )
    model = get_peft_model(model, lora_config)
    print_trainable_parameters(model)

    # Load data
    raw_datasets = DatasetDict()
    if data_args.train_file is not None:
        ext = data_args.train_file.rsplit(".", 1)[-1]
        raw_datasets["train"] = load_dataset(ext, data_files=data_args.train_file)["train"]
    else:
        raise ValueError("You have not specified any train file")
    if data_args.eval_file is not None:
        ext = data_args.eval_file.rsplit(".", 1)[-1]
        raw_datasets["eval"] = load_dataset(ext, data_files=data_args.eval_file)["train"]
    # logger.info(raw_datasets)

    max_source_length = data_args.max_source_length
    max_target_length = data_args.max_target_length

    def get_example_length(example):
        if max_source_length is None:
            user_prompt = generate_prompt(example)
            example["source_length"] = len(tokenizer(user_prompt)["input_ids"])
        if max_target_length is None:
            example["target_length"] = len(tokenizer(example["output"] + tokenizer.eos_token)["input_ids"])
        return example

    if max_source_length is None or max_target_length is None:
        with training_args.main_process_first(desc="dataset map tokenization"):
            train_example_lengths = raw_datasets["train"].map(
                get_example_length,
                num_proc=data_args.preprocessing_num_workers,
                remove_columns=next(iter(raw_datasets.values())).column_names,
                desc="get example lengths",
            )
        if max_source_length is None:
            # Take percentile of max length
            max_source_length = math.ceil(
                np.percentile(train_example_lengths["source_length"], data_args.model_max_source_length_percentile)
            )
            logger.info(
                f"`max_source_length` has been set to the {data_args.model_max_source_length_percentile}th percentile of training example lengths: {max_source_length}"
            )
        if max_target_length is None:
            # Take percentile of max length
            max_target_length = math.ceil(
                np.percentile(train_example_lengths["source_length"], data_args.model_max_target_length_percentile)
            )
            logger.info(
                f"`max_target_length` has been set to the {data_args.model_max_target_length_percentile}th percentile of training example lengths: {max_target_length}"
            )

    def preprocess_function(example):
        # Format prompt
        user_prompt = generate_prompt(example)

        input_ids = tokenizer(user_prompt, max_length=max_source_length, truncation=True)["input_ids"]

        labels = tokenizer(text_target=example["output"], max_length=max_target_length, truncation=True)["input_ids"]

        return {"input_ids": input_ids, "labels": labels}

        # # tokenize inputs
        # model_inputs = tokenizer(user_prompt, max_length=max_source_length, padding=padding, truncation=True)

        # # Tokenize targets with the `text_target` keyword argument
        # labels = tokenizer(text_target=example["output"], max_length=max_target_length, padding=padding, truncation=True)

        # # If we are padding here, replace all tokenizer.pad_token_id in the labels by -100 when we want to ignore
        # # padding in the loss.
        # if padding == "max_length":
        #     labels["input_ids"] = [label if label != tokenizer.pad_token_id else IGNORE_INDEX for label in labels["input_ids"]]

        # model_inputs["labels"] = labels["input_ids"]
        # return model_inputs

    with training_args.main_process_first(desc="dataset map tokenization"):
        preprocessed_dataset = raw_datasets.map(
            preprocess_function,
            num_proc=data_args.preprocessing_num_workers,
            remove_columns=next(iter(raw_datasets.values())).column_names,
            desc="preprocess data set",
        )

    trainer = Seq2SeqTrainer(
        model=model,
        train_dataset=preprocessed_dataset["train"],
        eval_dataset=preprocessed_dataset["eval"] if data_args.eval_file is not None else None,
        args=training_args,
        data_collator=DataCollatorForSupervisedDataset(
            tokenizer=tokenizer, pad_to_multiple_of=8 if training_args.fp16 else None
        ),
        # data_collator=DataCollatorForSeq2Seq(tokenizer, model=model, label_pad_token_id=IGNORE_INDEX, pad_to_multiple_of=8),
    )

    # Silence the warnings. Please re-enable for inference!
    model.config.use_cache = False

    old_state_dict = model.state_dict
    model.state_dict = (lambda self, *_, **__: get_peft_model_state_dict(self, old_state_dict())).__get__(model, type(model))

    if torch.__version__ >= "2" and sys.platform != "win32":
        model = torch.compile(model)

    trainer.train()

    model.save_pretrained(training_args.output_dir)


if __name__ == "__main__":
    train()
