"""EPUB import: parse a DRM-free EPUB into chapter candidates, let the
user review them (include/exclude, retitle), then create chapters exactly
as if they'd been uploaded as .txt files.

Import is a two-step flow, matching the review-everything philosophy:

  1. POST /projects/{id}/import-epub    — parse, stash the file as
     import.epub in the project dir, render the preview page.
  2. POST /projects/{id}/import-epub/confirm — re-parse the stashed file,
     create the selected chapters (titles land in chapters.title, so M4B
     chapter markers come out right), delete the stash.

Parsing is stdlib-only on purpose: an EPUB is a ZIP of XHTML plus XML
manifests (zipfile + xml.etree + html.parser cover it), and the popular
parsing library (ebooklib) is AGPL — a poor fit as a dependency of an
MIT-licensed repo. Kindle formats and PDF are out of scope: convert to
EPUB externally (Calibre does it in one click).

Structure mapping:
- Spine order = chapter order; one spine document = one chapter candidate
  (linear="no" auxiliary items are skipped) — UNLESS the TOC points at
  several anchors inside the same document (Project Gutenberg packs ~10
  chapters per file), in which case the document is split at those anchor
  positions into one candidate per TOC entry.
- Titles come from the TOC (EPUB3 nav doc, falling back to EPUB2 NCX),
  then the document's first heading, then the filename.
- Block elements become blank-line-separated paragraphs — which feeds the
  automatic paragraph-gap pacing directly (see chunking.py).
- <sup> subtrees and bracketed footnote refs like [12] are stripped: read
  aloud they're noise, and stray [12] risks colliding with Bookbarge's
  bracket-tag handling.
- Front/back matter (cover, TOC, copyright, ... or nearly empty docs) is
  detected heuristically and merely pre-UNCHECKED on the preview — the
  user always has the final say.
"""

import html.parser
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urldefrag

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from . import auth, db, storage
from .projects import MAX_CHAPTER_BYTES, _project_error, get_owned_project

router = APIRouter()
templates: Jinja2Templates = None  # set by main.py

MAX_EPUB_BYTES = 50 * 1024 * 1024   # images we won't use dominate EPUB size
IMPORT_FILENAME = "import.epub"     # stash between preview and confirm

_CONTAINER = "META-INF/container.xml"
_ENCRYPTION = "META-INF/encryption.xml"
_NS = {
    "cnt": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
}
_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"

# Titles/filenames/guide-types that look like front or back matter. Only
# affects the preview's default checkbox state, never a hard exclusion.
_MATTER_RE = re.compile(
    r"cover|title[-_ ]?page|halftitle|copyright|colophon|imprint|"
    r"^contents$|^toc$|table of contents|dedication|epigraph|"
    r"acknowledg|about the author|also by|gutenberg", re.IGNORECASE)
_MATTER_MIN_WORDS = 50
_FOOTNOTE_REF_RE = re.compile(r"\s*\[\d{1,3}\]")
# Gutenberg TOC labels prepend illustration captions ("He rode a black
# horse. CHAPTER III."); when a label ENDS in a plain chapter designation,
# the designation is the real title.
_TITLE_CHAPTER_RE = re.compile(r"\bchapter\s+[ivxlcdm0-9]+\.?$", re.IGNORECASE)


class EpubError(ValueError):
    """Parse-level failure with a message safe to show the user."""


# --- XHTML → paragraphs ---------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "li", "dt",
               "dd", "td", "th", "tr", "figcaption", "hr", "pre",
               "h1", "h2", "h3", "h4", "h5", "h6"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# sup: overwhelmingly footnote reference markers in books — reading "1"
# mid-sentence is worse than losing a rare legitimate superscript.
_SKIP_TAGS = {"script", "style", "head", "title", "sup"}


