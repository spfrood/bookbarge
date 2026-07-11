"""Thin async client for the RunPod Serverless endpoint (Phase 1 handler).

Contract (runpod/RESULTS.md): input {text, voice_reference_base64},
output {audio_base64, sample_rate, ...metrics}.
"""

import httpx

from .config import settings

_client: httpx.AsyncClient | None = None


def _base() -> str:
    return f"https://api.runpod.ai/v2/{settings.runpod_endpoint_id}"


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.runpod_api_key}"},
            timeout=httpx.Timeout(30.0, read=120.0),
        )
    return _client


async def submit(text: str, voice_reference_base64: str) -> str:
    """Submit one chunk; returns the RunPod job id."""
    r = await client().post(f"{_base()}/run", json={
        "input": {"text": text,
                  "voice_reference_base64": voice_reference_base64}})
    r.raise_for_status()
    return r.json()["id"]


async def status(job_id: str) -> dict:
    """Job status: {"status": IN_QUEUE|IN_PROGRESS|COMPLETED|FAILED|...,
    "output": {...} when completed}."""
    r = await client().get(f"{_base()}/status/{job_id}")
    r.raise_for_status()
    return r.json()
