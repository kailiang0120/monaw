from fastapi import APIRouter, HTTPException
import re
import shutil
import subprocess

from app.agent.sandbox.capabilities import get_sandbox_status
from app.agent.settings_store import load_agent_settings, save_agent_settings
from app.config import settings
from app.schemas import SandboxResolveImagePayload, SandboxResolveImageResponse, SandboxStatusPayload

router = APIRouter()


@router.get("/sandbox/status", response_model=SandboxStatusPayload)
async def sandbox_status():
    runtime_settings = load_agent_settings(settings)
    return get_sandbox_status(runtime_settings.sandbox)


@router.post("/sandbox/docker/resolve-image", response_model=SandboxResolveImageResponse)
async def resolve_docker_image(body: SandboxResolveImagePayload):
    image = body.image.strip()
    if any(char.isspace() for char in image) or any(char in image for char in ";&|<>`$\\"):
        raise HTTPException(status_code=400, detail="Docker image reference contains invalid characters.")
    if shutil.which("docker") is None:
        raise HTTPException(status_code=503, detail="Docker is not installed or not available on PATH.")

    try:
        pull = subprocess.run(
            ["docker", "pull", image],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Docker image pull timed out.") from exc
    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout or "Docker image pull failed.").strip().splitlines()[-1]
        raise HTTPException(status_code=502, detail=detail[:500])

    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    resolved = (inspected.stdout or "").strip().splitlines()[0] if inspected.returncode == 0 else ""
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-fA-F]{64}", resolved):
        detail = (inspected.stderr or "Docker did not return a pinned repository digest.").strip()
        raise HTTPException(status_code=502, detail=detail[:500])

    runtime_settings = load_agent_settings(settings)
    runtime_settings.sandbox.docker.image = resolved
    save_agent_settings(runtime_settings)
    return {
        "image": resolved,
        "detail": "Image pulled and pinned to its immutable digest.",
        "sandbox": get_sandbox_status(runtime_settings.sandbox),
    }