class _TextExtractor(html.parser.HTMLParser):
    """Flatten XHTML into (paragraph, is_heading) pairs.

    Every block element boundary flushes a paragraph; inline markup just
    contributes its text. html.parser tolerates the mildly broken markup
    real EPUBs contain, where an XML parser would refuse.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[str, bool]] = []
        # element id → index of the paragraph it lands in (or precedes):
        # lets TOC fragment anchors split multi-chapter documents.
        self.anchors: dict[str, int] = {}
        self._buf: list[str] = []
        self._skip = 0
        self._heading = 0

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        text = _FOOTNOTE_REF_RE.sub("", text).strip()
        if text:
            self.paragraphs.append((text, self._heading > 0))

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADING_TAGS:
                self._heading += 1
        elif tag == "br":
            self._buf.append(" ")
        elif tag == "img":
            # Illustrated drop caps (Gutenberg et al.) carry the letter in
            # alt — without it "WHEN Jane" imports as "HEN Jane". Longer
            # alt text is a caption/description: never narrate those.
            alt = (dict(attrs).get("alt") or "").strip()
            if 1 <= len(alt) <= 2:
                self._buf.append(alt)
        # After any block flush, so an id on a heading maps to the heading's
        # own paragraph, not the one before it.
        for key, value in attrs:
            if value and (key == "id" or (key == "name" and tag == "a")):
                self.anchors.setdefault(value, len(self.paragraphs))

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADING_TAGS:
                self._heading = max(0, self._heading - 1)

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _extract_paragraphs(doc: bytes) -> _TextExtractor:
    parser = _TextExtractor()
    parser.feed(doc.decode("utf-8", errors="replace"))
    parser.close()
    return parser


# --- EPUB container plumbing ----------------------------------------------------

def _check_drm(zf: zipfile.ZipFile, names: set[str]) -> None:
    """encryption.xml also appears for mere font obfuscation — only treat
    encrypted non-font resources as DRM."""
    if _ENCRYPTION not in names:
        return
    try:
        root = ET.fromstring(zf.read(_ENCRYPTION))
    except ET.ParseError:
        return
    for ref in root.iter(f"{{{_NS['enc']}}}CipherReference"):
        uri = (ref.get("URI") or "").lower()
        if not uri.endswith((".ttf", ".otf", ".woff", ".woff2")):
            raise EpubError(
                "This EPUB is DRM-protected — Bookbarge can only import "
                "DRM-free files.")


def _resolve(base_dir: str, href: str) -> str:
    """Resolve a (possibly %-encoded, possibly fragment-bearing) href
    relative to the directory of the file that referenced it."""
    path, _ = urldefrag(unquote(href))
    return posixpath.normpath(posixpath.join(base_dir, path))


def _resolve_frag(base_dir: str, href: str) -> tuple[str, str | None]:
    raw, frag = urldefrag(unquote(href))
    return posixpath.normpath(posixpath.join(base_dir, raw)), (frag or None)


def _toc_entries(zf, names, manifest, ncx_path) -> list[tuple]:
    """The TOC as ordered (zip_path, fragment, label) entries. EPUB3 nav
    doc preferred; EPUB2 NCX used when there is no usable nav. Fragments
    matter: multi-chapter documents are split at these anchors."""
    entries: list[tuple] = []
    nav = next((m for m in manifest.values()
                if "nav" in (m["properties"] or "").split()), None)
    if nav and nav["path"] in names:
        try:
            root = ET.fromstring(zf.read(nav["path"]))
            navs = list(root.iter(f"{{{_NS['xhtml']}}}nav"))
            tocs = [n for n in navs if (n.get(_EPUB_TYPE) or "") == "toc"]
            nav_dir = posixpath.dirname(nav["path"])
            # No toc-typed nav → take only the first nav (later ones are
            # landmarks/page-lists whose links aren't chapters).
            for n in (tocs if tocs else navs[:1]):
                for a in n.iter(f"{{{_NS['xhtml']}}}a"):
                    label = " ".join("".join(a.itertext()).split())
                    href = a.get("href")
                    if label and href:
                        path, frag = _resolve_frag(nav_dir, href)
                        entries.append((path, frag, label))
        except ET.ParseError:
            pass
    if not entries and ncx_path and ncx_path in names:
        try:
            root = ET.fromstring(zf.read(ncx_path))
            ncx_dir = posixpath.dirname(ncx_path)
            for point in root.iter(f"{{{_NS['ncx']}}}navPoint"):
                text = point.find(f"{{{_NS['ncx']}}}navLabel/"
                                  f"{{{_NS['ncx']}}}text")
                content = point.find(f"{{{_NS['ncx']}}}content")
                if text is not None and content is not None:
                    label = " ".join((text.text or "").split())
                    if label:
                        path, frag = _resolve_frag(
                            ncx_dir, content.get("src") or "")
                        entries.append((path, frag, label))
        except ET.ParseError:
            pass
    return entries


def parse_epub(data: bytes) -> list[dict]:
    """Parse EPUB bytes into ordered chapter candidates.

    Each candidate: {"index", "title", "paragraphs" [(text, is_heading)],
    "words", "preview", "skip" (front/back-matter suggestion)}.
    Raises EpubError with a user-safe message on anything unusable.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise EpubError("That file isn't an EPUB (not a readable ZIP "
                        "container).")
    names = set(zf.namelist())
    if _CONTAINER not in names:
        raise EpubError("That file isn't an EPUB — no META-INF/container.xml "
                        "inside.")
    _check_drm(zf, names)

    try:
        container = ET.fromstring(zf.read(_CONTAINER))
        rootfile = container.find(".//cnt:rootfile", _NS)
        opf_path = posixpath.normpath(rootfile.get("full-path"))
        opf = ET.fromstring(zf.read(opf_path))
    except (ET.ParseError, AttributeError, KeyError):
        raise EpubError("Couldn't read the EPUB's package manifest — the "
                        "file appears damaged.")
    opf_dir = posixpath.dirname(opf_path)

    manifest: dict[str, dict] = {}
    for item in opf.iter(f"{{{_NS['opf']}}}item"):
        manifest[item.get("id")] = {
            "path": _resolve(opf_dir, item.get("href") or ""),
            "media_type": item.get("media-type") or "",
            "properties": item.get("properties"),
        }
    spine = opf.find(f"{{{_NS['opf']}}}spine")
    if spine is None:
        raise EpubError("The EPUB has no spine (reading order) — nothing "
                        "to import.")
    ncx_item = manifest.get(spine.get("toc") or "")
    toc = _toc_entries(zf, names, manifest,
                       ncx_item["path"] if ncx_item else None)
    toc_by_path: dict[str, list[tuple]] = {}
    for path, frag, label in toc:
        toc_by_path.setdefault(path, []).append((frag, label))
    guide_types: dict[str, str] = {}
    guide = opf.find(f"{{{_NS['opf']}}}guide")
    if guide is not None:
        for ref in guide.iter(f"{{{_NS['opf']}}}reference"):
            guide_types[_resolve(opf_dir, ref.get("href") or "")] = \
                (ref.get("type") or "").lower()

    candidates: list[dict] = []

    def add(paragraphs: list, title: str, basename: str, gtype: str) -> None:
        m = _TITLE_CHAPTER_RE.search(title)
        if m and m.start() > 0:
            title = m.group(0)
        body = [t for t, h in paragraphs if not h]
        words = sum(len(t.split()) for t, _ in paragraphs)
        skip = bool(words < _MATTER_MIN_WORDS
                    or _MATTER_RE.search(title)
                    or _MATTER_RE.search(basename)
                    or _MATTER_RE.search(gtype))
        candidates.append({
            "index": len(candidates),
            "title": title,
            "paragraphs": paragraphs,
            "words": words,
            "preview": (body[0] if body else paragraphs[0][0])[:160],
            "skip": skip,
        })

    for itemref in spine.iter(f"{{{_NS['opf']}}}itemref"):
        if (itemref.get("linear") or "yes").lower() == "no":
            continue  # auxiliary content (footnote popups etc.)
        item = manifest.get(itemref.get("idref") or "")
        if (item is None or item["path"] not in names
                or "html" not in item["media_type"]):
            continue
        extracted = _extract_paragraphs(zf.read(item["path"]))
        paragraphs = extracted.paragraphs
        if not paragraphs:
            continue
        basename = posixpath.basename(item["path"])
        gtype = guide_types.get(item["path"], "")

        # TOC entries into this document, resolved to paragraph positions.
        # Two or more distinct positions = a multi-chapter document
        # (Gutenberg-style): split it so each TOC entry becomes a chapter.
        cuts: list[tuple[int, str]] = []
        seen: set[int] = set()
        for frag, label in toc_by_path.get(item["path"], []):
            pos = 0 if frag is None else extracted.anchors.get(frag)
            if pos is None or pos >= len(paragraphs) or pos in seen:
                continue
            seen.add(pos)
            cuts.append((pos, label))
        cuts.sort()

        if len(cuts) >= 2:
            if cuts[0][0] > 0:
                head = paragraphs[:cuts[0][0]]
                heading = next((t for t, h in head if h), None)
                add(head, heading or posixpath.splitext(basename)[0],
                    basename, gtype)
            bounds = [pos for pos, _ in cuts] + [len(paragraphs)]
            for (pos, label), end in zip(cuts, bounds[1:]):
                if paragraphs[pos:end]:
                    add(paragraphs[pos:end], label, basename, gtype)
        else:
            heading = next((t for t, h in paragraphs if h), None)
            title = ((cuts[0][1] if cuts else None) or heading
                     or posixpath.splitext(basename)[0])
            add(paragraphs, title, basename, gtype)
    return candidates


