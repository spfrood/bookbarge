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

# Default HF cache location; the endpoint template points this at the
# attached Network Volume (/runpod-volume) so weights persist across
# cold starts. Value here is only the fallback for volume-less runs.
ENV HF_HOME=/runpod-volume/hf

COPY handler.py /handler.py

CMD ["python", "-u", "/handler.py"]
