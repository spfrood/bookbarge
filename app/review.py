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

import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from . import auth, db, generation, storage
from .chunking import (CHUNK_HARD_CAP, PAUSE_MAX_SECONDS, PAUSE_MIN_SECONDS,
                       pause_seconds, split_pause_segments)
from .config import settings
from .projects import MAX_CHAPTER_BYTES, get_owned_project

router = APIRouter()
templates: Jinja2Templates = None  # set by main.py

BUSY = ("generating", "recasting")


def _sibling_busy(conn: sqlite3.Connection, project_id: int,
                  chapter_id: int) -> bool:
    """True when ANOTHER chapter in the project is generating — the
    one-at-a-time rule then locks this chapter's edit/recast forms."""
    return conn.execute(
        """SELECT 1 FROM chapters WHERE project_id = ? AND id != ?
           AND processing_status IN ('generating', 'recasting')""",
        (project_id, chapter_id)).fetchone() is not None

# Anything [pause…]-shaped that split_pause_segments did NOT extract is a
# typo ([pause 2s], [pause:2,4s]…) — reject it rather than let Chatterbox
# read it aloud as if it were a paralinguistic tag.
_MALFORMED_PAUSE_RE = re.compile(r"\[\s*pause[^\]]*\]", re.IGNORECASE)


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
                                detail="No chapter audio to approve — "
                                       "generate (or re-stitch) first.")
        now_approved = 0 if chapter["approved"] else 1
        conn.execute(
            """UPDATE chapters SET approved = ?,
                   approved_at = CASE WHEN ? THEN datetime('now') ELSE NULL END
               WHERE id = ?""", (now_approved, now_approved, chapter_id))
        if now_approved:
            # Reclaim the per-chunk working audio. Approval means the
            # chapter's assembled.m4a is the deliverable — and the final
            # M4B is built from those .m4a files, not the chunk WAVs — so
            # the chunks (uncompressed float32, by far the bulk of a
            # project's disk footprint) are now dead weight. Drop their
            # rows and files here; source.txt/raw_text stay, so unapproving
            # and regenerating (Edit text / Replace file / Regenerate all)
            # rebuilds the chunks from scratch. assembled.m4a is untouched
            # and stays fully playable. Reclaim only runs while assembled
            # audio exists (guarded above) and never while busy, so no
            # in-flight chunk work can be lost. Per-chunk editing and
            # re-stitch need the WAVs and so are unavailable until a
            # regenerate — the chunks page explains that state.
            conn.execute("DELETE FROM chunks WHERE chapter_id = ?",
                         (chapter_id,))
        conn.commit()
        if now_approved:
            cdir = storage.chunks_dir(user["id"], project_id, chapter_id)
            if cdir.is_dir():
                for wav in cdir.glob("*.wav"):
                    wav.unlink(missing_ok=True)
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


