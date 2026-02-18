import os
import uuid
import time
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
    src: str  # signed/public URL
    in_: float = Field(..., alias="in")
    out: float


class AudioSpec(BaseModel):
    normalize: bool = True  # MVP: not implemented yet


class CaptionsSpec(BaseModel):
    srt_url: Optional[str] = None  # MVP: not implemented
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
    duration_seconds: Optional[float] = None
    file_size_bytes: Optional[int] = None


# ---------- Helpers ----------
def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def get_s3_client():
    endpoint = env_required("R2_ENDPOINT")  # https://<accountid>.r2.cloudflarestorage.com
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
    public_base = os.getenv("R2_PUBLIC_BASE_URL")  # e.g. https://pub-xxxxx.r2.dev
    s3 = get_s3_client()
    s3.upload_file(local_path, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    if public_base:
        return f"{public_base.rstrip('/')}/{key}"
    return f"s3://{bucket}/{key}"


def download_file(url: str, dest_path: str):
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def run_cmd(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise subprocess.CalledProcessError(
            p.returncode, cmd, output=p.stdout, stderr=p.stderr
        )


def get_duration_seconds(path: str) -> Optional[float]:
    # Uses ffprobe to extract duration; returns None if it fails.
    probe_cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    p = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        return None
    s = (p.stdout or b"").decode("utf-8", errors="ignore").strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def run_ffmpeg_concat(trims: List[dict], out_path: str) -> None:
    """
    Reliable MVP:
    - 1 clip: trim directly (no concat).
    - multi clips: trim each to normalized segments, then concat by re-encoding final.
    """
    tmpdir = os.path.dirname(out_path)
    os.makedirs(tmpdir, exist_ok=True)

    def norm_filters() -> List[str]:
        return ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-r", "30"]

    # Single clip
    if len(trims) == 1:
        seg = trims[0]
        in_path = os.path.join(tmpdir, "input_0.mp4")
        download_file(seg["src"], in_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(seg["in"]),
            "-to",
            str(seg["out"]),
            "-i",
            in_path,
            *norm_filters(),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            out_path,
        ]
        run_cmd(cmd)
        return

    # Multiple clips -> segments
    segment_paths: List[str] = []
    for idx, seg in enumerate(trims):
        in_path = os.path.join(tmpdir, f"input_{idx}.mp4")
        seg_path = os.path.join(tmpdir, f"seg_{idx}.mp4")

        download_file(seg["src"], in_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(seg["in"]),
            "-to",
            str(seg["out"]),
            "-i",
            in_path,
            *norm_filters(),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            seg_path,
        ]
        run_cmd(cmd)
        segment_paths.append(seg_path)

    list_path = os.path.join(tmpdir, "concat.txt")
    with open(list_path, "w") as f:
        for pth in segment_paths:
            f.write(f"file '{pth}'\n")

    cmd_concat = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        out_path,
    ]
    run_cmd(cmd_concat)


def post_callback(callback_url: str, callback_secret: Optional[str], payload: dict):
    headers = {}
    if callback_secret:
        headers["X-RENDER-CALLBACK-SECRET"] = callback_secret
    try:
        requests.post(
            callback_url, json=payload, headers=headers, timeout=30
        ).raise_for_status()
    except Exception:
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
        "duration_seconds": None,
        "file_size_bytes": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "req": req.model_dump(by_alias=True),
    }

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
        "duration_seconds": job.get("duration_seconds"),
        "file_size_bytes": job.get("file_size_bytes"),
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
            post_callback(
                callback_url,
                callback_secret,
                {
                    "external_job_id": job_id,
                    "status": "processing",
                    "progress": 0.05,
                    "output_url": None,
                    "error": None,
                },
            )

        edit_plan = req["edit_plan"]
        trims = [
            {"src": clip["src"], "in": clip["in"], "out": clip["out"]}
            for clip in edit_plan["timeline"]
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "final.mp4")

            set_job(job_id, progress=0.35)
            run_ffmpeg_concat(trims, out_path)

            set_job(job_id, status="uploading", progress=0.8)
            if callback_url:
                post_callback(
                    callback_url,
                    callback_secret,
                    {
                        "external_job_id": job_id,
                        "status": "uploading",
                        "progress": 0.8,
                        "output_url": None,
                        "error": None,
                    },
                )

            user_id = (req.get("job_meta") or {}).get("user_id", "demo")
            key = f"renders/{user_id}/{job_id}.mp4"

            output_url = upload_to_r2(out_path, key)
            file_size_bytes = os.path.getsize(out_path)
            duration_seconds = get_duration_seconds(out_path)

        set_job(
            job_id,
            status="completed",
            progress=1.0,
            output_url=output_url,
            file_size_bytes=file_size_bytes,
            duration_seconds=duration_seconds,
        )

        if callback_url:
            post_callback(
                callback_url,
                callback_secret,
                {
                    "external_job_id": job_id,
                    "status": "completed",
                    "progress": 1.0,
                    "output_url": output_url,
                    "file_size_bytes": file_size_bytes,
                    "duration_seconds": duration_seconds,
                    "error": None,
                },
            )

    except subprocess.CalledProcessError as e:
        raw = (
            e.stderr
            if isinstance(e.stderr, (bytes, bytearray))
            else (str(e.stderr).encode("utf-8") if e.stderr else b"")
        )
        err = raw.decode("utf-8", errors="ignore").strip()
        if not err:
            err = str(e)
        err = err[-4000:]

        set_job(job_id, status="failed", error=err)
        if callback_url:
            post_callback(
                callback_url,
                callback_secret,
                {
                    "external_job_id": job_id,
                    "status": "failed",
                    "progress": job.get("progress", 0.0),
                    "output_url": None,
                    "error": err,
                },
            )

    except Exception as e:
        err = str(e)

        set_job(job_id, status="failed", error=err)
        if callback_url:
            post_callback(
                callback_url,
                callback_secret,
                {
                    "external_job_id": job_id,
                    "status": "failed",
                    "progress": job.get("progress", 0.0),
                    "output_url": None,
                    "error": err,
                },
            )
