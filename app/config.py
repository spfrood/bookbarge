"""Settings from environment variables, with repo-root .env as fallback.

Real environment variables win over .env values, so systemd/deploy
overrides work without editing the file.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(REPO_ROOT / ".env")


class Settings:
    # Filesystem layout root (PROJECT_BIBLE.md §6)
    data_root = Path(os.environ.get("BOOKBARGE_DATA_ROOT", "/srv/bookbarge"))

    # Port 8091 is bookbarge's reserved port in SERVER.md's registry.
    # Bind localhost only — nginx is the sole intended client.
    host = os.environ.get("BOOKBARGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BOOKBARGE_PORT", "8091"))

    runpod_api_key = os.environ.get("RUNPOD_API_KEY", "")
    runpod_endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
    session_secret = os.environ.get("SESSION_SECRET", "")

    @property
    def db_path(self) -> Path:
        return self.data_root / "data" / "app.db"


settings = Settings()