@router.post("/projects/{project_id}/chapters/{chapter_id}/delete")
async def delete_chapter(request: Request, project_id: int, chapter_id: int,
                         user=Depends(auth.require_user)):
    """Remove a chapter outright: row (chunks cascade), audio, source.txt.

    Refused while anything in the project is generating or the final
    audiobook is assembling — deletion mid-flight would yank files a
    worker is using. Remaining chapters renumber to stay contiguous;
    an existing M4B is untouched (re-assemble to drop the chapter)."""
    conn = db.connect()
    try:
        _get_chapter(conn, user, project_id, chapter_id)
        generation._require_idle(conn, project_id)
        assembling = conn.execute(
            """SELECT 1 FROM generation_jobs WHERE project_id = ?
               AND chapter_id IS NULL AND status = 'running'""",
            (project_id,)).fetchone()
        if assembling:
            raise HTTPException(status_code=409,
                                detail="Final audiobook is assembling — "
                                       "wait for it to finish.")
        number = conn.execute(
            "SELECT chapter_number FROM chapters WHERE id = ?",
            (chapter_id,)).fetchone()[0]
        conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
        conn.execute(
            """UPDATE chapters SET chapter_number = chapter_number - 1
               WHERE project_id = ? AND chapter_number > ?""",
            (project_id, number))
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,))
        conn.commit()
    finally:
        conn.close()
    storage.delete_chapter_tree(user["id"], project_id, chapter_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


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
        sibling_busy = _sibling_busy(conn, project_id, chapter_id)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "chapter.html", {
        "user": user, "project": project, "chapter": chapter,
        "sibling_busy": sibling_busy,
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
        # Project-wide one-at-a-time guard must run BEFORE the destructive
        # work below: launch_generation re-checks it, but by then this
        # chapter's audio files are already deleted while the DB update
        # rolls back — an unrecoverable DB/filesystem mismatch.
        generation._require_idle(conn, project_id)
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
        (chapter_dir / "assembled.m4a").unlink(missing_ok=True)
        (chapter_dir / "assembled.mp3").unlink(missing_ok=True)  # pre-AAC-switch
        for wav in storage.chunks_dir(user["id"], project_id, chapter_id).glob("*.wav"):
            wav.unlink()

        fresh = conn.execute("SELECT * FROM chapters WHERE id = ?",
                             (chapter_id,)).fetchone()
        generation.launch_generation(conn, user["id"], project_id, fresh,
                                     status="recasting")
    finally:
        conn.close()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# --- chunk-level editing (2026-07-12; staged saves 2026-07-17) -------------------
# Fix one flubbed line without re-paying to regenerate the whole chapter.
# Saving a chunk is instant and STAGED: the text is stored and the chunk
# marked pending ("needs regeneration"); nothing hits RunPod until the
# user triggers the batch regenerate/re-stitch. (Immediate regeneration
# locked the page and its reload discarded unsaved edits in other
# chunks' textareas.) Saving an unchanged chunk is allowed on purpose —
# generation is stochastic, so it works as a "re-roll this take" marker.

def _chunk_rows(conn: sqlite3.Connection, chapter_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chunks WHERE chapter_id = ? ORDER BY chunk_index",
        (chapter_id,)).fetchall()


def _render_chunks(request, conn, user, project_id, chapter, error=None):
    project = conn.execute("SELECT * FROM projects WHERE id = ?",
                           (project_id,)).fetchone()
    voice = conn.execute(
        "SELECT id FROM voice_references WHERE project_id = ?",
        (project_id,)).fetchone()
    chunks = [dict(r) for r in _chunk_rows(conn, chapter["id"])]
    for k in chunks:
        k["is_pause"] = pause_seconds(k["text"]) is not None
    return templates.TemplateResponse(request, "chunks.html", {
        "user": user, "project": project, "chapter": chapter,
        "chunks": chunks, "cap": CHUNK_HARD_CAP,
        "pause_min": PAUSE_MIN_SECONDS, "pause_max": PAUSE_MAX_SECONDS,
        "paragraph_gap": settings.paragraph_gap_seconds,
        "sibling_busy": _sibling_busy(conn, project_id, chapter["id"]),
        "has_voice": voice is not None, "error": error})


@router.get("/projects/{project_id}/chapters/{chapter_id}/chunks")
async def chunks_page(request: Request, project_id: int, chapter_id: int,
                      user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        chapter = _get_chapter(conn, user, project_id, chapter_id)
        return _render_chunks(request, conn, user, project_id, chapter)
    finally:
        conn.close()


@router.get("/projects/{project_id}/chapters/{chapter_id}/chunks/{chunk_id}/audio")
async def chunk_audio(request: Request, project_id: int, chapter_id: int,
                      chunk_id: int, user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        _get_chapter(conn, user, project_id, chapter_id)
        chunk = conn.execute(
            "SELECT audio_file_path FROM chunks WHERE id = ? AND chapter_id = ?",
            (chunk_id, chapter_id)).fetchone()
    finally:
        conn.close()
    if chunk is None or not chunk["audio_file_path"]:
        raise HTTPException(status_code=404)
    path = Path(chunk["audio_file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404)
    # no-store: the file is replaced in place on re-roll; without this,
    # browsers heuristically cache and replay the pre-edit audio.
    return FileResponse(path, media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


def _splice_edit(span: str, old_words: list[str], new_words: list[str]) -> str:
    """Rewrite `span` (original chapter text whose words are exactly
    `old_words`) to carry `new_words`, touching only the changed words.

    Whitespace — including paragraph breaks, which chunks routinely
    straddle — is preserved everywhere except inside the edited region
    itself, where the new words are joined by single spaces.
    """
    limit = min(len(old_words), len(new_words))
    p = 0
    while p < limit and old_words[p] == new_words[p]:
        p += 1
    s = 0
    while s < limit - p and old_words[-1 - s] == new_words[-1 - s]:
        s += 1

    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", span)]
    start = tokens[p - 1][1] if p else 0            # end of kept prefix
    end = tokens[len(tokens) - s][0] if s else len(span)  # start of kept suffix
    core = " ".join(new_words[p:len(new_words) - s])
    core_replaces = p + s < len(tokens)             # old words being swapped out

    if core and core_replaces:
        lead = span[start:tokens[p][0]] if p else ""
        trail = span[tokens[len(tokens) - s - 1][1]:end] if s else ""
        mid = lead + core + trail
    elif core:                                      # pure insertion between kept words
        gap = span[start:end]
        if p == 0:
            mid = core + (gap or " ")
        elif s == 0:
            mid = (gap or " ") + core
        else:
            mid = " " + core + gap
    elif core_replaces:                             # pure deletion
        lead = span[start:tokens[p][0]] if p else ""
        trail = span[tokens[len(tokens) - s - 1][1]:end] if s else ""
        if p and s:
            mid = trail if "\n" in trail else lead if "\n" in lead else " "
        else:
            mid = ""
    else:                                           # identical text
        return span
    return span[:start] + mid + span[end:]


def _sync_raw_text(conn: sqlite3.Connection, user_id: int, project_id: int,
                   chapter: sqlite3.Row, chunk: sqlite3.Row,
                   new_text: str) -> None:
    """Keep raw_text/source.txt authoritative after a chunk edit, so a
    later full recast doesn't silently resurrect the pre-edit wording.

    The chunk's text is the chapter text with whitespace normalized, so a
    whitespace-tolerant match locates its span in raw_text, and the edit
    is spliced in without disturbing surrounding formatting. If the match
    ever fails, fall back to rebuilding raw_text from the chunk texts —
    formatting is lost but the words stay truthful, which matters more.

    Chunks with identical text repeat — [pause:2s] markers routinely do —
    so the span is the Nth match, N counting earlier chunks with the same
    text; blindly taking the first match would edit the wrong pause.
    """
    raw = chapter["raw_text"] or ""
    old_words = chunk["text"].split()
    pattern = r"\s+".join(re.escape(w) for w in old_words)
    nth = conn.execute(
        """SELECT COUNT(*) FROM chunks WHERE chapter_id = ?
           AND chunk_index < ? AND text = ?""",
        (chapter["id"], chunk["chunk_index"], chunk["text"])).fetchone()[0]
    matches = list(re.finditer(pattern, raw))
    if len(matches) > nth:
        m = matches[nth]
        new_span = _splice_edit(m.group(0), old_words, new_text.split())
        new_raw = raw[:m.start()] + new_span + raw[m.end():]
    else:
        texts = [new_text if r["id"] == chunk["id"] else r["text"]
                 for r in _chunk_rows(conn, chapter["id"])]
        new_raw = "\n\n".join(t for t in texts if t)
        print(f"chunk edit: raw_text match failed for chunk {chunk['id']}, "
              "rebuilt from chunks", flush=True)
    conn.execute("UPDATE chapters SET raw_text = ? WHERE id = ?",
                 (new_raw, chapter["id"]))
    chapter_dir = storage.chapter_dir(user_id, project_id, chapter["id"])
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / "source.txt").write_text(new_raw, encoding="utf-8")


def _delete_pause_chunk(conn: sqlite3.Connection, user_id: int,
                        project_id: int, chapter: sqlite3.Row,
                        chunk: sqlite3.Row) -> None:
    """Remove a pause chunk (saved empty). No generation job runs — every
    other chunk's audio is untouched — but the assembled chapter audio is
    now stale, so the chapter lands in the unstitched ready_for_review
    state and the page offers a re-stitch."""
    _sync_raw_text(conn, user_id, project_id, chapter, chunk, "")
    conn.execute("DELETE FROM chunks WHERE id = ?", (chunk["id"],))
    if chunk["paragraph_gap_after"]:
        # The paragraph turn outlives the pause that sat on it — hand the
        # flag back to the previous chunk so the automatic gap resumes.
        conn.execute(
            """UPDATE chunks SET paragraph_gap_after = 1
               WHERE chapter_id = ? AND chunk_index = ?""",
            (chapter["id"], chunk["chunk_index"] - 1))
    conn.execute(
        """UPDATE chunks SET chunk_index = chunk_index - 1
           WHERE chapter_id = ? AND chunk_index > ?""",
        (chapter["id"], chunk["chunk_index"]))
    conn.execute(
        """UPDATE chapters SET processing_status = 'ready_for_review',
               approved = 0, approved_at = NULL,
               assembled_audio_path = NULL, assembled_at = NULL
           WHERE id = ?""", (chapter["id"],))
    conn.commit()
    if chunk["audio_file_path"]:
        Path(chunk["audio_file_path"]).unlink(missing_ok=True)
    chapter_dir = storage.chapter_dir(user_id, project_id, chapter["id"])
    (chapter_dir / "assembled.m4a").unlink(missing_ok=True)
    (chapter_dir / "assembled.mp3").unlink(missing_ok=True)


@router.post("/projects/{project_id}/chapters/{chapter_id}/chunks/{chunk_id}")
async def edit_chunk(request: Request, project_id: int, chapter_id: int,
                     chunk_id: int, user=Depends(auth.require_user),
                     # Default (not required): browsers submit an empty
                     # textarea as blank, which FastAPI treats as missing —
                     # and empty is meaningful here (deletes a pause chunk).
                     text: str = Form(""),
                     # "save" stages the edit; "regen" also regenerates
                     # exactly this edit's chunks right away (no stitch) so
                     # the new take can be auditioned before assembly.
                     action: str = Form("save")):
    # Same normalization the chunker applies, so the cap check is honest.
    normalized = " ".join(text.split())
    conn = db.connect()
    try:
        chapter = _get_chapter(conn, user, project_id, chapter_id)
        # Project-wide guard up front: source.txt and the assembled audio
        # are touched below, and those must not happen if a 409 aborts.
        generation._require_idle(conn, project_id)
        chunk = conn.execute(
            "SELECT * FROM chunks WHERE id = ? AND chapter_id = ?",
            (chunk_id, chapter_id)).fetchone()
        if chunk is None:
            raise HTTPException(status_code=404)
        if not normalized:
            # Saving a pause chunk empty deletes it — the documented way
            # to remove a pause. Needs no voice and no generation job.
            if pause_seconds(chunk["text"]) is not None:
                _delete_pause_chunk(conn, user["id"], project_id, chapter,
                                    chunk)
                return RedirectResponse(
                    f"/projects/{project_id}/chapters/{chapter_id}/chunks",
                    status_code=303)
            return _render_chunks(request, conn, user, project_id, chapter,
                                  error="Chunk text can't be empty.")
        voice = conn.execute(
            "SELECT id FROM voice_references WHERE project_id = ?",
            (project_id,)).fetchone()
        if voice is None:
            return _render_chunks(request, conn, user, project_id, chapter,
                                  error="Upload a voice reference first.")

        # [pause:Ns] markers split the text: each marker becomes its own
        # silence chunk, so the cap applies per text piece between them.
        segments = split_pause_segments(normalized)
        for seg in segments:
            dur = pause_seconds(seg)
            if dur is not None:
                if not PAUSE_MIN_SECONDS <= dur <= PAUSE_MAX_SECONDS:
                    return _render_chunks(
                        request, conn, user, project_id, chapter,
                        error=f"[pause:{dur:g}s] is out of range — pauses "
                              f"must be {PAUSE_MIN_SECONDS:g} to "
                              f"{PAUSE_MAX_SECONDS:g} seconds.")
            elif _MALFORMED_PAUSE_RE.search(seg):
                return _render_chunks(
                    request, conn, user, project_id, chapter,
                    error="Unrecognized pause marker — write it exactly as "
                          "[pause:2.4s], with no spaces inside the brackets.")
            elif len(seg) > CHUNK_HARD_CAP:
                where = ("A text piece between pauses" if len(segments) > 1
                         else "Chunk")
                return _render_chunks(
                    request, conn, user, project_id, chapter,
                    error=f"{where} is {len(seg)} characters — the limit is "
                          f"{CHUNK_HARD_CAP}. Audio degrades past that; trim "
                          "the text (or move words to a neighboring chunk).")

        if normalized != chunk["text"]:
            _sync_raw_text(conn, user["id"], project_id, chapter, chunk,
                           normalized)
        # The assembled chapter audio is stale the moment a chunk changes.
        chapter_dir = storage.chapter_dir(user["id"], project_id, chapter_id)
        (chapter_dir / "assembled.m4a").unlink(missing_ok=True)
        (chapter_dir / "assembled.mp3").unlink(missing_ok=True)

        ids = generation.stage_chunk_edit(conn, user["id"], project_id,
                                          chapter, chunk, segments)
        if action == "regen":
            generation.launch_regen(conn, user["id"], project_id, chapter_id,
                                    total=len(ids), chunk_ids=ids)
    finally:
        conn.close()
    return RedirectResponse(
        f"/projects/{project_id}/chapters/{chapter_id}/chunks",
        status_code=303)
