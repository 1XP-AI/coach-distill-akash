#!/usr/bin/env bash
# Phase C (public-repo, STOCK vllm image): serve the STUDENT (Qwen3.5-0.8B +
# trained LoRA) via vLLM and score it on the SAME eval grid + 5 gates as the
# teacher (the datagen bundle, pointed at the student). First-try intent-match
# rate = the student's score, comparable to the teacher's ~74%. Uploads the
# student's per-case outputs to a public HF repo for hands-off analysis.
# Env: HF_TOKEN, EVAL_REPO, ADAPTER, BASE_MODEL, REPO_ROOT.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/app/repo}"
BASE_MODEL="${BASE_MODEL:-Qwen/Qwen3.5-0.8B}"
ADAPTER="${ADAPTER:-jjangg96/coach-qwen35-0.8b-lora}"
EVAL_REPO="${EVAL_REPO:-jjangg96/coach-eval-phaseC}"
: "${HF_TOKEN:?set HF_TOKEN}"
cd "$REPO_ROOT"

echo "── install Node ──"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y --no-install-recommends nodejs
fi

echo "── serve STUDENT (base $BASE_MODEL + LoRA $ADAPTER) via vLLM ──"
# enable_thinking off (student was trained no-think). qwen tool parser decodes
# the Qwen-format <tool_call> the student emits. First unvalidated bit here.
vllm serve "$BASE_MODEL" \
  --enable-lora --max-lora-rank 16 --lora-modules "coach=$ADAPTER" \
  --port 8000 --gpu-memory-utilization 0.9 --max-model-len 4096 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  > /tmp/vllm.log 2>&1 &
VLLM_PID=$!

echo "── wait for student health (≤10min) ──"
for i in $(seq 1 120); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then echo "  ready"; break; fi
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then echo "vLLM died:"; tail -40 /tmp/vllm.log; exit 1; fi
  sleep 5
done

echo "── score student on the grid (retries 0 = raw first-try) ──"
cd "$REPO_ROOT/packages/api" 2>/dev/null || cd "$REPO_ROOT"
export TEACHER_BASE_URL="http://localhost:8000/v1" TEACHER_MODEL="coach"
# the standalone bundle lives at repo root
node "$REPO_ROOT/datagen.bundle.mjs" --langs all --vary-squads 1 --per-case 1 --retries 0 --concurrency 1

echo "── upload student outputs → $EVAL_REPO (public) ──"
set +e
python3 -m pip install -q "huggingface_hub>=0.27"
python3 - <<PY
from huggingface_hub import HfApi
import os, glob
api = HfApi(token=os.environ["HF_TOKEN"])
api.create_repo("$EVAL_REPO", repo_type="dataset", private=False, exist_ok=True)
outdir = None
for c in ("$REPO_ROOT/out", "$REPO_ROOT/packages/api/scripts/coach-distill/out", "$REPO_ROOT/scripts/coach-distill/out"):
    if os.path.isdir(c): outdir = c; break
if outdir:
    api.upload_folder(folder_path=outdir, repo_id="$EVAL_REPO", repo_type="dataset")
    print("EVAL_UPLOAD_OK →", "$EVAL_REPO", "from", outdir)
else:
    print("no out/ dir found to upload")
PY
set -e

echo "── EVAL_DONE — sleeping so logs stay inspectable; close the lease to stop billing ──"
kill "$VLLM_PID" 2>/dev/null || true
sleep infinity
