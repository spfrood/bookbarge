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
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import assembly, auth, db, runpod_client, storage
from .chunking import (PAUSE_MAX_SECONDS, PAUSE_MIN_SECONDS, pause_seconds,
                       persist_chunks)
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


def _require_idle(conn, project_id: int) -> None:
    busy = conn.execute(
        "SELECT id FROM chapters WHERE project_id = ? "
        "AND processing_status IN ('generating', 'recasting')",
        (project_id,)).fetchone()
    if busy is not None:
        raise HTTPException(status_code=409,
                            detail="Another chapter is already generating — "
                                   "one at a time.")


def launch_generation(conn, user_id: int, project_id: int,
                      chapter: sqlite3.Row, status: str) -> None:
    """Shared launch path for Generate and recast. `status` is 'generating'
    or 'recasting' (§5 enum). Raises HTTPException on guard failure;
    commits and spawns the worker on success."""
    _require_idle(conn, project_id)

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
    # The fresh rows write fresh row-id-suffixed WAVs (_chunk_wav_path),
    # so the replaced version's files would pile up orphaned — sweep them.
    cdir = storage.chunks_dir(user_id, project_id, chapter["id"])
    if cdir.is_dir():
        for old in cdir.glob("*.wav"):
            old.unlink(missing_ok=True)
    _spawn(run_chapter(user_id, project_id, chapter["id"]))


def stage_chunk_edit(conn, user_id: int, project_id: int,
                     chapter: sqlite3.Row, chunk: sqlite3.Row,
                     new_texts: list[str]) -> None:
    """Chunk-level edit, staged: save the text, regenerate NOTHING yet.

    (2026-07-17, replacing save-and-regenerate-immediately: that flow
    locked the page for the regeneration and its reload discarded any
    unsaved typing in the OTHER chunks' textareas. Staged saves are
    instant, so editing many chunks in a row loses nothing.)

    The edited chunk drops to pending — shown as "needs regeneration" —
    until a per-chunk "save & regenerate now", the chapter's batch
    regenerate, or a full recast produces fresh audio. Nothing here
    re-chunks or bumps the chapter version: every other chunk's row and
    audio stay valid. Caller has already validated the texts against
    CHUNK_HARD_CAP and synced raw_text/source.txt. Returns the affected
    row ids so a save-and-regenerate can target exactly this edit.

    `new_texts` has one entry normally; several when the edit introduced
    [pause:Ns] markers — the edited row keeps the first piece and fresh
    rows are inserted after it (later chunks shift up to make room).
    Pause pieces cost nothing, but text pieces flanking a new pause DO
    regenerate: the old audio was one continuous take across the split.
    """
    _require_idle(conn, project_id)
    n = len(new_texts)
    # The paragraph turn sits after the whole edited region, so on a split
    # the flag rides to the LAST piece, not the row that keeps the first.
    gap_after = chunk["paragraph_gap_after"]
    if n > 1:
        conn.execute(
            """UPDATE chunks SET chunk_index = chunk_index + ?
               WHERE chapter_id = ? AND chunk_index > ?""",
            (n - 1, chapter["id"], chunk["chunk_index"]))
    conn.execute(
        """UPDATE chunks SET text = ?, status = 'pending',
               runpod_job_id = NULL, audio_file_path = NULL,
               paragraph_gap_after = ?,
               updated_at = datetime('now') WHERE id = ?""",
        (new_texts[0], gap_after if n == 1 else 0, chunk["id"]))
    ids = [chunk["id"]]
    for i, t in enumerate(new_texts[1:], start=1):
        cur = conn.execute(
            """INSERT INTO chunks (chapter_id, chapter_version, chunk_index,
                                   text, paragraph_gap_after)
               VALUES (?, ?, ?, ?, ?)""",
            (chapter["id"], chunk["chapter_version"], chunk["chunk_index"] + i,
             t, gap_after if i == n - 1 else 0))
        ids.append(cur.lastrowid)
    # Same semantics as a pause delete: approval and the assembled audio
    # are stale the moment the text changes; the chapter sits in the
    # unstitched ready_for_review state until regeneration.
    conn.execute(
        """UPDATE chapters SET processing_status = 'ready_for_review',
               approved = 0, approved_at = NULL,
               assembled_audio_path = NULL, assembled_at = NULL
           WHERE id = ?""", (chapter["id"],))
    conn.commit()
    # The superseded take must not stay playable (or land in a stitch).
    if chunk["audio_file_path"]:
        Path(chunk["audio_file_path"]).unlink(missing_ok=True)
    return ids


