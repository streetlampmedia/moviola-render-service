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
            for ch
