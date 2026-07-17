# Bookbarge

Self-hosted audiobook production pipeline. Upload a book as plain-text chapters plus a voice reference clip, generate narration via AI voice cloning on cloud GPU compute, review and recast chapters until they're right, then assemble a finished audiobook.

**Status:** Pre-release / beta. Single admin-approved user base while core functionality is validated.

---

## What it does

1. Upload a book's chapters as `.txt` files — or import a DRM-free EPUB directly (chapters and titles are auto-detected; you pick which sections to keep) — plus a short voice reference clip
2. Generate — text is chunked and sent to a RunPod Serverless GPU endpoint running Chatterbox Turbo TTS for voice cloning
3. Review each chapter by streaming it in-browser or downloading it to listen in any media player
4. Mark each chapter Approved, or edit the text / upload a replacement and let it recast
5. Once every chapter is approved, assemble the final audiobook as an M4B with embedded chapter markers
6. Download the finished audiobook and clean up intermediate files when done

Full design rationale, data model, and open decisions live in [`PROJECT_BIBLE.md`](./PROJECT_BIBLE.md) — read that first for context on *why* something works the way it does.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.11+, FastAPI |
| Frontend | Jinja2 templates + vanilla JS |
| Database | SQLite |
| Auth | `passlib`/`bcrypt` + `pyotp` (TOTP 2FA), server-side sessions |
| Audio assembly | FFmpeg |
| GPU inference | RunPod Serverless |
| TTS model | [Chatterbox Turbo](https://huggingface.co/ResembleAI/chatterbox-turbo) (ResembleAI, MIT license, English-only — the only Chatterbox variant that renders paralinguistic tags) |
| Reverse proxy / TLS | Nginx |

---

## Accounts & access

- Self-registration is open, but every new account requires admin approval before login is permitted — this is a beta-period abuse gate, not a permanent feature.
- Login requires password + TOTP (authenticator app) once 2FA is enrolled.
- Admin approval is currently done via direct database access. No admin UI yet.
- Password resets are also handled via direct DB access for now. Acceptable at current beta scale (a handful of test users); revisit before opening self-serve signup more broadly.

---

## Project structure

```
/srv/bookbarge/
  data/app.db                        ← SQLite database
  users/{user_id}/
    projects/{project_id}/
      voice/                         ← uploaded reference clip(s)
      chapters/{chapter_id}/
        source.txt                   ← current chapter text
        chunks/                      ← per-chunk audio from RunPod
        assembled.mp3                ← per-chapter assembled audio
      output/                        ← final M4B + approved chapter MP3s
```

---

## Running locally / on the VPS

The app is designed to run only while actively in use, not continuously, to minimize cost:

```bash
# Start
sudo systemctl start bookbarge

# Stop
sudo systemctl stop bookbarge
```

Nginx stays running at all times (it also serves other sites on this VPS) and reverse-proxies the app's subdomain to the app when the service is up, serving a static offline page when it's down. Deployment specifics (hostname, port, neighboring sites) live in `PRIVATE.md`, which is gitignored and never published.

### Environment variables

Copy `.env.example` to `.env` (gitignored, never committed) and fill in at minimum:

```
RUNPOD_API_KEY=
RUNPOD_ENDPOINT_ID=
SESSION_SECRET=
```

---

## Commercial use notes

Chatterbox Turbo is MIT-licensed (model weights included) and commercial use of its output is permitted. Two things it does **not** cover:

- **Voice rights** — only clone voices you own or have explicit commercial rights to use. Using your own recorded voice as the reference clip is the safest path.
- **Book rights** — you need to own or hold audiobook production rights to whatever text you upload.

Distribution note: ACX/Audible currently disfavor externally-produced AI narration. Findaway Voices and direct sales are the viable distribution channels for output from this pipeline.

---

## EPUB import

Instead of splitting a book into `.txt` files yourself, upload a DRM-free `.epub` on the project page. Bookbarge reads the book's structure (spine + table of contents, including books that pack many chapters into one internal file), shows you every detected section with its title and word count, and pre-unticks what looks like front/back matter — you confirm what to import and can rename chapters before anything is created. Headings can be narrated or stripped. Kindle formats and PDF aren't supported directly; convert them to EPUB first (Calibre does this in one click).

---

## Text preparation

Bookbarge does not automatically enrich or tag your chapter text. Chatterbox Turbo supports inline paralinguistic tags that render as real vocalized reactions — the documented set is `[clear throat]`, `[sigh]`, `[shush]`, `[cough]`, `[groan]`, `[sniff]`, `[gasp]`, `[chuckle]`, `[laugh]` — if you want that, add tags to your `.txt` files yourself before upload, using whatever tool you like. This is intentional: an automated LLM enrichment pass would mean a recurring external API cost per book on top of RunPod compute, which this project avoids by design. The chunker preserves any tags you include exactly as written.

**Pauses** are the one tag Bookbarge handles itself: write `[pause:2.4s]` anywhere in your text (0.1–15 seconds) to insert exactly that much silence — useful at scene breaks and section transitions, where TTS tends to rush ahead. Pauses are synthesized locally, cost nothing to generate or adjust, and can also be added, retimed, or removed later from the per-chapter chunk editor.

**Paragraph pacing** is automatic: paragraph breaks (blank lines — single newlines are treated as hard-wrapping, not breaks) get a small silence when the chapter audio is stitched, so narration breathes at paragraph turns without hand-tagging every one. The gap defaults to 0.7 s and is set by `BOOKBARGE_PARAGRAPH_GAP_SECONDS` in `.env` (`0` disables it; changing it only needs a re-stitch, not regeneration). An explicit `[pause:Ns]` at a paragraph turn replaces the automatic gap there.

---

## Out of scope (for now)

- Payment/billing (monetization model not yet decided)
- Per-user storage quotas
- Email verification on signup
- Admin dashboard / user management UI

See `PROJECT_BIBLE.md` for the full list and reasoning.

---

## License

[MIT](LICENSE). Chatterbox Turbo itself is also MIT-licensed by ResembleAI (including weights).