def launch_regen(conn, user_id: int, project_id: int, chapter_id: int,
                 total: int, chunk_ids: list[int] | None = None) -> None:
    """Regenerate staged chunks WITHOUT re-stitching (2026-07-17).

    chunk_ids limits the pass to one edit's rows (per-chunk "save &
    regenerate now" — listen before committing to a stitch); None means
    every non-done chunk in the chapter. Caller has run the guards and
    holds the transaction; this flips the busy state and spawns."""
    conn.execute(
        "UPDATE chapters SET processing_status = 'recasting' WHERE id = ?",
        (chapter_id,))
    conn.execute(
        """INSERT INTO generation_jobs (project_id, chapter_id, total_chunks)
           VALUES (?, ?, ?)""", (project_id, chapter_id, total))
    conn.execute("UPDATE projects SET status = 'generating', "
                 "updated_at = datetime('now') WHERE id = ?", (project_id,))
    conn.commit()
    _spawn(run_chapter(user_id, project_id, chapter_id, assemble=False,
                       chunk_ids=chunk_ids))


@router.post("/projects/{project_id}/chapters/{chapter_id}/regenerate")
async def regenerate_chapter_chunks(request: Request, project_id: int,
                                    chapter_id: int,
                                    user=Depends(auth.require_user)):
    """Batch-regenerate every chunk marked needs-regeneration (staged
    edits, retimed pauses, errored chunks) — no re-stitch, so each chunk
    can be auditioned before the final assembly."""
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        chapter = conn.execute(
            "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
            (chapter_id, project_id)).fetchone()
        if chapter is None:
            raise HTTPException(status_code=404)
        _require_idle(conn, project_id)
        pending = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE chapter_id = ? "
            "AND status != 'done'", (chapter_id,)).fetchone()[0]
        if pending == 0:
            raise HTTPException(status_code=422,
                                detail="No chunks are waiting for "
                                       "regeneration.")
        launch_regen(conn, user["id"], project_id, chapter_id, pending)
    finally:
        conn.close()
    return RedirectResponse(
        f"/projects/{project_id}/chapters/{chapter_id}/chunks", status_code=303)


