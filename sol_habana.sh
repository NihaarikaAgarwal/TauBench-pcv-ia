#!/bin/bash
#SBATCH --partition=gaudi
#SBATCH --qos=class_gaudi
#SBATCH --gres=gpu:hl225:4
#SBATCH --cpus-per-task=64
#SBATCH --mem=160G
#SBATCH --time=04:30:00
#SBATCH --account=class_cse59827694spring2026
#SBATCH --output=logs/%j_%x.log
#SBATCH --error=logs/%j_%x.err

set -euo pipefail

# --- Models ---
AGENT_MODEL="Qwen/Qwen3-4B"
USER_MODEL="Qwen/Qwen3-32B"

###############################################################################
# --- Params ---
#MAX_LEN=32768
MAX_LEN=16384
DTYPE="bfloat16"
USER_QUANT=""
GPU_UTIL=0.57
GPU_UTIL_1=0.43
MAX_CONCURRENCY=2
NUM_TRIALS=1

echo "Running on hl225:4 (Gaudi) with:"
echo "  AGENT_MODEL=$AGENT_MODEL"
echo "  USER_MODEL=$USER_MODEL"
echo "  MAX_LEN=$MAX_LEN  DTYPE=$DTYPE  USER_QUANT=$USER_QUANT GPU_UTIL=$GPU_UTIL"
echo "  MAX_CONCURRENCY=$MAX_CONCURRENCY  NUM_TRIALS=$NUM_TRIALS"
echo "gpu:hl225:4"
echo "cpus-per-task=32"
echo "mem=120G"
echo "time=05:00:00"
###############################################################################
# --- Arguments ---
DOMAIN="${1:?DOMAIN missing}"
AGENT_STRATEGY="${2:?AGENT_STRATEGY missing}"
START_IDX="${3:?START_IDX missing}"
END_IDX="${4:?END_IDX missing}"

echo "Job: Domain=$DOMAIN | Strategy=$AGENT_STRATEGY | Tasks $START_IDX to $END_IDX"

###############################################################################
# Always run from where you submitted the job (important for scratch repo)
cd "${SLURM_SUBMIT_DIR:-$PWD}"

# Ensure Slurm log dir exists in the repo working directory
mkdir -p logs results

echo "Node: $(hostname)"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-}"
echo "HABANA_VISIBLE_DEVICES=${HABANA_VISIBLE_DEVICES:-<unset>}"
hl-smi || true

###############################################################################
# --- Conda (TauBench runner in host env) ---
source ~/miniconda3/etc/profile.d/conda.sh
conda activate taubench-py312
python -V

###############################################################################
# ---- Put caches on scratch (NOT $HOME) ----
SCRATCH_BASE="${SLURM_TMPDIR:-${SCRATCH:-/scratch/$USER}}"
JOB_ID="${SLURM_JOB_ID:-local}"
TB_SCRATCH="$SCRATCH_BASE/taubench/$JOB_ID"

mkdir -p "$TB_SCRATCH"/{hf,vllm,torch,xdg,tmp,logs,habana_logs_user,habana_logs_agent}

HABANA_LOGS_USER="$TB_SCRATCH/habana_logs_user"
HABANA_LOGS_AGENT="$TB_SCRATCH/habana_logs_agent"
chmod 700 "$HABANA_LOGS_USER" "$HABANA_LOGS_AGENT" || true

export HF_HOME="$TB_SCRATCH/hf"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export HF_HUB_DISABLE_TELEMETRY=1

export VLLM_CACHE_ROOT="$TB_SCRATCH/vllm"
export TORCHINDUCTOR_CACHE_DIR="$TB_SCRATCH/torch"
export XDG_CACHE_HOME="$TB_SCRATCH/xdg"
export TMPDIR="$TB_SCRATCH/tmp"

# Optional: HF auth passthrough
export HUGGINGFACE_HUB_TOKEN="${HUGGINGFACE_HUB_TOKEN:-${HF_TOKEN:-}}"

# Redirect Habana logs away from /var/log on host, then bind into container
export HABANA_LOGS_DIR="$TB_SCRATCH/habana_logs"

# If you see NCCL/network weirdness on clusters:
export NCCL_P2P_DISABLE=1
export NCCL_IB_DISABLE=1

###############################################################################
# --- Container ---
#module load apptainer

CONTAINER="/data/sse/gaudi/containers/vllm-gaudi.sif"

