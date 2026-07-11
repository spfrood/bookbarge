#!/usr/bin/env python3
"""Standalone tester for the deployed Chatterbox Turbo endpoint.

Stdlib-only; reads RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID from ../.env.

Usage:
  python3 test_endpoint.py single --voice ref.wav --text "Hello [chuckle] world." [--out out.wav]
  python3 test_endpoint.py sweep  --voice ref.wav [--lengths 300,400,500,600,700,800] [--outdir sweep]

`sweep` measures the input-length ceiling (PROJECT_BIBLE.md §8): it sends
prose samples of increasing character length and reports audio duration vs.
text length. Truncation shows up as audio_seconds flattening near ~40s or
the seconds-per-100-chars ratio collapsing; drift needs a human ear, so
every sample is saved for listening.
"""

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.runpod.ai/v2"

# Public-domain-style pulp prose used to build sweep samples, with a couple
# of paralinguistic tags to confirm they render rather than being read aloud.
PROSE = (
    "The rain hammered the tin roof of the marina office while Kate Marlowe "
    "counted the last of the charter money. [sigh] Three weeks of engine "
    "repairs had eaten every dollar, and the bank wanted its payment by "
    "Friday. She looked out at the barge riding low against the dock, its "
    "running lights smeared by the wet glass. Somewhere beyond the "
    "breakwater a horn sounded, long and mournful, and she felt the old "
    "restlessness stir. [chuckle] Trouble always announced itself politely "
    "at first. She pulled on her oilskin, checked the flashlight twice, and "
    "stepped out into the storm to see what the sea had decided to send her "
    "tonight. The dock planks groaned under her boots as the wind tried the "
    "buttons of her coat, and the first cold trickle found its way down her "
    "collar before she reached the gangway. "
)


def env() -> dict:
    path = Path(__file__).resolve().parent.parent / ".env"
    vals = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    for k in ("RUNPOD_API_KEY", "RUNPOD_ENDPOINT_ID"):
        if not vals.get(k):
            sys.exit(f"{k} missing from .env")
    return vals


def call(cfg: dict, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}/{cfg['RUNPOD_ENDPOINT_ID']}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {cfg['RUNPOD_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def generate(cfg: dict, text: str, voice_b64: str, timeout_s: int = 1200) -> dict:
    """Submit one job and poll to completion. Returns the handler output."""
    job = call(cfg, "run", {"input": {"text": text,
                                      "voice_reference_base64": voice_b64}})
    job_id = job["id"]
    t0 = time.time()
    last_status = None
    while time.time() - t0 < timeout_s:
        st = call(cfg, f"status/{job_id}")
        if st["status"] != last_status:
            print(f"  [{time.time()-t0:6.1f}s] {st['status']}", flush=True)
            last_status = st["status"]
        if st["status"] == "COMPLETED":
            out = st["output"]
            if "error" in out:
                sys.exit(f"handler error: {out['error']}")
            out["_wall_seconds"] = round(time.time() - t0, 1)
            return out
        if st["status"] in ("FAILED", "CANCELLED", "TIMED_OUT"):
            sys.exit(f"job {st['status']}: {json.dumps(st)[:1000]}")
        time.sleep(3)
    sys.exit("timed out waiting for job")


def load_voice(path: str) -> str:
    data = Path(path).read_bytes()
    if data[:4] != b"RIFF":
        sys.exit(f"{path} does not look like a WAV file")
    print(f"voice reference: {path} ({len(data)/1024:.0f} KiB)")
    return base64.b64encode(data).decode()


def save_wav(out: dict, path: Path) -> None:
    path.write_bytes(base64.b64decode(out["audio_base64"]))
    print(f"  saved {path}  ({out['audio_seconds']}s audio @ {out['sample_rate']}Hz, "
          f"generated in {out['generation_seconds']}s, wall {out['_wall_seconds']}s)")


def build_sample(chars: int) -> str:
    """Prose sample of ~chars length, cut at a sentence boundary."""
    text = (PROSE * (chars // len(PROSE) + 1))[:chars]
    cut = text.rfind(". ")
    return text[: cut + 1] if cut > chars * 0.6 else text


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    s1 = sub.add_parser("single")
    s1.add_argument("--voice", required=True)
    s1.add_argument("--text", required=True)
    s1.add_argument("--out", default="out.wav")
    s2 = sub.add_parser("sweep")
    s2.add_argument("--voice", required=True)
    s2.add_argument("--lengths", default="300,400,500,600,700,800")
    s2.add_argument("--outdir", default="sweep")
    args = ap.parse_args()

    cfg = env()
    voice_b64 = load_voice(args.voice)

    if args.mode == "single":
        out = generate(cfg, args.text, voice_b64)
        save_wav(out, Path(args.out))
        return

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    results = []
    for n in [int(x) for x in args.lengths.split(",")]:
        text = build_sample(n)
        print(f"sweep: {len(text)} chars")
        out = generate(cfg, text, voice_b64)
        save_wav(out, outdir / f"sweep_{len(text):04d}.wav")
        results.append((len(text), out["audio_seconds"], out["generation_seconds"]))

    print("\nchars  audio_s  s/100chars  gen_s")
    for chars, audio_s, gen_s in results:
        print(f"{chars:5d}  {audio_s:7.1f}  {audio_s/chars*100:10.2f}  {gen_s:5.1f}")
    print("\nTruncation indicator: audio_s flattening toward ~40s or the "
          "s/100chars ratio collapsing at higher lengths. Listen to each "
          "file for voice drift — that limit usually arrives first.")


if __name__ == "__main__":
    main()