@router.post("/projects/{project_id}/chapters/{chapter_id}/stitch")
async def stitch_chapter(request: Request, project_id: int, chapter_id: int,
                         user=Depends(auth.require_user)):
    """Re-stitch the chapter audio (2026-07-13; assemble-ONLY since
    2026-07-17). Refused while any chunk still needs regeneration —
    assembly includes only done chunks, so stitching early would silently
    drop the pending ones' audio. Regeneration is its own step (the
    per-chunk buttons or /regenerate). Never costs GPU."""
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        chapter = conn.execute(
            "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
            (chapter_id, project_id)).fetchone()
        if chapter is None:
            raise HTTPException(status_code=404)
        _require_idle(conn, project_id)
        n = conn.execute("SELECT COUNT(*) FROM chunks WHERE chapter_id = ?",
                         (chapter_id,)).fetchone()[0]
        if n == 0:
            raise HTTPException(status_code=422,
                                detail="No chunks to stitch — generate first.")
        pending = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE chapter_id = ? "
            "AND status != 'done'", (chapter_id,)).fetchone()[0]
        if pending:
            raise HTTPException(
                status_code=422,
                detail=f"{pending} chunk(s) still need regeneration — "
                       "regenerate them before stitching.")
        conn.execute("UPDATE chapters SET processing_status = 'recasting' "
                     "WHERE id = ?", (chapter_id,))
        conn.execute(
            """INSERT INTO generation_jobs (project_id, chapter_id, total_chunks)
               VALUES (?, ?, ?)""", (project_id, chapter_id, n))
        conn.execute("UPDATE projects SET status = 'generating', "
                     "updated_at = datetime('now') WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()
    _spawn(run_chapter(user["id"], project_id, chapter_id))
    return RedirectResponse(
        f"/projects/{project_id}/chapters/{chapter_id}/chunks", status_code=303)


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
        assembling = conn.execute(
            """SELECT 1 FROM generation_jobs WHERE project_id = ?
               AND chapter_id IS NULL AND status = 'running'""",
            (project_id,)).fetchone() is not None
    finally:
        conn.close()
    return {"assembling": assembling, "chapters": [
        {"id": r["id"], "status": r["processing_status"],
         "approved": bool(r["approved"]),
         "chunks_done": r["done"] or 0, "chunks_total": r["total"]}
        for r in rows]}


# --- worker -------------------------------------------------------------------

async def run_chapter(user_id: int, project_id: int, chapter_id: int,
                      assemble: bool = True,
                      chunk_ids: list[int] | None = None) -> None:
    """Generate every unfinished chunk of one chapter, then finalize.

    assemble=False (chunk-level edits, 2026-07-13): skip the chapter
    re-stitch — a full AAC re-encode that dwarfs a single chunk's
    generation time — so consecutive edits stay fast. The user triggers
    one explicit re-stitch when done (the /stitch route), which runs this
    same worker with no pending chunks and assemble=True.

    chunk_ids (2026-07-17): restrict the pass to those rows — the
    per-chunk "save & regenerate now". Other staged chunks stay pending
    on purpose (the user hasn't paid for them yet), so finalize must not
    treat them as a failure.
    """
    try:
        voice_path = storage.voice_dir(user_id, project_id) / "reference.wav"
        voice_b64 = base64.b64encode(voice_path.read_bytes()).decode()

        sql = ("SELECT * FROM chunks WHERE chapter_id = ? AND status != 'done'")
        params: list = [chapter_id]
        if chunk_ids:
            sql += f" AND id IN ({','.join('?' * len(chunk_ids))})"
            params += chunk_ids
        conn = db.connect()
        try:
            chunks = conn.execute(sql + " ORDER BY chunk_index",
                                  params).fetchall()
        finally:
            conn.close()

        sem = asyncio.Semaphore(settings.runpod_concurrency)

        async def bounded(chunk):
            async with sem:
                return await _process_chunk(user_id, project_id, chunk, voice_b64)

        results = await asyncio.gather(*(bounded(c) for c in chunks),
                                       return_exceptions=True)
        ok = all(r is True for r in results)
        if ok and assemble and _all_chunks_done(chapter_id):
            # Phase 8: assemble the chapter the moment its last chunk lands.
            ok = await assembly.assemble_chapter(user_id, project_id, chapter_id)
    except Exception as exc:  # voice file missing, DB trouble, ...
        print(f"generation fatal for chapter {chapter_id}: {exc!r}", flush=True)
        ok = False
    _finalize_chapter(project_id, chapter_id, ok,
                      require_all_done=chunk_ids is None)


def _chunk_wav_path(user_id: int, project_id: int, chunk: sqlite3.Row):
    """Where a chunk's audio is written. The row-id suffix makes names
    collision-proof: a pause splice shifts later chunks' indexes, so two
    live rows can otherwise claim the same index-derived name and one
    would overwrite the other's audio. audio_file_path in the DB is
    authoritative; pre-suffix files (0000.wav) stay valid until replaced."""
    return storage.chunks_dir(user_id, project_id, chunk["chapter_id"]) \
        / f"{chunk['chunk_index']:04d}-{chunk['id']}.wav"


async def _process_pause_chunk(user_id: int, project_id: int,
                               chunk: sqlite3.Row, seconds: float) -> bool:
    """A [pause:Ns] chunk: synthesize silence locally — no RunPod, free.

    Format matches the Chatterbox endpoint's output exactly (pcm_f32le,
    mono, 24 kHz — runpod/RESULTS.md) so the concat stitch treats it like
    any other chunk WAV. Bounds are validated at edit time; the clamp here
    is the backstop for markers arriving via chapter upload.
    """
    seconds = min(max(seconds, PAUSE_MIN_SECONDS), PAUSE_MAX_SECONDS)
    path = _chunk_wav_path(user_id, project_id, chunk)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", f"{seconds:.3f}", "-c:a", "pcm_f32le", str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        print(f"pause chunk {chunk['id']} silence failed: "
              f"{stderr.decode(errors='replace')[:300]}", flush=True)
        _update_chunk(chunk, "error")
        return False
    if not _update_chunk(chunk, "done", audio_file_path=str(path)):
        path.unlink(missing_ok=True)  # stale version's audio
        return True
    _bump_job_progress(chunk["chapter_id"])
    return True


async def _process_chunk(user_id: int, project_id: int,
                         chunk: sqlite3.Row, voice_b64: str) -> bool:
    """Drive one chunk to done. Returns True on success."""
    pause = pause_seconds(chunk["text"])
    if pause is not None:
        return await _process_pause_chunk(user_id, project_id, chunk, pause)
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
            path = _chunk_wav_path(user_id, project_id, chunk)
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


def _finalize_chapter(project_id: int, chapter_id: int, ok: bool,
                      require_all_done: bool = True) -> None:
    conn = db.connect()
    try:
        # Trust the DB, not the in-memory flag alone: every chunk must be
        # done — except after a targeted per-chunk regen, where OTHER
        # chunks legitimately stay staged ("needs regeneration").
        remaining = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE chapter_id = ? AND status != 'done'",
            (chapter_id,)).fetchone()[0]
        success = ok and (remaining == 0 or not require_all_done)
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
