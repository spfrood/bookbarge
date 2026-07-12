"""Per-chapter audio assembly (PROJECT_BIBLE.md §2 step 7, §9).

The moment a chapter's last chunk reaches 'done', its chunk WAVs are
concatenated with FFmpeg into assembled.mp3 in the chapter directory.
Plain concat, no crossfade — verified seamless by ear in Phase 1
(runpod/RESULTS.md). MP3 at VBR q2 (~190 kbps): universally playable,
generous for 24 kHz mono narration.
"""

import asyncio
import tempfile
from pathlib import Path

from . import db, storage


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
