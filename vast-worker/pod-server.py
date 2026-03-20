#!/usr/bin/env python3
"""
RunPod Pod HTTP API Server
Exposes endpoints for remote command execution, model setup, and video generation.
Access via: https://<podId>-8000.proxy.runpod.net/

Starts automatically via dockerArgs when pod is created.
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="RunPod Avatar Test Server")

# Job tracking
jobs = {}

# ─── Models ───

class ExecRequest(BaseModel):
    command: str
    timeout: int = 300  # 5 min default

class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    code: int
    duration: float

class GenerateRequest(BaseModel):
    model: str  # "multitalk", "infinitetalk", "omniavatar"
    image_url: str  # URL to download avatar image
    audio_url: str  # URL to download audio file
    prompt: Optional[str] = "A person is speaking naturally into a camera. Close-up shot with warm studio lighting."

class JobStatus(BaseModel):
    job_id: str
    status: str  # queued, downloading, generating, uploading, completed, failed
    progress: Optional[str] = None
    duration: Optional[float] = None
    output_file: Optional[str] = None
    error: Optional[str] = None

# ─── Endpoints ───

@app.get("/health")
def health():
    """Check server and GPU status."""
    gpu = "unknown"
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        pass

    disk = "unknown"
    try:
        disk = subprocess.run(
            ["df", "-h", "/workspace"], capture_output=True, text=True, timeout=5
        ).stdout.strip().split("\n")[-1]
    except Exception:
        pass

    return {
        "status": "ok",
        "gpu": gpu,
        "disk": disk,
        "python": sys.version.split()[0],
        "jobs_active": sum(1 for j in jobs.values() if j["status"] in ("downloading", "generating")),
    }


@app.post("/exec", response_model=ExecResponse)
def exec_command(req: ExecRequest):
    """Execute a shell command and return output. Max 90s for proxy timeout."""
    start = time.time()
    try:
        result = subprocess.run(
            req.command, shell=True,
            capture_output=True, text=True,
            timeout=min(req.timeout, 90),  # RunPod proxy has 100s timeout
            cwd="/workspace",
        )
        return ExecResponse(
            stdout=result.stdout[-10000:],  # Truncate to last 10KB
            stderr=result.stderr[-5000:],
            code=result.returncode,
            duration=time.time() - start,
        )
    except subprocess.TimeoutExpired:
        return ExecResponse(
            stdout="", stderr="Command timed out", code=-1,
            duration=time.time() - start,
        )


@app.post("/exec-bg")
def exec_background(req: ExecRequest, bg: BackgroundTasks):
    """Execute a long-running command in background. Poll /job/{id} for status."""
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "running", "command": req.command, "start": time.time()}
    bg.add_task(_run_bg_command, job_id, req.command, req.timeout)
    return {"job_id": job_id, "poll": f"/job/{job_id}"}


@app.post("/generate")
def generate_video(req: GenerateRequest, bg: BackgroundTasks):
    """Start video generation job. Poll /job/{id} for status."""
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "model": req.model, "start": time.time()}
    bg.add_task(_run_generation, job_id, req)
    return {"job_id": job_id, "poll": f"/job/{job_id}"}


@app.get("/job/{job_id}")
def get_job(job_id: str):
    """Get status of a background job."""
    job = jobs.get(job_id)
    if not job:
        return {"status": "not_found"}
    return job


@app.get("/jobs")
def list_jobs():
    """List all jobs."""
    return {k: {"status": v["status"], "model": v.get("model", "exec")} for k, v in jobs.items()}


@app.get("/files")
def list_files(path: str = "/workspace/outputs"):
    """List files in a directory."""
    try:
        p = Path(path)
        files = []
        for f in sorted(p.iterdir()):
            stat = f.stat()
            files.append({
                "name": f.name,
                "size": stat.st_size,
                "size_mb": round(stat.st_size / 1024 / 1024, 1),
                "is_dir": f.is_dir(),
            })
        return {"path": str(p), "files": files}
    except Exception as e:
        return {"error": str(e)}


@app.get("/download/{filename}")
def download_file(filename: str, dir: str = "/workspace/outputs"):
    """Download a file from the pod."""
    filepath = Path(dir) / filename
    if not filepath.exists():
        return {"error": f"File not found: {filepath}"}
    return FileResponse(filepath, filename=filename)


# ─── Background Tasks ───

def _run_bg_command(job_id: str, command: str, timeout: int):
    """Run a command in background and update job status."""
    try:
        result = subprocess.run(
            command, shell=True,
            capture_output=True, text=True,
            timeout=timeout,
            cwd="/workspace",
        )
        jobs[job_id] = {
            **jobs[job_id],
            "status": "completed" if result.returncode == 0 else "failed",
            "stdout": result.stdout[-10000:],
            "stderr": result.stderr[-5000:],
            "code": result.returncode,
            "duration": time.time() - jobs[job_id]["start"],
        }
    except subprocess.TimeoutExpired:
        jobs[job_id] = {**jobs[job_id], "status": "failed", "error": f"Timeout ({timeout}s)"}
    except Exception as e:
        jobs[job_id] = {**jobs[job_id], "status": "failed", "error": str(e)}


def _run_generation(job_id: str, req: GenerateRequest):
    """Run full generation pipeline: download inputs → generate → report."""
    try:
        test_dir = "/workspace/test"
        output_dir = "/workspace/outputs"
        os.makedirs(test_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # Download input files
        jobs[job_id]["status"] = "downloading"
        subprocess.run(f"wget -q -O {test_dir}/avatar.jpg '{req.image_url}'", shell=True, check=True, timeout=60)
        subprocess.run(f"wget -q -O {test_dir}/audio_input '{req.audio_url}'", shell=True, check=True, timeout=60)

        # Convert audio to WAV 16kHz
        subprocess.run(
            f"ffmpeg -y -i {test_dir}/audio_input -ar 16000 -ac 1 {test_dir}/test-audio.wav",
            shell=True, check=True, capture_output=True, timeout=30
        )

        jobs[job_id]["status"] = "generating"
        jobs[job_id]["progress"] = "starting model..."

        if req.model == "multitalk":
            _run_multitalk(job_id, req, test_dir, output_dir)
        elif req.model == "infinitetalk":
            _run_infinitetalk(job_id, req, test_dir, output_dir)
        elif req.model == "omniavatar":
            _run_omniavatar(job_id, req, test_dir, output_dir)
        else:
            raise ValueError(f"Unknown model: {req.model}")

        # Find output
        import glob
        outputs = sorted(glob.glob(f"{output_dir}/{req.model}-test*"), key=os.path.getmtime, reverse=True)
        if outputs:
            size_mb = os.path.getsize(outputs[0]) / 1024 / 1024
            jobs[job_id] = {
                **jobs[job_id],
                "status": "completed",
                "output_file": outputs[0],
                "size_mb": round(size_mb, 1),
                "duration": time.time() - jobs[job_id]["start"],
                "download": f"/download/{os.path.basename(outputs[0])}",
            }
        else:
            jobs[job_id] = {**jobs[job_id], "status": "failed", "error": "No output file generated"}

    except Exception as e:
        import traceback
        jobs[job_id] = {
            **jobs[job_id],
            "status": "failed",
            "error": str(e),
            "traceback": traceback.format_exc()[-3000:],
            "duration": time.time() - jobs[job_id]["start"],
        }


def _run_multitalk(job_id, req, test_dir, output_dir):
    """Run MultiTalk generation."""
    import json
    # Kill any zombie processes first
    subprocess.run("pkill -9 -f generate_multitalk", shell=True, capture_output=True)
    time.sleep(2)

    input_json = f"{test_dir}/multitalk_input.json"
    with open(input_json, "w") as f:
        json.dump({
            "prompt": req.prompt,
            "cond_image": f"{test_dir}/avatar.jpg",
            "cond_audio": {"person1": f"{test_dir}/test-audio.wav"},
        }, f)

    weights = "/workspace/weights"
    fusionx = f"{weights}/fusionx/FusionX_LoRa/Wan2.1_I2V_14B_FusionX_LoRA.safetensors"
    # Use FusionX if available, otherwise fall back to no LoRA
    lora_args = ""
    if os.path.exists(fusionx):
        lora_args = (
            f"--lora_dir {fusionx} --lora_scale 1.0 "
            f"--sample_text_guide_scale 1 --sample_audio_guide_scale 2 "
        )
    cmd = (
        f"cd /workspace/MultiTalk && PYTHONUNBUFFERED=1 python3 generate_multitalk.py "
        f"--ckpt_dir {weights}/Wan2.1-I2V-14B-480P "
        f"--wav2vec_dir {weights}/chinese-wav2vec2-base "
        f"--input_json {input_json} "
        f"--sample_steps 8 --mode streaming "
        f"--offload_model False "
        f"{lora_args}"
        f"--use_teacache --teacache_thresh 0.3 "
        f"--save_file {output_dir}/multitalk-test"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3600, cwd="/workspace")
    if result.returncode != 0:
        raise RuntimeError(f"MultiTalk failed:\n{result.stderr[-2000:]}")


def _run_infinitetalk(job_id, req, test_dir, output_dir):
    """Run InfiniteTalk generation."""
    import json
    input_json = f"{test_dir}/infinitetalk_input.json"
    with open(input_json, "w") as f:
        json.dump({
            "prompt": req.prompt,
            "cond_video": f"{test_dir}/avatar.jpg",
            "cond_audio": {"person1": f"{test_dir}/test-audio.wav"},
        }, f)

    weights = "/workspace/weights"
    adapter = f"{weights}/InfiniteTalk/single/infinitetalk.safetensors"
    cmd = (
        f"cd /workspace/InfiniteTalk && PYTHONUNBUFFERED=1 python3 generate_infinitetalk.py "
        f"--ckpt_dir {weights}/Wan2.1-I2V-14B-480P "
        f"--wav2vec_dir {weights}/chinese-wav2vec2-base "
        f"--infinitetalk_dir {adapter} "
        f"--input_json {input_json} "
        f"--size infinitetalk-480 --sample_steps 40 "
        f"--mode streaming --motion_frame 9 "
        f"--use_teacache --teacache_thresh 0.3 "
        f"--save_file {output_dir}/infinitetalk-test"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1800, cwd="/workspace")
    if result.returncode != 0:
        raise RuntimeError(f"InfiniteTalk failed:\n{result.stderr[-2000:]}")


def _run_omniavatar(job_id, req, test_dir, output_dir):
    """Run OmniAvatar generation."""
    input_file = f"{test_dir}/omniavatar_input.txt"
    with open(input_file, "w") as f:
        f.write(f"{req.prompt}@@{test_dir}/avatar.jpg@@{test_dir}/audio_input\n")

    cmd = (
        f"cd /workspace/OmniAvatar && PYTHONUNBUFFERED=1 "
        f"torchrun --standalone --nproc_per_node=1 scripts/inference.py "
        f"--config configs/inference.yaml "
        f"--input_file {input_file}"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1800, cwd="/workspace")
    if result.returncode != 0:
        raise RuntimeError(f"OmniAvatar failed:\n{result.stderr[-2000:]}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
