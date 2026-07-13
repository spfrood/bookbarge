"""Project, chapter, and voice-reference management.

Ownership is enforced at the query level: every lookup filters by the
session user's id, and a project that exists but belongs to someone else
is indistinguishable from one that doesn't exist (404 either way).
"""

import sqlite3
import struct

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
# request.form() yields Starlette's UploadFile, not FastAPI's subclass —
# isinstance checks must use the Starlette type.
from starlette.datastructures import UploadFile

from . import auth, db, storage

MAX_CHAPTER_BYTES = 1 * 1024 * 1024        # a plain-text chapter is ~100 KB
MAX_VOICE_BYTES = 20 * 1024 * 1024
VOICE_MIN_SECONDS = 5.0
VOICE_MAX_SECONDS = 30.0
# Editors cutting "exactly 30s" land a few ms over (packet boundaries);
# don't bounce those.
VOICE_DURATION_GRACE = 0.5

router = APIRouter()
templates: Jinja2Templates = None  # set by main.py


def get_owned_project(conn: sqlite3.Connection, user: sqlite3.Row,
                      project_id: int) -> sqlite3.Row:
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ? AND user_id = ?",
        (project_id, user["id"])).fetchone()
    if project is None:
        raise HTTPException(status_code=404)
    return project


def wav_duration_seconds(data: bytes) -> float | None:
    """Duration from RIFF/WAVE headers (PCM or float — no codec needed).

    Returns None if the bytes aren't a parseable WAV file.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, byte_rate, data_size = 12, None, None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        (chunk_size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        if chunk_id == b"fmt " and pos + 16 <= len(data):
            (byte_rate,) = struct.unpack("<I", data[pos + 16:pos + 20])
        elif chunk_id == b"data":
            data_size = chunk_size
        pos += 8 + chunk_size + (chunk_size % 2)
    if not byte_rate or data_size is None:
        return None
    return data_size / byte_rate


# --- dashboard & project CRUD -------------------------------------------------

@router.get("/dashboard")
async def dashboard(request: Request, user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        projects = conn.execute(
            """SELECT p.*, COUNT(c.id) AS chapter_count
               FROM projects p LEFT JOIN chapters c ON c.project_id = p.id
               WHERE p.user_id = ? GROUP BY p.id ORDER BY p.created_at DESC""",
            (user["id"],)).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "projects": projects})


@router.post("/projects")
async def create_project(request: Request, user=Depends(auth.require_user),
                         title: str = Form(...), author: str = Form("")):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title is required.")
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO projects (user_id, title, author) VALUES (?, ?, ?)",
            (user["id"], title, author.strip()))
        conn.commit()
        project_id = cur.lastrowid
    finally:
        conn.close()
    storage.create_project_tree(user["id"], project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _project_context(conn, user, project_id: int, error: str = None) -> dict:
    project = get_owned_project(conn, user, project_id)
    chapters = conn.execute(
        "SELECT * FROM chapters WHERE project_id = ? ORDER BY chapter_number",
        (project_id,)).fetchall()
    voice = conn.execute(
        "SELECT * FROM voice_references WHERE project_id = ?",
        (project_id,)).fetchone()
    assembling = conn.execute(
        """SELECT 1 FROM generation_jobs WHERE project_id = ?
           AND chapter_id IS NULL AND status = 'running'""",
        (project_id,)).fetchone() is not None
    audiobook_exists = (storage.output_dir(user["id"], project_id)
                        / "audiobook.m4b").is_file()
    return {"user": user, "project": project, "chapters": chapters,
            "voice": voice, "assembling": assembling,
            "audiobook_exists": audiobook_exists,
            "stock_voices": list_stock_voices(), "error": error}


@router.get("/projects/{project_id}")
async def project_page(request: Request, project_id: int,
                       user=Depends(auth.require_user), error: str = None):
    conn = db.connect()
    try:
        context = _project_context(conn, user, project_id, error)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "project.html", context)


@router.post("/projects/{project_id}/edit")
async def edit_project(request: Request, project_id: int,
                       user=Depends(auth.require_user),
                       title: str = Form(...), author: str = Form(""),
                       narrator: str = Form("")):
    title = title.strip()
    if not title:
        return _project_error(request, user, project_id, "Title is required.")
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        conn.execute(
            """UPDATE projects SET title = ?, author = ?, narrator = ?,
                   updated_at = datetime('now') WHERE id = ?""",
            (title, author.strip(), narrator.strip(), project_id))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/delete")
async def delete_project(request: Request, project_id: int,
                         user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()
    storage.delete_project_tree(user["id"], project_id)
    return RedirectResponse("/dashboard", status_code=303)


# --- chapter upload -----------------------------------------------------------

@router.post("/projects/{project_id}/chapters")
async def upload_chapters(request: Request, project_id: int,
                          user=Depends(auth.require_user)):
    form = await request.form()
    files = [v for v in form.getlist("chapters") if isinstance(v, UploadFile)]
    if not files:
        return _project_error(request, user, project_id, "No files selected.")

    validated = []
    for f in files:
        name = (f.filename or "").strip()
        if not name.lower().endswith(".txt"):
            return _project_error(request, user, project_id,
                                  f"{name or 'file'}: only .txt files are accepted.")
        data = await f.read()
        if len(data) > MAX_CHAPTER_BYTES:
            return _project_error(request, user, project_id,
                                  f"{name}: over the {MAX_CHAPTER_BYTES // 1024} KB limit.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _project_error(request, user, project_id,
                                  f"{name}: not valid UTF-8 text.")
        if not text.strip():
            return _project_error(request, user, project_id, f"{name}: file is empty.")
        validated.append((name, text))

    # Deterministic chapter order for a multi-file upload: sort by filename.
    validated.sort(key=lambda item: item[0].lower())

    conn = db.connect()
    try:
        project = get_owned_project(conn, user, project_id)
        next_number = conn.execute(
            "SELECT COALESCE(MAX(chapter_number), 0) + 1 FROM chapters "
            "WHERE project_id = ?", (project_id,)).fetchone()[0]
        for offset, (name, text) in enumerate(validated):
            cur = conn.execute(
                """INSERT INTO chapters (project_id, chapter_number, filename, raw_text)
                   VALUES (?, ?, ?, ?)""",
                (project_id, next_number + offset, name, text))
            chapter_id = cur.lastrowid
            storage.create_chapter_tree(user["id"], project_id, chapter_id)
            source = storage.chapter_dir(user["id"], project_id, chapter_id) / "source.txt"
            source.write_text(text, encoding="utf-8")
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# --- chapter audio: streaming + download (PROJECT_BIBLE.md §9) -----------------

@router.get("/projects/{project_id}/chapters/{chapter_id}/audio")
async def chapter_audio(request: Request, project_id: int, chapter_id: int,
                        user=Depends(auth.require_user), download: int = 0):
    """Serve the chapter's assembled audio (assembled.m4a; assembled.mp3
    on projects from before the AAC switch). FileResponse handles HTTP
    Range natively, so the browser player can scrub without fetching the
    whole file; with ?download=1 the same bytes arrive as an attachment
    for external players.
    """
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        chapter = conn.execute(
            "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
            (chapter_id, project_id)).fetchone()
    finally:
        conn.close()
    if chapter is None or not chapter["assembled_audio_path"]:
        raise HTTPException(status_code=404)
    path = Path(chapter["assembled_audio_path"])
    if not path.is_file():
        raise HTTPException(status_code=404)

    media_type = "audio/mpeg" if path.suffix == ".mp3" else "audio/mp4"
    if download:
        stem = Path(chapter["filename"] or "chapter").stem
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in stem)
        filename = f"{chapter['chapter_number']:02d}-{safe}{path.suffix}"
        return FileResponse(path, media_type=media_type, filename=filename)
    return FileResponse(path, media_type=media_type)


@router.get("/projects/{project_id}/audiobook")
async def download_audiobook(request: Request, project_id: int,
                             user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        project = get_owned_project(conn, user, project_id)
    finally:
        conn.close()
    path = storage.output_dir(user["id"], project_id) / "audiobook.m4b"
    if not path.is_file():
        raise HTTPException(status_code=404)
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_"
                   for ch in project["title"]).strip() or "audiobook"
    return FileResponse(path, media_type="audio/mp4", filename=f"{safe}.m4b")


# --- stock voices (2026-07-12): site-provided selectable voices ----------------
# Curated WAVs the admin drops into stock_voices/ under the data root
# (5-30s, same spec as uploads). Scanned per request — no DB rows, no
# admin UI needed yet. Selecting one copies it into the project as its
# voice reference; user uploads never join this list.

def list_stock_voices() -> list[dict]:
    d = storage.stock_voices_dir()
    if not d.is_dir():
        return []
    return [{"name": p.stem, "filename": p.name}
            for p in sorted(d.glob("*.wav")) if p.is_file()]


def _stock_voice_path(filename: str) -> Path:
    """Resolve a stock voice by exact filename match against the scan —
    the request value is never used to build a path directly."""
    for v in list_stock_voices():
        if v["filename"] == filename:
            return storage.stock_voices_dir() / v["filename"]
    raise HTTPException(status_code=404, detail="No such voice.")


@router.get("/voices/{filename}/audio")
async def stock_voice_preview(request: Request, filename: str,
                              user=Depends(auth.require_user)):
    """Preview stream so the user can hear a stock voice before choosing."""
    return FileResponse(_stock_voice_path(filename), media_type="audio/wav")


@router.post("/projects/{project_id}/voice/stock")
async def select_stock_voice(request: Request, project_id: int,
                             user=Depends(auth.require_user),
                             voice_name: str = Form(...)):
    src = _stock_voice_path(voice_name)
    data = src.read_bytes()
    # Same spec as uploads — catches an out-of-spec file in the stock
    # folder at selection time instead of at generation time.
    duration = wav_duration_seconds(data)
    if duration is None or not (
            VOICE_MIN_SECONDS <= duration
            <= VOICE_MAX_SECONDS + VOICE_DURATION_GRACE):
        return _project_error(
            request, user, project_id,
            f"'{src.stem}' is out of spec on the server "
            f"({'unreadable' if duration is None else f'{duration:.1f}s'}) — "
            "tell the admin.")

    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        # Single active clip, same as an upload: replace file and row.
        path = storage.voice_dir(user["id"], project_id) / "reference.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        conn.execute("DELETE FROM voice_references WHERE project_id = ?",
                     (project_id,))
        conn.execute(
            """INSERT INTO voice_references (project_id, filename, file_path)
               VALUES (?, ?, ?)""",
            (project_id, f"{src.stem} (stock voice)", str(path)))
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# --- voice reference ----------------------------------------------------------

@router.post("/projects/{project_id}/voice")
async def upload_voice(request: Request, project_id: int,
                       user=Depends(auth.require_user)):
    form = await request.form()
    f = form.get("voice")
    if not isinstance(f, UploadFile) or not (f.filename or "").strip():
        return _project_error(request, user, project_id, "No file selected.")
    name = f.filename.strip()
    if not name.lower().endswith(".wav"):
        return _project_error(request, user, project_id,
                              "Voice reference must be a WAV file.")
    data = await f.read()
    if len(data) > MAX_VOICE_BYTES:
        return _project_error(request, user, project_id,
                              f"Over the {MAX_VOICE_BYTES // (1024*1024)} MB limit.")
    duration = wav_duration_seconds(data)
    if duration is None:
        return _project_error(request, user, project_id,
                              "That file doesn't parse as a WAV.")
    if not (VOICE_MIN_SECONDS <= duration <= VOICE_MAX_SECONDS + VOICE_DURATION_GRACE):
        return _project_error(
            request, user, project_id,
            f"Clip is {duration:.1f}s — it must be between "
            f"{VOICE_MIN_SECONDS:.0f} and {VOICE_MAX_SECONDS:.0f} seconds.")

    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        # Single active clip: replace file and row outright.
        path = storage.voice_dir(user["id"], project_id) / "reference.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        conn.execute("DELETE FROM voice_references WHERE project_id = ?",
                     (project_id,))
        conn.execute(
            """INSERT INTO voice_references (project_id, filename, file_path)
               VALUES (?, ?, ?)""", (project_id, name, str(path)))
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _project_error(request, user, project_id, message):
    """Re-render the project page with a validation error (422)."""
    conn = db.connect()
    try:
        context = _project_context(conn, user, project_id, message)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "project.html", context,
                                      status_code=422)
