"""Chapter review: Approved toggle, inline edit, .txt replacement, recast.

Recast semantics (PROJECT_BIBLE.md §2 step 9, §5): either edit path bumps
version, clears approval (the audio backing it is being replaced), wipes
the old chunks and audio, re-chunks, and regenerates immediately through
the Phase 6-8 pipeline — landing back at ready_for_review. No text history
is kept; raw_text and source.txt are overwritten outright.

Approval (§9): a plain boolean, freely flippable — approve, unapprove,
re-approve — whether or not the text changed. The only time it's refused
is while the chapter's audio is actively being regenerated.
"""

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from . import auth, db, generation, storage
from .projects import MAX_CHAPTER_BYTES, get_owned_project

router = APIRouter()
templates: Jinja2Templates = None  # set by main.py

BUSY = ("generating", "recasting")


def _get_chapter(conn: sqlite3.Connection, user: sqlite3.Row,
                 project_id: int, chapter_id: int) -> sqlite3.Row:
    get_owned_project(conn, user, project_id)
    chapter = conn.execute(
        "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
        (chapter_id, project_id)).fetchone()
    if chapter is None:
        raise HTTPException(status_code=404)
    return chapter


# --- approval toggle ----------------------------------------------------------

@router.post("/projects/{project_id}/chapters/{chapter_id}/approve")
async def toggle_approved(request: Request, project_id: int, chapter_id: int,
                          user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        chapter = _get_chapter(conn, user, project_id, chapter_id)
        if chapter["processing_status"] in BUSY:
            raise HTTPException(status_code=409,
                                detail="Chapter audio is being regenerated.")
        if not chapter["assembled_audio_path"]:
            raise HTTPException(status_code=422,
                                detail="Nothing to approve yet — generate first.")
        now_approved = 0 if chapter["approved"] else 1
        conn.execute(
            """UPDATE chapters SET approved = ?,
                   approved_at = CASE WHEN ? THEN datetime('now') ELSE NULL END
               WHERE id = ?""", (now_approved, now_approved, chapter_id))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/chapters/{chapter_id}/title")
async def edit_chapter_title(request: Request, project_id: int, chapter_id: int,
                             user=Depends(auth.require_user),
                             title: str = Form("")):
    """Metadata-only rename — no recast, no approval change. Used for the
    chapter marker in the final M4B (re-assemble to bake it in)."""
    conn = db.connect()
    try:
        _get_chapter(conn, user, project_id, chapter_id)
        conn.execute("UPDATE chapters SET title = ? WHERE id = ?",
                     (title.strip() or None, chapter_id))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}/chapters/{chapter_id}",
                            status_code=303)


# --- chapter detail: inline editor + replace upload ----------------------------

@router.get("/projects/{project_id}/chapters/{chapter_id}")
async def chapter_page(request: Request, project_id: int, chapter_id: int,
                       user=Depends(auth.require_user), error: str = None):
    conn = db.connect()
    try:
        chapter = _get_chapter(conn, user, project_id, chapter_id)
        project = conn.execute("SELECT * FROM projects WHERE id = ?",
                               (project_id,)).fetchone()
        voice = conn.execute(
            "SELECT id FROM voice_references WHERE project_id = ?",
            (project_id,)).fetchone()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "chapter.html", {
        "user": user, "project": project, "chapter": chapter,
        "has_voice": voice is not None, "error": error})


@router.post("/projects/{project_id}/chapters/{chapter_id}/text")
async def edit_text(request: Request, project_id: int, chapter_id: int,
                    user=Depends(auth.require_user), raw_text: str = Form(...)):
    text = raw_text.replace("\r\n", "\n").strip()
    if not text:
        return await chapter_page(request, project_id, chapter_id, user,
                                  error="Chapter text can't be empty.")
    return _recast(request, user, project_id, chapter_id, text)


@router.post("/projects/{project_id}/chapters/{chapter_id}/replace")
async def replace_file(request: Request, project_id: int, chapter_id: int,
                       user=Depends(auth.require_user)):
    form = await request.form()
    f = form.get("replacement")
    if not isinstance(f, UploadFile) or not (f.filename or "").strip():
        return await chapter_page(request, project_id, chapter_id, user,
                                  error="No file selected.")
    name = f.filename.strip()
    if not name.lower().endswith(".txt"):
        return await chapter_page(request, project_id, chapter_id, user,
                                  error="Only .txt files are accepted.")
    data = await f.read()
    if len(data) > MAX_CHAPTER_BYTES:
        return await chapter_page(request, project_id, chapter_id, user,
                                  error="File is over the size limit.")
    try:
        text = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return await chapter_page(request, project_id, chapter_id, user,
                                  error="Not valid UTF-8 text.")
    if not text:
        return await chapter_page(request, project_id, chapter_id, user,
                                  error="File is empty.")
    return _recast(request, user, project_id, chapter_id, text, filename=name)


def _recast(request: Request, user: sqlite3.Row, project_id: int,
            chapter_id: int, text: str, filename: str | None = None):
    """Shared recast path: version bump, approval cleared, old audio wiped,
    immediate regeneration through the standard pipeline."""
    conn = db.connect()
    try:
        chapter = _get_chapter(conn, user, project_id, chapter_id)
        if chapter["processing_status"] in BUSY:
            raise HTTPException(status_code=409,
                                detail="Already regenerating — wait for it "
                                       "to finish.")
        voice = conn.execute(
            "SELECT id FROM voice_references WHERE project_id = ?",
            (project_id,)).fetchone()
        if voice is None:
            raise HTTPException(status_code=422,
                                detail="Upload a voice reference first.")

        conn.execute(
            """UPDATE chapters SET raw_text = ?, version = version + 1,
                   approved = 0, approved_at = NULL,
                   assembled_audio_path = NULL, assembled_at = NULL,
                   filename = COALESCE(?, filename)
               WHERE id = ?""", (text, filename, chapter_id))

        # Overwrite source.txt and remove the replaced version's audio —
        # nothing stale stays playable or assembleable.
        chapter_dir = storage.chapter_dir(user["id"], project_id, chapter_id)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "source.txt").write_text(text, encoding="utf-8")
        (chapter_dir / "assembled.mp3").unlink(missing_ok=True)
        for wav in storage.chunks_dir(user["id"], project_id, chapter_id).glob("*.wav"):
            wav.unlink()

        fresh = conn.execute("SELECT * FROM chapters WHERE id = ?",
                             (chapter_id,)).fetchone()
        generation.launch_generation(conn, user["id"], project_id, fresh,
                                     status="recasting")
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)
