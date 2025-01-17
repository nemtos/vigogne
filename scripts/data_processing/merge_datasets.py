#!/usr/bin/env python
# coding=utf-8
# Copyright 2023 Bofeng Huang

import argparse
from pathlib import Path

from vigogne.data_utils import Conversation, Instruct
from vigogne.file_utils import jsonl_dump, jsonl_load


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs-files", "-i", type=str, required=True, nargs="+")
    parser.add_argument("--output-file", "-o", type=str, required=True)
    args = parser.parse_args()

    # validated_keys = {"instruction", "input", "output"}
    # validated_keys = {"messages"}
    # validated_keys = set(Instruct.__fields__.keys())
    validated_keys = set(Conversation.__fields__.keys())

    data = []
    for input_file in args.inputs_files:
        # data.extend(jsonl_load(input_file))
        sub_data = jsonl_load(input_file)
        print(f"Loaded {len(sub_data):,d} examples from {input_file}")

        for example_idx, example in enumerate(sub_data):
            example = {k: v for k, v in example.items() if k in validated_keys}
            # if "tmp_alpaca_selfinstruct_dollybactrian_selfchatquora_oasstfr_dummy_train" not in input_file:
            #     example[ID] = Path(input_file).stem + f"_{example_idx:09d}"
            data.append(example)

    jsonl_dump(data, args.output_file, mode="w")
    print(f"Saved {len(data):,d} examples into {args.output_file}")


if __name__ == "__main__":
    main()
