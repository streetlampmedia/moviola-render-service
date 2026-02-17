import os
import uuid
import json
import time
import shutil
import tempfile
import subprocess
from typing import Optional, List, Literal, Dict, Any

import boto3
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Moviola Render Service", version="0.1.0")

Status = Literal["queued", "processing", "uploading", "completed", "failed"]

# In-memory job store (MVP). Later: Redis/DB.
JOBS: Dict[str, Dict[str, Any]] = {}

# ---------- Models ----------
class TimelineClip(BaseModel):
    type: Literal["clip"] = "clip"
    src: str  # signed URL
    in_: float = Field(..., alias="in")
    out: float

class AudioSpec(BaseModel):
    normalize: bool = True

class CaptionsSpec(BaseModel):
    srt_url: Optional[str] = None
    burn_in: bool = False  # MVP: not implemented

class OutputSpec(BaseModel):
    format: Literal["mp4"] = "mp4"
    width: int = 1920
    height: int = 1080

class EditPlan(BaseModel):
    timeline: List[TimelineClip]
    audio: AudioSpec = AudioSpec()
    captions: CaptionsSpec = CaptionsSpec()
    output: OutputSpec = OutputSpec()

class RenderRequest(BaseModel):
    callback_url: Optional[str] = None
    callback_secret: Optional[str] = None
    job_meta: Optional[dict] = None
    edit_plan: EditPlan

class RenderResponse(BaseModel):
    job_id: str
    status: Status

class RenderStatusResponse(BaseModel):
    job_id: str
    status: Status
    progress: float = 0.0
    output_url: Optional[str] = None
    error: Optional[str] = None

# ---------- Helpers ----------
def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

def get_s3_client():
    # Cloudflare R2 is S3-compatible
    endpoint = env_required("R2_ENDPOINT")  # e.g. https://<accountid>.r2.cloudflarestorage.com
    access_key = env_required("R2_ACCESS_KEY_ID")
    secret_key = env_required("R2_SECRET_ACCESS_KEY")
    region = os.getenv("R2_REGION", "auto")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )

def upload_to_r2(local_path: str, key: str) -> str:
    bucket = env_required("R2_BUCKET")
    public_base = os.getenv("R2_PUBLIC_BASE_URL")  # optional (e.g. https://cdn.yourdomain.com)
    s3 = get_s3_client()
    s3.upload_file(local_path, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    if public_base:
        return f"{public_base.rstrip('/')}/{key}"
    # Fallback: return s3-style URL (may not be publicly accessible)
    return f"s3://{bucket}/{key}"

def download_file(url: str, dest_path: str):
    # Simple streaming download; works with signed URLs.
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

def run_ffmpeg_concat(trims: List[dict], out_path: str) -> None:
    """
    MVP strategy:
    - For each segment: re-encode to a common format (H.264/AAC), 30fps-ish default, +faststart.
    - Concatenate via concat demuxer.
    """
    tmpdir = os.path.dirname(out_path)
    segment_paths = []

    for idx, seg in enumerate(trims):
        src = seg["src"]
        ss = seg["in"]
        to = seg["out"]

        in_path = os.path.join(tmpdir, f"input_{idx}.mp4")
        seg_path = os.path.join(tmpdir, f"seg_{idx}.mp4")

        download_file(src, in_path)

        # Reliable trim: re-encode
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(ss),
            "-to", str(to),
            "-i", in_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            seg_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        segment_paths.append(seg_path)

    # Create concat list
    list_path = os.path.join(tmpdir, "concat.txt")
    with open(list_path, "w") as f:
        for p in segment_paths:
            f.write(f"file '{p}'\n")

    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        "-movflags", "+faststart",
        out_path
    ]
    subprocess.run(cmd_concat, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def post_callback(callback_url: str, callback_secret: Optional[str], payload: dict):
    headers = {}
    if callback_secret:
        headers["X-RENDER-CALLBACK-SECRET"] = callback_secret
    try:
        requests.post(callback_url, json=payload, headers=headers, timeout=30).raise_for_status()
    except Exception:
        # Non-fatal for MVP
        pass

def set_job(job_id: str, **updates):
    JOBS[job_id].update(updates)
    JOBS[job_id]["updated_at"] = time.time()

# ---------- API ----------
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/render", response_model=RenderResponse)
def start_render(req: RenderRequest):
    if not req.edit_plan.timeline:
        raise HTTPException(400, "edit_plan.timeline is empty")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "queued",
        "progress": 0.0,
        "output_url": None,
        "error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "req": req.model_dump(by_alias=True),
    }

    # Fire-and-forget background render (MVP)
    # Railway container is long-running, so this is fine for now.
    import threading
    threading.Thread(target=_do_render, args=(job_id,), daemon=True).start()

    return {"job_id": job_id, "status": "queued"}