# Pass caches into container
export APPTAINERENV_HF_HOME="$HF_HOME"
export APPTAINERENV_HUGGINGFACE_HUB_CACHE="$HUGGINGFACE_HUB_CACHE"
export APPTAINERENV_TRANSFORMERS_CACHE="$TRANSFORMERS_CACHE"
export APPTAINERENV_VLLM_CACHE_ROOT="$VLLM_CACHE_ROOT"
export APPTAINERENV_XDG_CACHE_HOME="$XDG_CACHE_HOME"
export APPTAINERENV_TMPDIR="$TMPDIR"
export APPTAINERENV_HABANA_LOGS_DIR="/var/log/habana_logs"
export APPTAINERENV_VLLM_SKIP_WARMUP=true
export APPTAINERENV_VLLM_ENABLE_EXPERIMENTAL_FLAGS=1

###############################################################################
# --- Ports (2 distinct ports) ---

# Pick ports that are actually free (avoids collisions with other jobs on same node)
find_free_port() {
  python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
"
}
USER_PORT=$(find_free_port)
AGENT_PORT=$(find_free_port)

echo "Ports: USER=$USER_PORT, AGENT=$AGENT_PORT"

###############################################################################
# --- HPU split ---
# Use SLURM-assigned devices if available, otherwise default to 0,1,2,3.
HPU_USER="${HABANA_VISIBLE_DEVICES:+${HABANA_VISIBLE_DEVICES%%,*}}"
# Simple default: USER gets HPU 0,1 — AGENT gets HPU 2,3
HPU_USER=0,1
HPU_AGENT=2,3
echo "Using HPU_USER=$HPU_USER (USER port $USER_PORT, TP=2), HPU_AGENT=$HPU_AGENT (AGENT port $AGENT_PORT, TP=2)"
echo "Scratch: $TB_SCRATCH"

###############################################################################

