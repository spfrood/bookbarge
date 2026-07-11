# RunPod Chatterbox Turbo — Phase 1 measurements (2026-07-11)

Endpoint: serverless, US-TX-3, GPU priority 3090 → A5000 → 4090, scale-to-zero,
FlashBoot, 10 GB network volume as HF cache (`HF_HOME=/runpod-volume/hf`).
Voice reference: 30 s / 44.1 kHz / 16-bit mono WAV (~3.5 MB as base64 —
well under the 10 MB request cap). Output: 24 kHz mono WAV.

## Input-length ceiling (PROJECT_BIBLE.md §8)

| chars | audio s | s/100 chars | verdict |
|------:|--------:|------------:|---------|
| 213   | 15.0    | 7.04        | clean |
| 315   | 23.3    | 7.39        | clean |
| 483   | 34.4–35.1 | 7.1–7.3   | clean but at the edge |
| 624   | 35.8    | 5.74        | **truncated** (expected ~45 s) |
| 796   | 34.4    | 4.33        | **truncated** (expected ~57 s) |

Raw duration flatlines at ~35 s of audio, but the **real limit is
intelligibility, and it fails earlier**: human listening (Scott, 2026-07-11)
found everything past **~30 s of audio** collapses into voice-like noises —
correct timbre, no recognizable words. So the 483-char sample (34–35 s) is
NOT usable despite looking linear in the table above.

Verification samples: 350 chars → 24.7 s and 400 chars → 25.9 s, both
comfortably under 30 s. Pacing also varies run-to-run (same 315-char text:
18.8–23.3 s across three runs, ±20%), so the cap needs margin for a slow
run, not just the average.

**Chunker hard cap: 350 characters** (expected ~25 s audio; even a
20%-slower run stays under the 30 s intelligibility cliff). Target range
becomes ~250–350. Pacing is voice-dependent — re-verify if the reference
voice changes substantially.

## Latency

- Cold start, first ever (image pull + model download to volume): ~2.6 min
- Cold start, model cached on volume (FlashBoot): ~30–50 s to first audio
- Warm worker: ~6–10 s generation per chunk (≈ 3–4× faster than real time)

## Cost

Whole measurement session (7 generations + cold starts) ≈ ~5 min of
24 GB-class worker time — a few cents. Billing API lags; check
`GET /v1/billing/endpoints` later for exact figures. Standing cost: 10 GB
network volume ≈ $0.70/month.

## Contract

Request `input`: `{"text": str, "voice_reference_base64": str}` (WAV b64)
Returned audio is **float32 WAV** (torchaudio default) — FFmpeg reads it
natively, but Python's stdlib `wave` module cannot. Chunk joins at sentence
boundaries verified by ear via FFmpeg concat (stitch_demo, 2026-07-11):
**seam not locatable by the listener** — plain concat suffices for
per-chapter assembly, no crossfade needed.

Open tuning note: reading pacing judged "a little quick" (intelligible,
not a blocker). Pacing largely follows the voice reference clip; options
if it stays annoying: use a slower-read reference section, or expose the
model's delivery parameters through the handler later.
Response `output`: `{"audio_base64", "sample_rate", "audio_seconds",
"generation_seconds", "text_chars"}` — the last three are metrics used by
`test_endpoint.py`; Bookbarge's backend needs only the first two.

Paralinguistic tags confirmed rendered (not read aloud) in smoke test:
`[sigh]`, `[chuckle]` — pending final human verdict on quality.