@app.get("/render/{job_id}", response_model=RenderStatusResponse)
def render_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job.get("progress", 0.0),
        "output_url": job.get("output_url"),
        "error": job.get("error"),
    }

# ---------- Worker ----------
def _do_render(job_id: str):
    job = JOBS[job_id]
    req = job["req"]
    callback_url = req.get("callback_url")
    callback_secret = req.get("callback_secret")

    try:
        set_job(job_id, status="processing", progress=0.05)
        if callback_url:
            post_callback(callback_url, callback_secret, {
                "external_job_id": job_id,
                "status": "processing",
                "progress": 0.05,
                "output_url": None,
                "error": None
            })

        edit_plan = req["edit_plan"]
        trims = []
        for clip in edit_plan["timeline"]:
            trims.append({"src": clip["src"], "in": clip["in"], "out": clip["out"]})

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "final.mp4")

            set_job(job_id, progress=0.35)
            def run_ffmpeg_concat(trims: List[dict], out_path: str) -> None:
    tmpdir = os.path.dirname(out_path)
    os.makedirs(tmpdir, exist_ok=True)

    # If only one segment, don't concat — just trim directly (most reliable)
    if len(trims) == 1:
        seg = trims[0]
        in_path = os.path.join(tmpdir, "input_0.mp4")
        download_file(seg["src"], in_path)

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(seg["in"]),
            "-to", str(seg["out"]),
            "-i", in_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            out_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return

    # Multiple segments: create uniform segments then concat (re-encode for reliability)
    segment_paths = []
    for idx, seg in enumerate(trims):
        in_path = os.path.join(tmpdir, f"input_{idx}.mp4")
        seg_path = os.path.join(tmpdir, f"seg_{idx}.mp4")

        download_file(seg["src"], in_path)

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(seg["in"]),
            "-to", str(seg["out"]),
            "-i", in_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            seg_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        segment_paths.append(seg_path)

    list_path = os.path.join(tmpdir, "concat.txt")
    with open(list_path, "w") as f:
        for p in segment_paths:
            f.write(f"file {p}\n")

    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out_path
    ]
    subprocess.run(cmd_concat, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


            set_job(job_id, status="uploading", progress=0.8)
            if callback_url:
                post_callback(callback_url, callback_secret, {
                    "external_job_id": job_id,
                    "status": "uploading",
                    "progress": 0.8,
                    "output_url": None,
                    "error": None
                })

            # Upload
            key = f"renders/{job_id}.mp4"
            output_url = upload_to_r2(out_path, key)

        set_job(job_id, status="completed", progress=1.0, output_url=output_url)
        if callback_url:
            post_callback(callback_url, callback_secret, {
                "external_job_id": job_id,
                "status": "completed",
                "progress": 1.0,
                "output_url": output_url,
                "error": None
            })

    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", errors="ignore")[-2000:]
        set_job(job_id, status="failed", error=err)
        if callback_url:
            post_callback(callback_url, callback_secret, {
                "external_job_id": job_id,
                "status": "failed",
                "progress": job.get("progress", 0.0),
                "output_url": None,
                "error": err
            })
    except Exception as e:
        err = str(e)
        set_job(job_id, status="failed", error=err)
        if callback_url:
            post_callback(callback_url, callback_secret, {
                "external_job_id": job_id,
                "status": "failed",
                "progress": job.get("progress", 0.0),
                "output_url": None,
                "error": err
            })