def candidate_text(candidate: dict, include_headings: bool) -> str:
    """A candidate's chapter text: blank-line-separated paragraphs (the
    paragraph-gap pacing boundary), headings optionally dropped."""
    return "\n\n".join(t for t, h in candidate["paragraphs"]
                       if include_headings or not h)


# --- routes ---------------------------------------------------------------------

def _stash_path(user_id: int, project_id: int):
    return storage.project_dir(user_id, project_id) / IMPORT_FILENAME


@router.post("/projects/{project_id}/import-epub")
async def import_epub(request: Request, project_id: int,
                      user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
    finally:
        conn.close()
    form = await request.form()
    f = form.get("epub")
    if not isinstance(f, UploadFile) or not (f.filename or "").strip():
        return _project_error(request, user, project_id, "No file selected.")
    if not f.filename.strip().lower().endswith(".epub"):
        return _project_error(
            request, user, project_id,
            "Only .epub files are accepted — convert Kindle/PDF books to "
            "EPUB first (Calibre does this).")
    data = await f.read()
    if len(data) > MAX_EPUB_BYTES:
        return _project_error(
            request, user, project_id,
            f"Over the {MAX_EPUB_BYTES // (1024 * 1024)} MB limit.")
    try:
        candidates = parse_epub(data)
    except EpubError as exc:
        return _project_error(request, user, project_id, str(exc))
    if not candidates:
        return _project_error(request, user, project_id,
                              "No readable text found in that EPUB.")
    stash = _stash_path(user["id"], project_id)
    stash.parent.mkdir(parents=True, exist_ok=True)
    stash.write_bytes(data)
    return templates.TemplateResponse(request, "epub_import.html", {
        "user": user, "project_id": project_id, "filename": f.filename.strip(),
        "candidates": candidates})


@router.post("/projects/{project_id}/import-epub/confirm")
async def confirm_import(request: Request, project_id: int,
                         user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
    finally:
        conn.close()
    stash = _stash_path(user["id"], project_id)
    if not stash.is_file():
        return _project_error(request, user, project_id,
                              "The import session expired — upload the EPUB "
                              "again.")
    candidates = parse_epub(stash.read_bytes())

    form = await request.form()
    include_headings = form.get("headings") == "on"
    try:
        selected = sorted({int(v) for v in form.getlist("include")})
    except ValueError:
        raise HTTPException(status_code=422)
    if any(i < 0 or i >= len(candidates) for i in selected):
        raise HTTPException(status_code=422)
    if not selected:
        return _project_error(request, user, project_id,
                              "No chapters were selected to import.")

    chapters: list[tuple[str, str]] = []   # (title, text)
    for i in selected:
        title = (form.get(f"title-{i}") or "").strip() \
            or candidates[i]["title"]
        text = candidate_text(candidates[i], include_headings)
        if not text.strip():
            continue  # heading-only doc with headings stripped
        if len(text.encode()) > MAX_CHAPTER_BYTES:
            return _project_error(
                request, user, project_id,
                f"'{title}' is over the {MAX_CHAPTER_BYTES // 1024} KB "
                "chapter limit — split it in the EPUB or import it as "
                "edited .txt instead.")
        chapters.append((title, text))
    if not chapters:
        return _project_error(request, user, project_id,
                              "The selected chapters had no narration text.")

    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
        next_number = conn.execute(
            "SELECT COALESCE(MAX(chapter_number), 0) + 1 FROM chapters "
            "WHERE project_id = ?", (project_id,)).fetchone()[0]
        for offset, (title, text) in enumerate(chapters):
            safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_"
                           for ch in title).strip() or "chapter"
            cur = conn.execute(
                """INSERT INTO chapters (project_id, chapter_number, filename,
                                         title, raw_text)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, next_number + offset, f"{safe}.txt", title, text))
            chapter_id = cur.lastrowid
            storage.create_chapter_tree(user["id"], project_id, chapter_id)
            source = storage.chapter_dir(
                user["id"], project_id, chapter_id) / "source.txt"
            source.write_text(text, encoding="utf-8")
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,))
        conn.commit()
    finally:
        conn.close()
    stash.unlink(missing_ok=True)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/import-epub/cancel")
async def cancel_import(request: Request, project_id: int,
                        user=Depends(auth.require_user)):
    conn = db.connect()
    try:
        get_owned_project(conn, user, project_id)
    finally:
        conn.close()
    _stash_path(user["id"], project_id).unlink(missing_ok=True)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)
