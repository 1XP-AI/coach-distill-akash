#!/usr/bin/env bash
# Phase B (public-repo, STOCK pytorch/pytorch image): pull the Phase-A dataset,
# render Qwen training pairs, LoRA-train the student, push the adapter to HF.
# No GitHub token (public clone). Env: HF_TOKEN, DATA_REPO (Phase-A output),
# HUB_REPO, REPO_ROOT(=/app/repo).
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/app/repo}"
DATA_REPO="${DATA_REPO:?set DATA_REPO (Phase-A HF dataset repo)}"
: "${HF_TOKEN:?set HF_TOKEN}"
cd "$REPO_ROOT"

echo "── install training deps (stock pytorch image) ──"
pip install --no-cache-dir \
    "transformers>=4.57" "peft>=0.14" "accelerate>=1.3" "datasets>=3.2" \
    "huggingface_hub>=0.27" "flash-linear-attention" "causal-conv1d" \
  || pip install --no-cache-dir \
    transformers peft accelerate datasets huggingface_hub

echo "── pull Phase-A dataset ($DATA_REPO) → out/ ──"
mkdir -p out
python3 - <<PY
from huggingface_hub import hf_hub_download
import os, shutil
p = hf_hub_download(repo_id="$DATA_REPO", repo_type="dataset",
                    filename="teacher-data.jsonl", token=os.environ["HF_TOKEN"])
shutil.copy(p, "out/teacher-data.jsonl")
print("pulled teacher-data.jsonl")
PY
cp tools.json out/tools.json   # static tool schema (committed in this repo)

echo "── render Qwen training pairs ──"
python3 prepare-training.py    # reads out/{teacher-data.jsonl,tools.json} → out/train

echo "── LoRA train (adapter → $HUB_REPO) ──"
# Train+push must NOT crash-loop the container on a push failure — a ~1h train
# is precious. On failure keep the container alive (adapter is at /output/adapters,
# recoverable via shell).
set +e
DATA_DIR="$REPO_ROOT/out/train" OUTPUT_DIR=/output/adapters \
  python3 train_lora_cuda.py
TR=$?
set -e
if [ "$TR" != "0" ]; then
  echo "── TRAIN/PUSH FAILED (rc=$TR): trained adapter (if any) at /output/adapters."
  echo "   Fix HF perms/HUB_REPO, then re-push via shell — no re-train needed."
fi

echo "── TRAIN_DONE (rc=$TR) — sleeping so logs stay inspectable; close the lease to stop billing ──"
sleep infinity
