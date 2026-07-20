# Bookbarge — Chatterbox Turbo serverless worker
# torch 2.6.0 (pinned by chatterbox-tts) ships cu124 wheels, so a plain
# python base works: the CUDA runtime comes bundled in the wheel and the
# host driver comes from RunPod (endpoint requires CUDA >= 12.4).
FROM python:3.11-slim

# git: resemble-perth is a git dependency of chatterbox-tts
# libsndfile1/ffmpeg: audio I/O backends for librosa/torchaudio
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Bake the Turbo weights INTO the image (HF cache at /opt/hf) instead of
# onto a network volume. A network volume is region-locked, which pinned
# the whole endpoint to a single datacenter and starved it of GPUs (only
# ~3 workers ever scaled up); with the weights in the image the endpoint
# can run in ANY datacenter. Chatterbox Turbo is small (~350M params), so
# the image grows only modestly. The weights are downloaded on CPU at
# build time — no GPU is needed to populate the cache.
ENV HF_HOME=/opt/hf
RUN python -c "import torch; from chatterbox.tts_turbo import ChatterboxTurboTTS; ChatterboxTurboTTS.from_pretrained(device='cpu')"

COPY handler.py /handler.py

CMD ["python", "-u", "/handler.py"]
