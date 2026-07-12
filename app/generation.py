"""Generation pipeline: dispatch chunks to RunPod, poll, store audio.

Status machine (exact enums from PROJECT_BIBLE.md §5):
  chunk:   pending → processing → done   (or error)
  chapter: pending → generating → ready_for_review   (or error)

One chapter generates at a time per project (2026-07-11 decision); chunk
parallelism within it is settings.runpod_concurrency. Workers are plain
asyncio tasks in the app process — all resumable state lives in the DB
(runpod_job_id per chunk), so resume_orphaned() at startup picks up
whatever a stop/crash left mid-flight rather than abandoning paid jobs.

Recast race guard: every chunk row update is qualified by id AND
chapter_version; a recast deletes chunk rows, so late results for a
replaced version hit rowcount 0 and their audio is discarded.
"""

import asyncio
import base64
import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import assembly, auth, db, runpod_client, storage
from .chunking import persist_chunks
from .config import settings
from .projects import get_owned_project

POLL_SECONDS = 3
JOB_TIMEOUT_SECONDS = 15 * 60
MAX_ATTEMPTS = 2  # initial + one retry

router = APIRouter()

# Keep task references so they aren't garbage-collected mid-run.
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# --- routes -------------------------------------------------------------------

@router.post("/projects/{project_id}/chapters/{chapter_id}/generate")
async def generate_chapter(request: Request, project_id: int, chapter_id: int,
                           user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        chapter = conn.execute(
            "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
            (chapter_id, project_id)).fetchone()
        if chapter is None:
            raise HTTPException(status_code=404)
        voice = conn.execute(
            "SELECT file_path FROM voice_references WHERE project_id = ?",
            (project_id,)).fetchone()
        if voice is None:
            raise HTTPException(status_code=422,
                                detail="Upload a voice reference first.")
        launch_generation(conn, user["id"], project_id, chapter,
                          status="generating")
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def launch_generation(conn, user_id: int, project_id: int,
                      chapter: sqlite3.Row, status: str) -> None:
    """Shared launch path for Generate and recast. `status` is 'generating'
    or 'recasting' (§5 enum). Raises HTTPException on guard failure;
    commits and spawns the worker on success."""
    busy = conn.execute(
        "SELECT id FROM chapters WHERE project_id = ? "
        "AND processing_status IN ('generating', 'recasting')",
        (project_id,)).fetchone()
    if busy is not None:
        raise HTTPException(status_code=409,
                            detail="Another chapter is already generating — "
                                   "one at a time.")

    n = persist_chunks(conn, chapter)
    if n == 0:
        raise HTTPException(status_code=422, detail="Chapter has no text.")
    conn.execute(
        "UPDATE chapters SET processing_status = ? WHERE id = ?",
        (status, chapter["id"]))
    conn.execute(
        """INSERT INTO generation_jobs (project_id, chapter_id, total_chunks)
           VALUES (?, ?, ?)""", (project_id, chapter["id"], n))
    conn.execute("UPDATE projects SET status = 'generating', "
                 "updated_at = datetime('now') WHERE id = ?", (project_id,))
    conn.commit()
    _spawn(run_chapter(user_id, project_id, chapter["id"]))


@router.get("/projects/{project_id}/status.json")
async def project_status(request: Request, project_id: int,
                         user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        rows = conn.execute(
            """SELECT c.id, c.processing_status, c.approved,
                      COUNT(k.id) AS total,
                      SUM(CASE WHEN k.status = 'done' THEN 1 ELSE 0 END) AS done
               FROM chapters c LEFT JOIN chunks k ON k.chapter_id = c.id
               WHERE c.project_id = ? GROUP BY c.id""", (project_id,)).fetchall()
    finally:
        conn.close()
    return {"chapters": [
        {"id": r["id"], "status": r["processing_status"],
         "approved": bool(r["approved"]),
         "chunks_done": r["done"] or 0, "chunks_total": r["total"]}
        for r in rows]}


# --- worker -------------------------------------------------------------------

async def run_chapter(user_id: int, project_id: int, chapter_id: int) -> None:
    """Generate every unfinished chunk of one chapter, then finalize."""
    try:
        voice_path = storage.voice_dir(user_id, project_id) / "reference.wav"
        voice_b64 = base64.b64encode(voice_path.read_bytes()).decode()

        conn = db.connect()
        try:
            chunks = conn.execute(
                "SELECT * FROM chunks WHERE chapter_id = ? AND status != 'done' "
                "ORDER BY chunk_index", (chapter_id,)).fetchall()
        finally:
            conn.close()

        sem = asyncio.Semaphore(settings.runpod_concurrency)

        async def bounded(chunk):
            async with sem:
                return await _process_chunk(user_id, project_id, chunk, voice_b64)

        results = await asyncio.gather(*(bounded(c) for c in chunks),
                                       return_exceptions=True)
        ok = all(r is True for r in results)
        if ok and _all_chunks_done(chapter_id):
            # Phase 8: assemble the chapter the moment its last chunk lands.
            ok = await assembly.assemble_chapter(user_id, project_id, chapter_id)
    except Exception as exc:  # voice file missing, DB trouble, ...
        print(f"generation fatal for chapter {chapter_id}: {exc!r}", flush=True)
        ok = False
    _finalize_chapter(project_id, chapter_id, ok)


async def _process_chunk(user_id: int, project_id: int,
                         chunk: sqlite3.Row, voice_b64: str) -> bool:
    """Drive one chunk to done. Returns True on success."""
    job_id = chunk["runpod_job_id"]  # set = resuming an in-flight job
    for attempt in range(MAX_ATTEMPTS):
        try:
            if job_id is None:
                job_id = await runpod_client.submit(chunk["text"], voice_b64)
                if not _update_chunk(chunk, "processing", runpod_job_id=job_id):
                    return True  # recast replaced this version; abandon quietly
            output = await _poll(job_id)
            if output is None:
                job_id = None  # job failed → resubmit on next attempt
                continue
            audio = base64.b64decode(output["audio_base64"])
            path = storage.chunks_dir(user_id, project_id, chunk["chapter_id"]) \
                / f"{chunk['chunk_index']:04d}.wav"
            path.write_bytes(audio)
            if not _update_chunk(chunk, "done", audio_file_path=str(path)):
                path.unlink(missing_ok=True)  # stale version's audio
                return True
            _bump_job_progress(chunk["chapter_id"])
            return True
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            print(f"chunk {chunk['id']} attempt {attempt + 1} failed: {exc!r}",
                  flush=True)
            job_id = None
    _update_chunk(chunk, "error")
    return False


async def _poll(job_id: str) -> dict | None:
    """Poll until terminal. Returns output dict, or None if job failed."""
    waited = 0
    while waited < JOB_TIMEOUT_SECONDS:
        st = await runpod_client.status(job_id)
        if st["status"] == "COMPLETED":
            out = st.get("output") or {}
            if "audio_base64" in out:
                return out
            return None  # handler-level error payload
        if st["status"] in ("FAILED", "CANCELLED", "TIMED_OUT"):
            return None
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return None


def _update_chunk(chunk: sqlite3.Row, status: str, **fields) -> bool:
    """Guarded update: only applies if the row still exists at the same
    chapter_version (recast deletes/replaces rows). Returns False if stale."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    sql = (f"UPDATE chunks SET status = ?, updated_at = datetime('now')"
           f"{', ' + sets if sets else ''} "
           "WHERE id = ? AND chapter_version = ?")
    conn = db.connect()
    try:
        cur = conn.execute(sql, (status, *fields.values(),
                                 chunk["id"], chunk["chapter_version"]))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _all_chunks_done(chapter_id: int) -> bool:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE chapter_id = ? AND status != 'done'",
            (chapter_id,)).fetchone()[0] == 0
    finally:
        conn.close()


def _bump_job_progress(chapter_id: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            """UPDATE generation_jobs SET completed_chunks = completed_chunks + 1
               WHERE chapter_id = ? AND status = 'running'""", (chapter_id,))
        conn.commit()
    finally:
        conn.close()


def _finalize_chapter(project_id: int, chapter_id: int, ok: bool) -> None:
    conn = db.connect()
    try:
        # Trust the DB, not the in-memory flag alone: every chunk must be done.
        remaining = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE chapter_id = ? AND status != 'done'",
            (chapter_id,)).fetchone()[0]
        success = ok and remaining == 0
        conn.execute(
            "UPDATE chapters SET processing_status = ? WHERE id = ? "
            "AND processing_status IN ('generating', 'recasting')",
            ("ready_for_review" if success else "error", chapter_id))
        conn.execute(
            """UPDATE generation_jobs SET status = ?, completed_at = datetime('now')
               WHERE chapter_id = ? AND status = 'running'""",
            ("completed" if success else "error", chapter_id))
        conn.execute(
            """UPDATE projects SET status = 'reviewing', updated_at = datetime('now')
               WHERE id = ? AND NOT EXISTS (SELECT 1 FROM chapters
                   WHERE project_id = ?
                   AND processing_status IN ('generating', 'recasting'))""",
            (project_id, project_id))
        conn.commit()
    finally:
        conn.close()


def resume_orphaned() -> int:
    """Called at startup: restart workers for chapters a stop/crash left
    in 'generating'. Chunks with a runpod_job_id resume polling that job
    (audio RunPod already produced is not re-paid); the rest resubmit."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT c.id AS chapter_id, c.project_id, p.user_id
               FROM chapters c JOIN projects p ON p.id = c.project_id
               WHERE c.processing_status IN ('generating', 'recasting')""").fetchall()
    finally:
        conn.close()
    for r in rows:
        print(f"resuming generation: chapter {r['chapter_id']}", flush=True)
        _spawn(run_chapter(r["user_id"], r["project_id"], r["chapter_id"]))
    return len(rows)
