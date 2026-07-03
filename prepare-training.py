#!/usr/bin/env python3
"""prepare-training.py — teacher JSONL → mlx_lm.lora prompt/completion pairs.

Renders each accepted teacher sample through the STUDENT's own chat template
(Qwen3.5, thinking disabled, tools injected) so the training text is byte-
identical to what the browser runtime will produce at inference time:

  prompt     = chat_template(system + turns, tools, add_generation_prompt)
  completion = assistant turn (reply text + native <tool_call> block) + EOS

Loss is masked to the completion (mask_prompt: true in train-lora.yaml), so
the student learns replies/tool-calls — not to regurgitate system prompts.

Run inside the mlx venv (transformers ships with mlx-lm):
  ~/.mlx-coach/venv/bin/python scripts/coach-distill/prepare-training.py
"""

import json
import os
import random
import sys
from pathlib import Path

from transformers import AutoTokenizer

HERE = Path(__file__).parent
OUT = HERE / "out"
DATA = OUT / "teacher-data.jsonl"
TRAIN_DIR = OUT / "train"
MODEL = os.environ.get("STUDENT_MODEL", "Qwen/Qwen3.5-0.8B")
VALID_FRACTION = 0.05
SEED = 7

def to_messages(sample: dict) -> tuple[list, dict]:
    msgs = [{"role": "system", "content": sample["system"]}]
    for t in sample["turns"]:
        msgs.append({"role": "assistant" if t["role"] == "coach" else "user", "content": t["text"]})
    assistant: dict = {"role": "assistant", "content": sample["reply"] or ""}
    if sample.get("proposal"):
        assistant["tool_calls"] = [
            {
                "type": "function",
                "function": {
                    "name": sample["proposal"]["toolName"],
                    "arguments": sample["proposal"]["params"],
                },
            }
        ]
    return msgs, assistant

def main() -> None:
    tok = AutoTokenizer.from_pretrained(MODEL)
    tools = json.loads((OUT / "tools.json").read_text())
    rows = [json.loads(l) for l in DATA.read_text().splitlines() if l.strip()]
    print(f"loaded {len(rows)} accepted samples | student template: {MODEL}")

    pairs = []
    skipped = 0
    tok_lens = []
    for sample in rows:
        msgs, assistant = to_messages(sample)
        kwargs = dict(tools=tools, tokenize=False, enable_thinking=False)
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, **kwargs)
        full = tok.apply_chat_template(msgs + [assistant], **kwargs)
        if not full.startswith(prompt):
            # Template idiosyncrasy (e.g. generation scaffold not a strict
            # prefix) — surface loudly rather than train on garbage.
            skipped += 1
            if skipped <= 3:
                print(f"  PREFIX MISMATCH on {sample['id']} — inspect template", file=sys.stderr)
            continue
        completion = full[len(prompt):]
        if not completion.strip():
            skipped += 1
            continue
        pairs.append({"prompt": prompt, "completion": completion})
        tok_lens.append(len(tok(full).input_ids))

    random.Random(SEED).shuffle(pairs)
    n_valid = max(8, int(len(pairs) * VALID_FRACTION))
    valid, train = pairs[:n_valid], pairs[n_valid:]

    TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    for name, split in (("train", train), ("valid", valid)):
        with open(TRAIN_DIR / f"{name}.jsonl", "w") as f:
            for p in split:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")

    tok_lens.sort()
    p95 = tok_lens[int(len(tok_lens) * 0.95)] if tok_lens else 0
    print(
        f"train {len(train)} / valid {len(valid)} → {TRAIN_DIR}\n"
        f"token length: median {tok_lens[len(tok_lens)//2]}, p95 {p95}, max {tok_lens[-1]}"
        f"{f' | skipped {skipped}' if skipped else ''}"
    )
    if p95 > 2560:
        print(f"WARNING: p95 {p95} exceeds max_seq_length 2560 in train-lora.yaml", file=sys.stderr)

if __name__ == "__main__":
    main()
