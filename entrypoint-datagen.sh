#!/usr/bin/env bash
# Phase A (public-repo, STOCK vllm/vllm-openai image): serve the Gemma 4
# teacher via vLLM, run the SELF-CONTAINED datagen bundle against it, upload
# the dataset to a private HF repo. No GitHub token (public clone), no
# monorepo/pnpm (the bundle is standalone Node). Env: HF_TOKEN, DATA_REPO,
# TEACHER_MODEL, REPO_ROOT(=/app/repo).
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/app/repo}"
TEACHER_MODEL="${TEACHER_MODEL:-google/gemma-4-26B-A4B-it}"
DATA_REPO="${DATA_REPO:?set DATA_REPO (private HF dataset repo)}"
: "${HF_TOKEN:?set HF_TOKEN (write)}"
cd "$REPO_ROOT"

echo "── install Node (stock vLLM image is python-only) ──"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y --no-install-recommends nodejs
fi

echo "── start vLLM teacher ($TEACHER_MODEL) ──"
vllm serve "$TEACHER_MODEL" \
  --port 8000 --gpu-memory-utilization 0.92 --max-model-len 4096 \
  --enable-auto-tool-choice --tool-call-parser gemma4 \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  > /tmp/vllm.log 2>&1 &
VLLM_PID=$!

echo "── wait for teacher health (≤20min cold start incl. weight download) ──"
for i in $(seq 1 240); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then echo "  ready"; break; fi
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then echo "vLLM died:"; tail -40 /tmp/vllm.log; exit 1; fi
  sleep 5
done

echo "── datagen (standalone bundle, concurrency 1) ──"
export TEACHER_BASE_URL="http://localhost:8000/v1" TEACHER_MODEL
node datagen.bundle.mjs --langs all --vary-squads 3 --per-case 1 --retries 2 --concurrency 1
# writes ./out/teacher-data.jsonl (next to the bundle)

echo "── upload dataset → $DATA_REPO ──"
python3 -m pip install -q "huggingface_hub>=0.27"
# Upload must NOT crash the container: the ~1h datagen output is precious. On any
# failure (perms/namespace/network) keep the container alive so the file can be
# recovered via `provider shell` or re-uploaded — never crash-loop the datagen.
set +e
python3 - <<PY
from huggingface_hub import HfApi
import os, sys
try:
    api = HfApi(token=os.environ["HF_TOKEN"])
    who = api.whoami()
    print("HF token account:", who.get("name"), "| orgs:",
          [o.get("name") for o in who.get("orgs", [])])
    api.create_repo("$DATA_REPO", repo_type="dataset", private=False, exist_ok=True)
    api.upload_file(path_or_fileobj="out/teacher-data.jsonl",
                    path_in_repo="teacher-data.jsonl",
                    repo_id="$DATA_REPO", repo_type="dataset")
    print("UPLOAD_OK → $DATA_REPO")
except Exception as e:
    print("UPLOAD_FAILED:", repr(e), file=sys.stderr)
    sys.exit(3)
PY
UP=$?
set -e
if [ "$UP" != "0" ]; then
  echo "── UPLOAD_FAILED (rc=$UP): data is safe at $PWD/out/teacher-data.jsonl."
  echo "   Fix HF_TOKEN perms / DATA_REPO namespace, then re-upload via shell — no re-run needed."
fi

echo "── DATAGEN_DONE (upload rc=$UP) — sleeping so logs stay inspectable; close the lease to stop billing ──"
kill "$VLLM_PID" 2>/dev/null || true
sleep infinity
