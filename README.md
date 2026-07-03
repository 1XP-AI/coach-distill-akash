# coach-distill-akash

**World Soccer 2026 AI 헤드코치**를 브라우저 로컬 모델(Qwen3.5-0.8B, WebGPU)로 만들기
위한 증류 파이프라인의 **Akash 배포용 self-contained 번들**입니다. 로컬 맥이 두 번
다운된 뒤 GPU 작업을 Akash A100로 옮겼고, 이 repo는 **public**이라 Akash 컨테이너가
GitHub 토큰 없이 clone합니다. (비밀정보 없음 — 토큰/키는 Console env로만 주입.)

## 2단계 (둘 다 https://air.akash.network 에서 SDL 붙여넣기)

- **Phase A — `datagen.yaml`**: 스톡 vLLM 이미지 → Gemma 4 26B-A4B 교사 서빙
  (`--tool-call-parser gemma4`, thinking off) → `datagen.bundle.mjs`(standalone Node)로
  8언어 교사 데이터 생성 → private HF dataset repo(`1xp/coach-distill-data`)에 업로드.
- **Phase B — `deploy.yaml`**: 스톡 pytorch 이미지 → HF에서 데이터 pull →
  `prepare-training.py`(Qwen 챗템플릿 렌더) → `train_lora_cuda.py`(LoRA) → 어댑터를
  `1xp/coach-qwen35-0.8b-lora`로 push.

## 필요한 것

- **HF write 토큰** 하나만 (Console에서 각 배포의 `HF_TOKEN` env로 추가). GitHub 토큰 불필요.
- Akash 지갑 + AKT (A100 ≈ $0.77/hr; Phase A ~$1~1.5, Phase B ~$1).

## 파일

| 파일 | 역할 |
|---|---|
| `datagen.yaml` / `entrypoint-datagen.sh` | Phase A: vLLM 교사 + 번들 데이터젠 → HF |
| `deploy.yaml` / `entrypoint-train.sh` | Phase B: pull → prepare → LoRA → HF |
| `datagen.bundle.mjs` | standalone 데이터젠 (monorepo/pnpm/tsx 불필요, Node만) |
| `tools.json` | 코치 도구 스키마 (prepare-training 입력) |
| `prepare-training.py` | 교사 JSONL → Qwen prompt/completion 페어 (completion-only loss) |
| `train_lora_cuda.py` | HF+PEFT LoRA 학습 |

## 미검증 (첫 배포 시 확인)

- vLLM `gemma4` tool-call 파서가 구조화 `tool_calls`를 내는지 (Phase A 핵심). 로그에서
  빈 응답/JSON 텍스트면 `TEACHER_MODEL`을 `google/gemma-4-12B-it`로 교체.
- Qwen3.5-0.8B(MoE+GatedDeltaNet) CUDA 로딩 — `transformers>=4.57` + linear-attn 커널.

소스(모노레포): `1XP-AI/solana-world-soccer-2026` (private) · 브랜치 `coach-local-distill`.
