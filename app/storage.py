"""Filesystem layout under the data root (PROJECT_BIBLE.md §6).

/srv/bookbarge/
  data/app.db
  users/{user_id}/projects/{project_id}/
    voice/                      ← reference clip (single active: reference.wav)
    chapters/{chapter_id}/
      source.txt                ← current chapter text
      chunks/                   ← per-chunk audio for the current version
      assembled.m4a             ← per-chapter assembled audio (AAC;
                                  assembled.mp3 on pre-AAC-switch projects)
    output/                     ← final M4B

Every path builder takes user_id first: user isolation is structural —
there is no way to build a path that isn't inside the owner's tree, and
all names below users/ are integer database IDs, never user input.
"""

import shutil
from pathlib import Path

from .config import settings


def project_dir(user_id: int, project_id: int) -> Path:
    return settings.data_root / "users" / str(user_id) / "projects" / str(project_id)


def stock_voices_dir() -> Path:
    """Site-provided selectable voices (admin drops curated WAVs here;
    no per-user data). Display name = filename stem."""
    return settings.data_root / "stock_voices"


def voice_dir(user_id: int, project_id: int) -> Path:
    return project_dir(user_id, project_id) / "voice"


def chapter_dir(user_id: int, project_id: int, chapter_id: int) -> Path:
    return project_dir(user_id, project_id) / "chapters" / str(chapter_id)


def chunks_dir(user_id: int, project_id: int, chapter_id: int) -> Path:
    return chapter_dir(user_id, project_id, chapter_id) / "chunks"


def output_dir(user_id: int, project_id: int) -> Path:
    return project_dir(user_id, project_id) / "output"


def create_project_tree(user_id: int, project_id: int) -> None:
    voice_dir(user_id, project_id).mkdir(parents=True, exist_ok=True)
    output_dir(user_id, project_id).mkdir(parents=True, exist_ok=True)


def create_chapter_tree(user_id: int, project_id: int, chapter_id: int) -> None:
    chunks_dir(user_id, project_id, chapter_id).mkdir(parents=True, exist_ok=True)


def delete_project_tree(user_id: int, project_id: int) -> None:
    shutil.rmtree(project_dir(user_id, project_id), ignore_errors=True)


def delete_chapter_tree(user_id: int, project_id: int, chapter_id: int) -> None:
    shutil.rmtree(chapter_dir(user_id, project_id, chapter_id),
                  ignore_errors=True)
