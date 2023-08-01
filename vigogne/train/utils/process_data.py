# coding=utf-8
# Copyright 2023  Bofeng Huang

from dataclasses import asdict, dataclass
from typing import Dict, Optional

import transformers

from vigogne.constants import ASSISTANT, CHAT, CONTENT, CONVERSATION, INSTRUCT, ROLE, USER
from vigogne.preprocess import SUPPORTED_DATA_TEMPLATES, ConversationTemplate, InstructTemplate
from vigogne.train.utils.constants import IGNORE_INDEX


@dataclass
class InstructProcessor(InstructTemplate):
    def get_example_length(self, example: Dict[str, str], tokenizer: transformers.PreTrainedTokenizer):
        # user_prompt = self.get_training_prompt(example["instruction"], example["input"], example["output"])
        user_prompt = self.get_training_prompt(**example)
        # NB: might be incorrect for other tokenizers than llama depending on config
        example["example_length"] = len(tokenizer(user_prompt + tokenizer.eos_token)["input_ids"])
        return example

    def process_example(
        self,
        example: Dict[str, str],
        tokenizer: transformers.PreTrainedTokenizer,
        model_max_length: Optional[int] = None,
        length_column_name: Optional[str] = None,
    ):
        """
        input_tokens = [tokenizer.bos_token] + prompt_tokens + completion_tokens + [tokenizer.eos_token]
        label_tokens = [tokenizer.bos_token] + [-100] * len(prompt_tokens) + completion_tokens + [tokenizer.eos_token]
        """
        # Format prompt
        user_prompt = self.get_inference_prompt(**example)

        # Get prompt length for masking
        len_user_prompt_tokens = len(tokenizer(user_prompt, truncation=True, max_length=model_max_length)["input_ids"])

        # Tokenize
        input_ids = tokenizer(
            user_prompt + example["output"] + tokenizer.eos_token, truncation=True, max_length=model_max_length
        )["input_ids"]

        # Mask prompt
        labels = [IGNORE_INDEX] * len_user_prompt_tokens + input_ids[len_user_prompt_tokens:]

        # Tokenize
        # input_ids = tokenizer(user_prompt + example["output"] + tokenizer.eos_token, truncation=True, return_tensors="pt")["input_ids"][0]
        # labels = input_ids.clone()
        # Mask prompt
        # labels[:len_user_prompt_tokens] = IGNORE_INDEX

        # attention_mask will be added later by collator
        processed_example = {"input_ids": input_ids, "labels": labels}
        if length_column_name is not None:
            processed_example[length_column_name] = len(input_ids)

        return processed_example


@dataclass
class ConversationProcessor(ConversationTemplate):
    def get_example_length(self, example: Dict[str, str], tokenizer: transformers.PreTrainedTokenizer):
        user_prompt = self.get_training_prompt(example, tokenizer)
        # eos_token has been already formatted into prompt
        # NB: might be incorrect for other tokenizers than llama depending on config
        example["example_length"] = len(tokenizer(user_prompt)["input_ids"])
        return example

    def process_example(
        self,
        example: Dict[str, str],
        tokenizer: transformers.PreTrainedTokenizer,
        model_max_length: Optional[int] = None,
        length_column_name: Optional[str] = None,
        do_mask_input: bool = True,
    ):
        """
        input_tokens = [tokenizer.bos_token] + user_tokens_a + assistant_tokens_a + [tokenizer.eos_token]  + user_tokens_b + assistant_tokens_b + [tokenizer.eos_token]
        label_tokens = [tokenizer.bos_token] + [-100] * len(user_tokens_a) + assistant_tokens_a + [tokenizer.eos_token] + [-100] * len(user_tokens_b) + assistant_tokens_b
        """
        # w/ bos_token, w/o eos_token
        input_ids = tokenizer(self.system_message + "\n")["input_ids"]

        # w/o bos_token or eos_token
        user_prefix_input_ids = tokenizer(f"\n{self.user_prefix}:", add_special_tokens=False)["input_ids"]
        assistant_prefix_input_ids = tokenizer(f"\n{self.assistant_prefix}:", add_special_tokens=False)["input_ids"]

        # tmp fix for llama-2
        # NB: might be incorrect for other tokenizers than llama depending on config
        user_prefix_input_ids = user_prefix_input_ids[1:]
        assistant_prefix_input_ids = assistant_prefix_input_ids[1:]

        non_ignore_indexes = []
        for speaking_turn in example[CONVERSATION]:
            # w/o bos_token or eos_token
            message_input_ids = tokenizer(
                f'{speaking_turn[CONTENT]}{tokenizer.eos_token if speaking_turn[ROLE] == ASSISTANT else ""}',
                add_special_tokens=False,
            )["input_ids"]

            input_ids += (
                assistant_prefix_input_ids + message_input_ids
                if speaking_turn[ROLE] == ASSISTANT
                else user_prefix_input_ids + message_input_ids
            )

            # note token indexes for reponse
            if speaking_turn[ROLE] == ASSISTANT:
                non_ignore_indexes.append([len(input_ids) - len(message_input_ids), len(input_ids)])

        if model_max_length is not None:
            input_ids = input_ids[:model_max_length]

        # mask system message, user prompt, and all format tokens
        if do_mask_input:
            labels = [IGNORE_INDEX] * len(input_ids)

            for non_ignore_s, non_ignore_e in non_ignore_indexes:
                labels[non_ignore_s:non_ignore_e] = input_ids[non_ignore_s:non_ignore_e]
        else:
            labels = input_ids.copy()

        processed_example = {"input_ids": input_ids, "labels": labels}
        if length_column_name is not None:
            processed_example[length_column_name] = len(input_ids)

        return processed_example


SUPPORTED_PROCESSOR_TEMPLATES = {
    INSTRUCT: InstructProcessor(**SUPPORTED_DATA_TEMPLATES.get(INSTRUCT).to_dict()),
    CHAT: ConversationProcessor(**SUPPORTED_DATA_TEMPLATES.get(CHAT).to_dict()),
}