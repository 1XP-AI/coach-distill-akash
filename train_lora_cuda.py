#!/usr/bin/env python3
"""train_lora_cuda.py — NVIDIA/PyTorch port of the coach-distill LoRA training.

The Mac path (mlx_lm.lora) is Apple-Silicon only; Akash GPUs are NVIDIA, so
this runs the SAME dataset through HuggingFace transformers + PEFT. The data
(out/train/{train,valid}.jsonl) is framework-agnostic prompt/completion text
already rendered through the Qwen3.5 chat template, so nothing about WHAT the
student learns changes — only the trainer.

Loss is masked to the completion (prompt tokens set to -100), matching the
mlx `mask_prompt: true` semantics: the student learns replies + tool calls,
not to echo the system prompt.

Env:
  STUDENT_MODEL   base model (default Qwen/Qwen3.5-0.8B)
  DATA_DIR        dir with train.jsonl / valid.jsonl (default /data/train)
  OUTPUT_DIR      adapter output (default /output/adapters)
  EPOCHS, BATCH_SIZE, GRAD_ACCUM, LR, MAX_SEQ_LEN, LORA_RANK
  HF_TOKEN        (optional) write token → push adapter to HUB_REPO on finish
  HUB_REPO        (optional) e.g. "1xp/coach-qwen35-0.8b-lora"
"""

import json
import os
from pathlib import Path

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

MODEL = os.environ.get("STUDENT_MODEL", "Qwen/Qwen3.5-0.8B")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/train"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output/adapters"))
EPOCHS = float(os.environ.get("EPOCHS", "3.3"))
BATCH = int(os.environ.get("BATCH_SIZE", "4"))
GRAD_ACCUM = int(os.environ.get("GRAD_ACCUM", "1"))
LR = float(os.environ.get("LR", "1e-4"))
MAX_SEQ_LEN = int(os.environ.get("MAX_SEQ_LEN", "3328"))
LORA_RANK = int(os.environ.get("LORA_RANK", "16"))
SEED = 7


def build_example(tokenizer, prompt: str, completion: str):
    """Tokenize prompt+completion; label-mask the prompt so loss is on the
    completion only. Truncates from the LEFT of the prompt if over budget so
    the completion (with its tool call) is never cut."""
    p_ids = tokenizer(prompt, add_special_tokens=False).input_ids
    c_ids = tokenizer(completion, add_special_tokens=False).input_ids
    if len(p_ids) + len(c_ids) > MAX_SEQ_LEN:
        keep = MAX_SEQ_LEN - len(c_ids)
        p_ids = p_ids[-keep:] if keep > 0 else []
    input_ids = p_ids + c_ids
    labels = [-100] * len(p_ids) + c_ids[:]
    return {"input_ids": input_ids, "labels": labels}


class Collator:
    def __init__(self, pad_id):
        self.pad_id = pad_id

    def __call__(self, batch):
        width = max(len(b["input_ids"]) for b in batch)
        input_ids, labels, attn = [], [], []
        for b in batch:
            pad = width - len(b["input_ids"])
            input_ids.append(b["input_ids"] + [self.pad_id] * pad)
            labels.append(b["labels"] + [-100] * pad)
            attn.append([1] * len(b["input_ids"]) + [0] * pad)
        return {
            "input_ids": torch.tensor(input_ids),
            "labels": torch.tensor(labels),
            "attention_mask": torch.tensor(attn),
        }


def main():
    torch.manual_seed(SEED)
    print(f"model={MODEL} | data={DATA_DIR} | epochs={EPOCHS} batch={BATCH} "
          f"seq={MAX_SEQ_LEN} rank={LORA_RANK} | cuda={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print("  GPU:", torch.cuda.get_device_name(0),
              f"{torch.cuda.get_device_properties(0).total_memory/1e9:.0f}GB")

    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        MODEL, torch_dtype=torch.bfloat16, device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False
    model.enable_input_require_grads()

    # target every linear proj so a small model still learns the domain well;
    # let PEFT auto-resolve module names across the hybrid arch's layers.
    peft_cfg = LoraConfig(
        r=LORA_RANK, lora_alpha=LORA_RANK * 2, lora_dropout=0.05,
        target_modules="all-linear", task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, peft_cfg)
    model.print_trainable_parameters()

    # Data source: local DATA_DIR if present, else pull from a private HF
    # dataset repo (DATA_REPO) that the datagen phase uploaded to — keeps the
    # training container self-sufficient with nothing baked in.
    data_dir = DATA_DIR
    if not (data_dir / "train.jsonl").exists():
        data_repo = os.environ.get("DATA_REPO")
        if not data_repo:
            raise SystemExit(f"no {data_dir}/train.jsonl and DATA_REPO unset")
        from huggingface_hub import snapshot_download
        data_dir = Path(snapshot_download(
            repo_id=data_repo, repo_type="dataset",
            token=os.environ.get("HF_TOKEN"),
        ))
        print(f"pulled dataset from {data_repo} → {data_dir}")

    ds = load_dataset(
        "json",
        data_files={
            "train": str(data_dir / "train.jsonl"),
            "valid": str(data_dir / "valid.jsonl"),
        },
    )
    cols = ds["train"].column_names
    ds = ds.map(
        lambda ex: build_example(tokenizer, ex["prompt"], ex["completion"]),
        remove_columns=cols,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    args = TrainingArguments(
        output_dir=str(OUTPUT_DIR),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH,
        gradient_accumulation_steps=GRAD_ACCUM,
        learning_rate=LR,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        logging_steps=10,
        eval_strategy="steps",
        eval_steps=50,
        save_steps=150,
        save_total_limit=2,
        bf16=True,
        gradient_checkpointing=True,
        report_to=[],
        seed=SEED,
    )
    trainer = Trainer(
        model=model, args=args,
        train_dataset=ds["train"], eval_dataset=ds["valid"],
        data_collator=Collator(tokenizer.pad_token_id),
    )
    trainer.train()

    trainer.save_model(str(OUTPUT_DIR))
    tokenizer.save_pretrained(str(OUTPUT_DIR))
    print(f"adapter saved → {OUTPUT_DIR}")

    hub_repo = os.environ.get("HUB_REPO")
    hf_token = os.environ.get("HF_TOKEN")
    if hub_repo and hf_token:
        from huggingface_hub import HfApi
        api = HfApi(token=hf_token)
        api.create_repo(hub_repo, private=True, exist_ok=True)
        api.upload_folder(folder_path=str(OUTPUT_DIR), repo_id=hub_repo)
        print(f"adapter pushed → https://huggingface.co/{hub_repo}")
    else:
        print("HUB_REPO/HF_TOKEN not set — adapter kept in container only "
              "(retrieve via `provider shell` / Console before closing lease)")


if __name__ == "__main__":
    main()
