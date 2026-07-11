#!/usr/bin/env python3
"""Create the Bookbarge RunPod resources: network volume, template, endpoint.

Stdlib-only; reads RUNPOD_API_KEY from ../.env (never prints it).

Usage:
  python3 deploy.py volume                 # create HF-cache network volume
  python3 deploy.py template IMAGE         # create serverless template
  python3 deploy.py endpoint VOLUME_ID TEMPLATE_ID
  python3 deploy.py list                   # show existing resources
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://rest.runpod.io/v1"

VOLUME_NAME = "bookbarge-hf-cache"
VOLUME_SIZE_GB = 10
# Volume + endpoint must share a datacenter; tried in order until one works.
DATACENTER_CANDIDATES = ["US-KS-2", "US-TX-3", "US-GA-1", "US-IL-1", "EU-RO-1"]

TEMPLATE_NAME = "bookbarge-chatterbox"
ENDPOINT_NAME = "bookbarge-chatterbox"

# Cheapest 24GB-class cards first; 4090 as availability fallback.
GPU_TYPE_IDS = [
    "NVIDIA GeForce RTX 3090",
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 4090",
]


def api_key() -> str:
    env = Path(__file__).resolve().parent.parent / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("RUNPOD_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    sys.exit("RUNPOD_API_KEY not found in .env")


def call(method: str, path: str, body: dict | None = None) -> dict | list:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} on {method} {path}: {detail}") from e


def create_volume() -> None:
    existing = [v for v in call("GET", "/networkvolumes") if v["name"] == VOLUME_NAME]
    if existing:
        v = existing[0]
        print(f"volume already exists: {v['id']} in {v['dataCenterId']}")
        return
    for dc in DATACENTER_CANDIDATES:
        try:
            v = call("POST", "/networkvolumes",
                     {"name": VOLUME_NAME, "size": VOLUME_SIZE_GB, "dataCenterId": dc})
            print(f"created volume {v['id']} ({VOLUME_SIZE_GB}GB) in {dc}")
            return
        except SystemExit as e:
            print(f"  {dc}: {e}")
    sys.exit("no candidate datacenter accepted the volume")


def create_template(image: str) -> None:
    t = call("POST", "/templates", {
        "name": TEMPLATE_NAME,
        "imageName": image,
        "isServerless": True,
        "category": "NVIDIA",
        "containerDiskInGb": 20,
        "env": {"HF_HOME": "/runpod-volume/hf"},
    })
    print(f"created template {t['id']} for image {image}")


def create_endpoint(volume_id: str, template_id: str) -> None:
    volume = call("GET", f"/networkvolumes/{volume_id}")
    ep = call("POST", "/endpoints", {
        "name": ENDPOINT_NAME,
        "templateId": template_id,
        "computeType": "GPU",
        "gpuTypeIds": GPU_TYPE_IDS,
        "gpuCount": 1,
        "dataCenterIds": [volume["dataCenterId"]],
        "networkVolumeId": volume_id,
        "workersMin": 0,
        "workersMax": 2,
        "idleTimeout": 10,
        "executionTimeoutMs": 600_000,
        "flashboot": True,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 4,
        "minCudaVersion": "12.4",
    })
    print(f"created endpoint {ep['id']} in {volume['dataCenterId']}")
    print(f"→ set RUNPOD_ENDPOINT_ID={ep['id']} in .env")


def list_resources() -> None:
    for label, path in [("volumes", "/networkvolumes"),
                        ("templates", "/templates"),
                        ("endpoints", "/endpoints")]:
        items = call("GET", path)
        print(f"{label}:")
        for it in items:
            print(f"  {json.dumps(it)[:200]}")


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["volume"]:
            create_volume()
        case ["template", image]:
            create_template(image)
        case ["endpoint", volume_id, template_id]:
            create_endpoint(volume_id, template_id)
        case ["list"]:
            list_resources()
        case _:
            sys.exit(__doc__)
