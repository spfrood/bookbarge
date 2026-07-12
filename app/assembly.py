"""Per-chapter audio assembly (PROJECT_BIBLE.md §2 step 7, §9).

The moment a chapter's last chunk reaches 'done', its chunk WAVs are
concatenated with FFmpeg into assembled.mp3 in the chapter directory.
Plain concat, no crossfade — verified seamless by ear in Phase 1
(runpod/RESULTS.md). MP3 at VBR q2 (~190 kbps): universally playable,
generous for 24 kHz mono narration.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from mutagen.mp4 import MP4

from . import auth, db, storage

router = APIRouter()


async def assemble_chapter(user_id: int, project_id: int, chapter_id: int) -> bool:
    """Concatenate a chapter's done chunks into assembled.mp3.

    Returns True on success (DB fields updated), False otherwise.
    Chunk order comes from chunk_index in the DB, not directory listing.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT chunk_index, audio_file_path FROM chunks
               WHERE chapter_id = ? AND status = 'done' ORDER BY chunk_index""",
            (chapter_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return False
    paths = [Path(r["audio_file_path"]) for r in rows]
    if any(not p.is_file() for p in paths):
        print(f"assembly: missing chunk file(s) for chapter {chapter_id}",
              flush=True)
        return False

    out_path = storage.chapter_dir(user_id, project_id, chapter_id) / "assembled.mp3"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("".join(f"file '{p}'\n" for p in paths))
        list_path = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-codec:a", "libmp3lame", "-qscale:a", "2", str(out_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            print(f"assembly failed for chapter {chapter_id}: "
                  f"{stderr.decode(errors='replace')[:500]}", flush=True)
            out_path.unlink(missing_ok=True)
            return False
    finally:
        Path(list_path).unlink(missing_ok=True)

    conn = db.connect()
    try:
        # Guard on status: if a recast started while we encoded, the new
        # version owns this chapter now — don't stamp stale assembly onto it.
        cur = conn.execute(
            """UPDATE chapters SET assembled_audio_path = ?,
                                   assembled_at = datetime('now')
               WHERE id = ? AND processing_status IN ('generating', 'recasting')""",
            (str(out_path), chapter_id))
        conn.commit()
        if cur.rowcount == 0:
            out_path.unlink(missing_ok=True)
            return False
    finally:
        conn.close()
    return True


# --- final audiobook assembly (PROJECT_BIBLE.md §2 steps 10-11) -----------------

def _ffmeta_escape(value: str) -> str:
    for ch in "\\=;#\n":
        value = value.replace(ch, f"\\{ch}" if ch != "\n" else "\\\n")
    return value


async def _duration_ms(path: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return round(float(json.loads(out)["format"]["duration"]) * 1000)


@router.post("/projects/{project_id}/assemble")
async def assemble_audiobook_route(request: Request, project_id: int,
                                   user=Depends(auth.require_user)):
    from .projects import get_owned_project
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        # The gate, evaluated fresh at click time (§9): every chapter must
        # be approved right now — button state in the UI is cosmetic only.
        total, approved = conn.execute(
            """SELECT COUNT(*), SUM(CASE WHEN approved THEN 1 ELSE 0 END)
               FROM chapters WHERE project_id = ?""", (project_id,)).fetchone()
        if total == 0 or (approved or 0) < total:
            raise HTTPException(
                status_code=409,
                detail=f"All chapters must be approved ({approved or 0}/{total}).")
        busy = conn.execute(
            """SELECT 1 FROM generation_jobs WHERE project_id = ?
               AND chapter_id IS NULL AND status = 'running'""",
            (project_id,)).fetchone()
        if busy:
            raise HTTPException(status_code=409, detail="Already assembling.")
        conn.execute(
            """INSERT INTO generation_jobs (project_id, chapter_id, total_chunks)
               VALUES (?, NULL, ?)""", (project_id, total))
        conn.commit()
    finally:
        conn.close()

    from .generation import _spawn
    _spawn(assemble_audiobook(user["id"], project_id))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


async def assemble_audiobook(user_id: int, project_id: int) -> bool:
    """Concatenate approved chapters into output/audiobook.m4b with
    embedded chapter markers (ffmetadata)."""
    conn = db.connect()
    try:
        project = conn.execute("SELECT * FROM projects WHERE id = ?",
                               (project_id,)).fetchone()
        chapters = conn.execute(
            """SELECT * FROM chapters WHERE project_id = ? AND approved = 1
               ORDER BY chapter_number""", (project_id,)).fetchall()
    finally:
        conn.close()

    ok = False
    out_path = storage.output_dir(user_id, project_id) / "audiobook.m4b"
    try:
        paths = [Path(c["assembled_audio_path"] or "") for c in chapters]
        if not paths or any(not p.is_file() for p in paths):
            raise RuntimeError("missing chapter audio")

        # Chapter markers: cumulative offsets from real durations.
        meta = [";FFMETADATA1",
                f"title={_ffmeta_escape(project['title'])}",
                f"album={_ffmeta_escape(project['title'])}"]
        if project["author"]:
            meta.append(f"artist={_ffmeta_escape(project['author'])}")
        start = 0
        for c, p in zip(chapters, paths):
            end = start + await _duration_ms(p)
            title = (c["title"] or Path(c["filename"] or "").stem
                     or f"Chapter {c['chapter_number']}")
            meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}",
                     f"END={end}", f"title={_ffmeta_escape(title)}"]
            start = end

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("".join(f"file '{p}'\n" for p in paths))
            list_path = f.name
        with tempfile.NamedTemporaryFile("w", suffix=".meta", delete=False) as f:
            f.write("\n".join(meta) + "\n")
            meta_path = f.name
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", list_path,
                "-i", meta_path, "-map_metadata", "1", "-map", "0:a",
                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                "-f", "mp4", str(out_path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace")[:500])
        finally:
            Path(list_path).unlink(missing_ok=True)
            Path(meta_path).unlink(missing_ok=True)

        # FFmpeg can't write the iTunes media-kind atom, and without
        # stik=audiobook Apple Books refuses to import the file (it plays
        # in generic players but isn't recognized as an audiobook).
        m4b = MP4(out_path)
        m4b["stik"] = [2]   # media kind: audiobook
        m4b["pgap"] = [True]
        if project["narrator"]:
            # Audiobook apps read the narrator from composer (©wrt);
            # album-artist is a common secondary.
            m4b["\xa9wrt"] = [project["narrator"]]
            m4b["aART"] = [project["narrator"]]
        m4b.save()
        ok = True
    except Exception as exc:
        print(f"final assembly failed for project {project_id}: {exc!r}",
              flush=True)
        out_path.unlink(missing_ok=True)

    conn = db.connect()
    try:
        conn.execute(
            """UPDATE generation_jobs SET status = ?, completed_at = datetime('now')
               WHERE project_id = ? AND chapter_id IS NULL AND status = 'running'""",
            ("completed" if ok else "error", project_id))
        if ok:
            conn.execute(
                "UPDATE projects SET status = 'assembled', "
                "updated_at = datetime('now') WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()
    return ok
