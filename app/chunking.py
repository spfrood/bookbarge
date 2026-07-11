"""Sentence-aware text chunking for TTS generation.

Splits chapter text into chunks that each stay under Chatterbox Turbo's
usable input ceiling, measured empirically in Phase 1 (runpod/RESULTS.md):
audio becomes unintelligible past ~30s, pacing runs ~6-7.4s per 100 chars
with ±20% run-to-run variance, so the hard cap is 350 characters.

Rules (PROJECT_BIBLE.md §7 + build-order step 6):
- Sentence boundaries come from nltk punkt (handles Mr./Dr./U.S. etc.);
  chunks never cut mid-sentence unless a single sentence exceeds the cap.
- Over-cap sentences split at clause punctuation first, whitespace last.
- Bracketed paralinguistic tags ([sigh], [clear throat], ...) are atomic:
  passed through byte-for-byte, never split, stripped, or escaped, even
  multi-word tags. They ride with whatever chunk their sentence lands in.
"""

import re
import sqlite3

import nltk

CHUNK_HARD_CAP = 350   # chars — measured Phase 1 ceiling, do not raise casually
CHUNK_TARGET_MIN = 250

# A tag is atomic even when it contains spaces ("[clear throat]").
_ATOM_RE = re.compile(r"\[[^\]]*\]|\S+")
_CLAUSE_END = (",", ";", ":", "—", "–")


def _ensure_punkt() -> None:
    try:
        nltk.data.find("tokenizers/punkt_tab")
    except LookupError:
        nltk.download("punkt_tab", quiet=True)


def _split_long_sentence(sentence: str, cap: int) -> list[str]:
    """Split an over-cap sentence at clause punctuation, then whitespace.

    Works on atoms (words or whole [tags]), so a tag can never be cut.
    """
    atoms = _ATOM_RE.findall(sentence)
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for atom in atoms:
        added = length + (1 if current else 0) + len(atom)
        if current and added > cap:
            # Prefer to break after the last clause-ending atom, provided
            # that keeps the piece a useful size.
            split_at = len(current)
            for i in range(len(current) - 1, -1, -1):
                head = " ".join(current[: i + 1])
                if current[i].endswith(_CLAUSE_END) and len(head) >= cap // 2:
                    split_at = i + 1
                    break
            pieces.append(" ".join(current[:split_at]))
            current = current[split_at:]
            length = len(" ".join(current))
        current.append(atom)
        length = len(" ".join(current))
    if current:
        pieces.append(" ".join(current))
    return pieces


def chunk_text(text: str, cap: int = CHUNK_HARD_CAP) -> list[str]:
    """Split chapter text into TTS-ready chunks of at most `cap` chars."""
    _ensure_punkt()
    # Normalize whitespace: TTS gets no meaning from layout, and uniform
    # spacing makes the cap arithmetic exact. Tags are unaffected.
    normalized = " ".join(text.split())
    if not normalized:
        return []

    units: list[str] = []
    for sentence in nltk.sent_tokenize(normalized):
        if len(sentence) <= cap:
            units.append(sentence)
        else:
            units.extend(_split_long_sentence(sentence, cap))

    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}" if current else unit
        if current and len(candidate) > cap:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def persist_chunks(conn: sqlite3.Connection, chapter: sqlite3.Row) -> int:
    """(Re)create chunk rows for a chapter's current text and version.

    Recast semantics (PROJECT_BIBLE.md §5): existing chunk rows are deleted
    outright and fresh ones inserted stamped with the chapter's current
    version. Returns the number of chunks created. Caller commits.
    """
    texts = chunk_text(chapter["raw_text"] or "")
    conn.execute("DELETE FROM chunks WHERE chapter_id = ?", (chapter["id"],))
    conn.executemany(
        """INSERT INTO chunks (chapter_id, chapter_version, chunk_index, text)
           VALUES (?, ?, ?, ?)""",
        [(chapter["id"], chapter["version"], i, t) for i, t in enumerate(texts)],
    )
    return len(texts)
