"""RunPod Serverless handler for Chatterbox Turbo TTS (Bookbarge).

Input:  {"text": str, "voice_reference_base64": str}   (WAV, base64)
Output: {"audio_base64": str, "sample_rate": int, ...metrics}

The model MUST be the Turbo variant — it is the only Chatterbox model that
renders paralinguistic tags ([sigh], [laugh], ...) instead of reading them
aloud. HF_HOME points at an in-image cache (/opt/hf) where the Turbo
weights are baked at build time, so the endpoint needs no region-locked
network volume and can run in any datacenter.
"""

import base64
import hashlib
import io
import os
import time

import runpod
import torch
import torchaudio

from chatterbox.tts_turbo import ChatterboxTurboTTS

# Voice references are cached per worker by content hash so a warm worker
# processing many chunks of the same chapter only writes the clip once.
VOICE_CACHE_DIR = "/tmp/voice_refs"
os.makedirs(VOICE_CACHE_DIR, exist_ok=True)

_t0 = time.time()
MODEL = ChatterboxTurboTTS.from_pretrained(
    device="cuda" if torch.cuda.is_available() else "cpu"
)
print(
    f"Chatterbox Turbo loaded in {time.time() - _t0:.1f}s "
    f"(HF_HOME={os.environ.get('HF_HOME', 'unset')})",
    flush=True,
)


def handler(job):
    inp = job.get("input") or {}
    text = inp.get("text")
    ref_b64 = inp.get("voice_reference_base64")
    if not text or not ref_b64:
        return {"error": "Both 'text' and 'voice_reference_base64' are required."}

    try:
        ref_bytes = base64.b64decode(ref_b64)
    except Exception:
        return {"error": "voice_reference_base64 is not valid base64."}

    ref_path = os.path.join(
        VOICE_CACHE_DIR, hashlib.sha256(ref_bytes).hexdigest()[:16] + ".wav"
    )
    if not os.path.exists(ref_path):
        with open(ref_path, "wb") as f:
            f.write(ref_bytes)

    t_gen = time.time()
    with torch.inference_mode():
        wav = MODEL.generate(text, audio_prompt_path=ref_path)
    generation_seconds = time.time() - t_gen

    buf = io.BytesIO()
    torchaudio.save(buf, wav.cpu(), MODEL.sr, format="wav")

    return {
        "audio_base64": base64.b64encode(buf.getvalue()).decode(),
        "sample_rate": int(MODEL.sr),
        # Extra metrics beyond the contract — used by test_endpoint.py to
        # measure the input-length ceiling and by cost/latency analysis.
        "audio_seconds": round(wav.shape[-1] / MODEL.sr, 2),
        "generation_seconds": round(generation_seconds, 2),
        "text_chars": len(text),
    }


runpod.serverless.start({"handler": handler})
