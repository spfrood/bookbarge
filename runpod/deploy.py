#!/usr/bin/env python3
"""Create the Bookbarge RunPod resources: template + endpoint.

The Turbo weights are baked into the Docker image (HF cache at /opt/hf), so
the endpoint needs no network volume and is free to run in any datacenter —
a network volume is region-locked and previously pinned the endpoint to one
datacenter, starving it of GPUs. The legacy `volume` command is kept for
reference but is no longer part of the deploy flow.

Stdlib-only; reads RUNPOD_API_KEY from ../.env (never prints it).

Usage:
  python3 deploy.py template IMAGE                    # create serverless template
  python3 deploy.py endpoint TEMPLATE_ID [MAX_WORKERS]  # create endpoint (no volume, multi-DC)
  python3 deploy.py list                              # show existing resources
  python3 deploy.py delete-endpoint ID               # tear down an old endpoint
  python3 deploy.py delete-volume ID                 # tear down an old network volume
  python3 deploy.py volume                            # (legacy) create HF-cache volume
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://rest.runpod.io/v1"

VOLUME_NAME = "bookbarge-hf-cache"
VOLUME_SIZE_GB = 10
# Volume + endpoint must share a datacenter; tried in order until one works.
DATACENTER_CANDIDATES = ["US-KS-2", "US-TX-3", "US-GA-1", "US-IL-1", "EU-RO-1"]

# Now that the weights are baked into the image, the endpoint is not tied to
# a volume, so it can span every US datacenter — RunPod places workers
# wherever a suitable GPU is free, which is what lets workersMax actually
# fill. (US-only to keep audio data stateside.)
US_DATACENTERS = ["US-KS-2", "US-TX-3", "US-GA-1", "US-IL-1", "US-NC-1", "US-CA-2"]
DEFAULT_MAX_WORKERS = 5

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
    # No HF_HOME env override: the image bakes HF_HOME=/opt/hf itself, and a
    # template env would win at runtime and send it back to a (now absent)
    # volume path. containerDiskInGb is the writable overlay, not the image.
    # RunPod requires globally-unique template names, so a redeploy can't
    # reuse the bare name — suffix it so old + new templates coexist.
    name = f"{TEMPLATE_NAME}-{time.strftime('%m%d%H%M')}"
    t = call("POST", "/templates", {
        "name": name,
        "imageName": image,
        "isServerless": True,
        "category": "NVIDIA",
        "containerDiskInGb": 20,
    })
    print(f"created template {t['id']} ({name}) for image {image}")


def create_endpoint(template_id: str,
                    max_workers: int = DEFAULT_MAX_WORKERS) -> None:
    # No networkVolumeId and a multi-datacenter spread: weights ride in the
    # image, so RunPod can start workers in any of these datacenters — which
    # is what lets max workers actually fill instead of queuing behind the
    # 2-3 GPUs free in one region.
    ep = call("POST", "/endpoints", {
        "name": f"{ENDPOINT_NAME}-{time.strftime('%m%d%H%M')}",
        "templateId": template_id,
        "computeType": "GPU",
        "gpuTypeIds": GPU_TYPE_IDS,
        "gpuCount": 1,
        "dataCenterIds": US_DATACENTERS,
        "workersMin": 0,
        "workersMax": max_workers,
        "idleTimeout": 10,
        "executionTimeoutMs": 600_000,
        "flashboot": True,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 4,
        "minCudaVersion": "12.4",
    })
    print(f"created endpoint {ep['id']} "
          f"(max {max_workers} workers, datacenters: {', '.join(US_DATACENTERS)})")
    print(f"→ set RUNPOD_ENDPOINT_ID={ep['id']} in .env, then restart bookbarge")


def set_workers(endpoint_id: str, max_workers: int) -> None:
    call("PATCH", f"/endpoints/{endpoint_id}", {"workersMax": max_workers})
    print(f"endpoint {endpoint_id} max workers -> {max_workers}")


def delete_endpoint(endpoint_id: str) -> None:
    call("DELETE", f"/endpoints/{endpoint_id}")
    print(f"deleted endpoint {endpoint_id}")


def delete_volume(volume_id: str) -> None:
    call("DELETE", f"/networkvolumes/{volume_id}")
    print(f"deleted network volume {volume_id}")


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
        case ["endpoint", template_id]:
            create_endpoint(template_id)
        case ["endpoint", template_id, max_workers]:
            create_endpoint(template_id, int(max_workers))
        case ["set-workers", endpoint_id, max_workers]:
            set_workers(endpoint_id, int(max_workers))
        case ["delete-endpoint", endpoint_id]:
            delete_endpoint(endpoint_id)
        case ["delete-volume", volume_id]:
            delete_volume(volume_id)
        case ["list"]:
            list_resources()
        case _:
            sys.exit(__doc__)
