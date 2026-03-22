#!/bin/bash
# Run parallel SkyReels V3 videos on a single GPU (FP8 mode, ~13GB each)
# Each job gets its own working directory to avoid audio file conflicts
# Usage: bash run-skyreels-parallel.sh

set -e
export HF_HOME=/workspace/.cache/huggingface
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

JOBS=${1:-3}  # Number of parallel jobs (default 3)
echo "=== Starting $JOBS parallel SkyReels V3 generations ==="
echo "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader)"
echo ""

START=$(date +%s)

for i in $(seq 1 $JOBS); do
  # Create isolated working directory for each job (avoids processed_audio/ conflicts)
  WORKDIR=/workspace/skyreels-job-$i
  mkdir -p $WORKDIR
  cp -r /workspace/SkyReels-V3/* $WORKDIR/ 2>/dev/null || true

  echo "[Video $i] Starting (audio-$i.mp3)..."
  cd $WORKDIR
  python generate_video.py \
    --task_type talking_avatar \
    --model_id Skywork/SkyReels-V3-A2V-19B \
    --input_image /workspace/test/avatar.jpg \
    --input_audio /workspace/test/audio-$i.mp3 \
    --prompt "A person speaking naturally, direct eye contact, professional setting." \
    --seed $((41+i)) \
    --resolution 720P \
    --low_vram \
    > /workspace/outputs/log-$i.txt 2>&1 &
  echo "[Video $i] PID: $!"
done

echo ""
echo "All $JOBS launched. Waiting..."
wait
END=$(date +%s)

echo ""
echo "=== ALL DONE in $((END-START)) seconds ($((  (END-START)/60  )) min) ==="
echo ""
echo "Output videos:"
for i in $(seq 1 $JOBS); do
  find /workspace/skyreels-job-$i/result -name "*_with_audio.mp4" 2>/dev/null
done
echo ""
echo "Copying to /workspace/outputs/..."
for i in $(seq 1 $JOBS); do
  find /workspace/skyreels-job-$i/result -name "*_with_audio.mp4" -exec cp {} /workspace/outputs/video-$i.mp4 \; 2>/dev/null
done
ls -lh /workspace/outputs/*.mp4 2>/dev/null