# --- Cleanup handler ---
cleanup() {
  echo "[CLEANUP] $(date) Stopping vLLM..."
  echo "USER_PID=${USER_PID:-} AGENT_PID=${AGENT_PID:-}" | tee -a "$TB_SCRATCH/logs/${JOB_ID}_cleanup.log"
  kill "${AGENT_PID:-}" "${USER_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT


###############################################################################

  # SERVER 1: USER  Simulator
  # Important: set HABANA_VISIBLE_DEVICES for THIS process

echo "Starting USER vLLM: $USER_MODEL on GPU=$HPU_USER port $USER_PORT..."
(
HABANA_VISIBLE_DEVICES="$HPU_USER" \
APPTAINERENV_HABANA_VISIBLE_DEVICES="$HPU_USER" \
  apptainer exec \
    --bind /scratch:/scratch \
    --bind /data:/data \
    --bind "$HABANA_LOGS_USER:/var/log/habana_logs" \
    --env HABANA_LOGS=/var/log/habana_logs \
    --env HABANA_LOGS_DIR=/var/log/habana_logs \
    --env HABANA_VISIBLE_DEVICES="$HPU_USER" \
    "$CONTAINER" \
    vllm serve "$USER_MODEL" \
      --device hpu \
      --host 127.0.0.1 \
      --port "$USER_PORT" \
      --dtype "$DTYPE" \
      --block-size 128 \
      --max-model-len "$MAX_LEN" \
      --gpu-memory-utilization "$GPU_UTIL" \
      --tensor-parallel-size 2 \
      --trust-remote-code \
)      > "$TB_SCRATCH/logs/${JOB_ID}_vllm_user.log" 2>&1 &
USER_PID=$!

###############################################################################
# --- Wait helper ---
wait_for_model() {
  local port="$1"
  local model="$2"
  local name="$3"
  echo "Waiting for $name server on port $port to expose model id: $model ..."
  if ! timeout 1800 bash -c "
    until curl -s http://127.0.0.1:${port}/v1/models | grep -q '\"id\":\"${model}\"'; do
      sleep 5
    done
  "; then
    echo "$name server not ready (OR) $model missing"
    exit 1
  fi

  echo "$name model is ready"
  curl -s "http://127.0.0.1:${port}/v1/models" | head -c 1000; echo
}

# Wait for USER to be fully ready before starting AGENT
# (avoids Habana device init race between two TP=2 containers)
wait_for_model "$USER_PORT"  "$USER_MODEL"  "USER"

###############################################################################

  # SERVER 2: AGENT Simulator
  # Important: set HABANA_VISIBLE_DEVICES for THIS process

echo "Starting AGENT vLLM: $AGENT_MODEL on GPU=$HPU_AGENT port $AGENT_PORT..."
(
HABANA_VISIBLE_DEVICES="$HPU_AGENT" \
APPTAINERENV_HABANA_VISIBLE_DEVICES="$HPU_AGENT" \
  apptainer exec \
    --bind /scratch:/scratch \
    --bind /data:/data \
    --bind "$HABANA_LOGS_AGENT:/var/log/habana_logs" \
    --env HABANA_LOGS=/var/log/habana_logs \
    --env HABANA_LOGS_DIR=/var/log/habana_logs \
    --env HABANA_VISIBLE_DEVICES="$HPU_AGENT" \
    "$CONTAINER" \
    vllm serve "$AGENT_MODEL" \
      --device hpu \
      --host 127.0.0.1 \
      --port "$AGENT_PORT" \
      --dtype "$DTYPE" \
      --block-size 128 \
      --max-model-len "$MAX_LEN" \
      --gpu-memory-utilization "$GPU_UTIL_1" \
      --tensor-parallel-size 1 \
      --enable-auto-tool-choice \
      --tool-call-parser hermes \
      --trust-remote-code \
)      > "$TB_SCRATCH/logs/${JOB_ID}_vllm_agent.log" 2>&1 &
AGENT_PID=$!

wait_for_model "$AGENT_PORT" "$AGENT_MODEL" "AGENT"

echo "Both servers ready!"

# Sanitize model names for filesystem (replace / and \ with _)
AGENT_MODEL_TAG="${AGENT_MODEL//[\/\\]/_}"
USER_MODEL_TAG="${USER_MODEL//[\/\\]/_}"

OUT_DIR="$(pwd)/results/${DOMAIN}_${AGENT_STRATEGY}_agent_${AGENT_MODEL_TAG}_user_${USER_MODEL_TAG}"

mkdir -p "$OUT_DIR"

###############################################################################
# RUN BENCHMARK
###############################################################################

# CRITICAL: Point the API bases to the correct ports
export OPENAI_API_BASE="http://127.0.0.1:${AGENT_PORT}/v1"
export USER_MODEL_API_BASE="http://127.0.0.1:${USER_PORT}/v1"
export OPENAI_API_KEY="EMPTY"

#echo "****************************************************************"
#echo "USER_MODEL_API_BASE=$USER_MODEL_API_BASE"
#echo "USER /models:"; curl -s "${USER_MODEL_API_BASE%/}/models" | head -c 300; echo
#echo "****************************************************************"
#echo "OPENAI_API_BASE=$OPENAI_API_BASE"
#echo "AGENT /models:"; curl -s "${OPENAI_API_BASE%/}/models" | head -c 300; echo
#echo "****************************************************************"

echo "τ-bench args sanity check:"
echo "  AGENT_MODEL=$AGENT_MODEL"
echo "  USER_MODEL=$USER_MODEL"
echo "  OUT_DIR=$OUT_DIR"
env | egrep "OPENAI_API_BASE|USER_MODEL_API_BASE" || true

echo "Running benchmark..."
python run.py \
  --agent-strategy "$AGENT_STRATEGY" \
  --env "$DOMAIN" \
  --model "$AGENT_MODEL" \
  --model-provider openai \
  --user-model "$USER_MODEL" \
  --user-model-provider openai \
  --user-strategy llm \
  --max-concurrency "$MAX_CONCURRENCY" \
  --num-trials "$NUM_TRIALS" \
  --temperature 0.7 \
  --start-index "$START_IDX" \
  --end-index "$END_IDX" \
  --log-dir "$OUT_DIR" \
  > "$TB_SCRATCH/logs/${JOB_ID}_taubench.log" 2>&1

###############################################################################
# Copy logs back into repo logs/ for convenience
cp -f "$TB_SCRATCH/logs/"* "logs/" 2>/dev/null || true

echo "Done."
echo "Results: $OUT_DIR"
echo "Repo logs:"
echo "  logs/${JOB_ID}_taubench.log"
echo "  logs/${JOB_ID}_vllm_agent.log"
echo "  logs/${JOB_ID}_vllm_user.log"
echo "Slurm logs:"
echo "  logs/%j_%x.log"
echo "  logs/%j_%x.err"