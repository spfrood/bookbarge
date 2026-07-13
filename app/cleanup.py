"""User-driven cleanup (PROJECT_BIBLE.md §2 step 13, §6 cleanup note).

Per chapter, the user explicitly chooses keep or delete for that chapter's
assembled audio + leftover chunk audio — nothing is deleted without a choice
(the form requires a selection for every chapter; defaults are 'keep').
Deleting also drops the chapter's chunk rows and RunPod job records. The
final M4B in output/ and anything kept persist until the project itself
is deleted.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, db, storage
from .projects import get_owned_project

router = APIRouter()
templates: Jinja2Templates = None  # set by main.py

BUSY = ("generating", "recasting")

# assembled.mp3 is the pre-AAC-switch name; older projects still have it.
ASSEMBLED_NAMES = ("assembled.m4a", "assembled.mp3")


def _chapter_sizes(user_id: int, project_id: int, chapter_id: int) -> tuple[int, int]:
    chunks = sum(f.stat().st_size for f in
                 storage.chunks_dir(user_id, project_id, chapter_id).glob("*.wav"))
    chapter_dir = storage.chapter_dir(user_id, project_id, chapter_id)
    audio = sum((chapter_dir / name).stat().st_size
                for name in ASSEMBLED_NAMES if (chapter_dir / name).is_file())
    return chunks, audio


@router.get("/projects/{project_id}/cleanup")
async def cleanup_page(request: Request, project_id: int,
                       user=Depends(auth.require_user), error: str = None):
    conn = db.connect()
    try:
        project = get_owned_project(conn, user, project_id)
        chapters = conn.execute(
            "SELECT * FROM chapters WHERE project_id = ? ORDER BY chapter_number",
            (project_id,)).fetchall()
    finally:
        conn.close()
    rows = []
    for c in chapters:
        chunk_bytes, audio_bytes = _chapter_sizes(user["id"], project_id, c["id"])
        rows.append({"chapter": c, "chunk_mb": chunk_bytes / 1048576,
                     "audio_mb": audio_bytes / 1048576})
    return templates.TemplateResponse(request, "cleanup.html", {
        "user": user, "project": project, "rows": rows, "error": error})


@router.post("/projects/{project_id}/cleanup")
async def run_cleanup(request: Request, project_id: int,
                      user=Depends(auth.require_user)):
    form = await request.form()
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        chapters = conn.execute(
            "SELECT * FROM chapters WHERE project_id = ? ORDER BY chapter_number",
            (project_id,)).fetchall()
        if any(c["processing_status"] in BUSY for c in chapters):
            raise HTTPException(status_code=409,
                                detail="Wait for generation to finish first.")

        # Every chapter needs an explicit keep/delete — no silent defaults
        # server-side, even though the form pre-selects 'keep'.
        choices = {}
        for c in chapters:
            choice = form.get(f"chapter_{c['id']}")
            if choice not in ("keep", "delete"):
                raise HTTPException(status_code=422,
                                    detail="Every chapter needs a keep/delete "
                                           "choice.")
            choices[c["id"]] = choice

        for c in chapters:
            if choices[c["id"]] != "delete":
                continue
            for wav in storage.chunks_dir(user["id"], project_id,
                                          c["id"]).glob("*.wav"):
                wav.unlink()
            chapter_dir = storage.chapter_dir(user["id"], project_id, c["id"])
            for name in ASSEMBLED_NAMES:
                (chapter_dir / name).unlink(missing_ok=True)
            conn.execute("DELETE FROM chunks WHERE chapter_id = ?", (c["id"],))
            conn.execute("DELETE FROM generation_jobs WHERE chapter_id = ?",
                         (c["id"],))
            conn.execute(
                """UPDATE chapters SET assembled_audio_path = NULL,
                       assembled_at = NULL WHERE id = ?""", (c["id"],))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)
