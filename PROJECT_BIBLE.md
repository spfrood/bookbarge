# Bookbarge — Project Bible

Audiobook Production Pipeline

## 1. Purpose

A self-hosted web application for converting book manuscripts into commercial-quality audiobooks using AI voice cloning. A user uploads a book split into chapters plus a voice reference clip, triggers cloud GPU text-to-speech generation, reviews finished chapters (by streaming or downloading them), approves or sends them back for recast, then assembles the final audiobook — all through a browser interface.

Multi-user from day one: any visitor can self-register an account, secured with password + TOTP two-factor authentication. New accounts require admin approval before login is permitted — this gates access during the beta period to prevent abuse if traffic spikes unexpectedly. Each user's projects, voice references, and files are fully isolated from other users.

This is a proof-of-concept. Monetization (flat fee per project, bring-your-own-RunPod-token, or another model) is not decided yet — the architecture should not block adding billing later, but billing itself is out of scope for this build.

---

## 2. Core Workflow

1. User registers an account and lands on a pending-approval screen; admin approves in the DB or a minimal approval view; user logs in (password + TOTP) only after approval
2. User creates a new project (book title, author, etc.)
3. User uploads chapters as plain `.txt` files (one file per chapter)
4. User uploads a voice reference clip (WAV, 5–30 seconds)
5. User clicks **Generate** — backend splits each chapter into sentence-aware chunks (~300–500 characters, sized so each chunk's audio stays under Chatterbox's ~40-second generation ceiling — see Section 8) and dispatches each chunk to a RunPod Serverless Chatterbox Turbo endpoint
6. Backend polls RunPod for completion, stores returned audio per chunk, and updates status in real time
7. As soon as all chunks belonging to a chapter are complete, the backend automatically assembles them (via FFmpeg) into a single per-chapter audio file in a widely-compatible format (MP3), independent of the other chapters
8. The chapter becomes available for review: the user can **stream it in-browser** or **download the MP3** to review at their own pace in any media player — full-length listening isn't realistic to force entirely within a browser session, so both paths are first-class
9. After listening, the user sets the chapter's **Approved** toggle on or off — it can be flipped again at any time later, even after being approved, right up until the project is deleted:
   - **Approved (on)** — the chapter counts toward the project being ready for final assembly
   - **Not approved** — if the user edits the chapter text inline or uploads a replacement `.txt` file, the backend re-chunks *that chapter only*, regenerates its chunks via RunPod, and re-assembles its chapter audio (this also clears the Approved toggle automatically); the chapter returns to pending review and the cycle (steps 7–9) repeats for that chapter alone. The user can also simply toggle approval off without editing anything, if they change their mind on re-listening.
10. Once **every** chapter in the project is marked Approved, the **Assemble Final Audiobook** action becomes available
11. User clicks **Assemble** — FFmpeg concatenates the approved per-chapter audio files into a single M4B with embedded chapter markers
12. User downloads the finished audiobook and/or the individual approved chapter MP3s
13. User clicks **Clean Up** — deletes intermediate chunk files and RunPod job records, leaving only final chapter and audiobook output files until the user manually deletes the project

---

## 3. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python 3.11+, FastAPI | Native fit for RunPod/HuggingFace SDKs, async-friendly for polling jobs, reliable with Claude Code |
| Frontend | Jinja2 templates + vanilla JS | No build tooling, simple enough not to need a SPA framework |
| Database | SQLite | Zero-ops, sufficient for expected load; migrate to Postgres later only if concurrency demands it |
| Auth | `passlib`/`bcrypt` for passwords, `pyotp` for TOTP, server-side sessions (signed cookies via `itsdangerous` or `starlette` session middleware) | Standard, well-documented, no external auth provider dependency |
| Audio assembly | FFmpeg (VPS-side, CPU only) | Two-stage: per-chapter MP3 assembly as chunks complete, plus final M4B assembly with chapter markers once all chapters are approved |
| GPU inference | RunPod Serverless | Pay-per-second, scales to zero when idle, fits "bring online only when needed" cost model |
| TTS model | Chatterbox **Turbo** (ResembleAI, MIT license incl. weights, 350M, English-only) | Best open-source quality/cost tradeoff; commercial use permitted under MIT; voice cloning from short reference clip. Must be the **Turbo** variant specifically — it is the only Chatterbox model that renders paralinguistic tags (Section 7); the standard and multilingual variants speak tags aloud as literal text |
| Reverse proxy / TLS | Nginx + existing domain (already pointed at VPS) | HTTPS termination, required for secure session cookies |
| Process management | systemd unit for the FastAPI app | Simple start/stop control matching the "bring online only when in use" requirement |

---

## 4. Infrastructure

- **VPS**: a Linux VPS (provider, specs, and hostnames live in `PRIVATE.md`, which is deliberately kept out of this public repo). Runs the FastAPI app, Nginx, SQLite, and FFmpeg. No GPU work happens here. Nginx already hosts other, unrelated sites on this VPS (listed in `PRIVATE.md`) — this app gets its own subdomain and its own Nginx server block, entirely separate from the others.
- **GPU compute**: RunPod Serverless endpoint running a custom Chatterbox Turbo handler. Needs to be built from scratch (see Section 8).
- **Domain/TLS**: the app runs on its own dedicated subdomain (hostname in `PRIVATE.md`). Nginx terminates TLS for all sites including this one and reverse-proxies this subdomain to the FastAPI app. Use whatever internal port is dictated by existing Claude Code server directives/conventions already in place on this VPS for port allocation, rather than assuming one — check those directives before assigning a port.
- **Bring-online pattern** *(revised 2026-07-12 at alpha start)*: originally the FastAPI app was to run only while in use; that was retired once the build reached alpha — idle cost is negligible (the GPU side scales to zero on its own) and the approval gate holds unattended. The app now runs as an always-up, boot-enabled systemd service (`bookbarge`). Nginx stays always-on for all sites either way, and the app subdomain's server block serves a static "currently offline" page rather than a raw 502 during any downtime (maintenance, crash-restart gaps), with the other sites unaffected.
- **Run location**: the app runs from the dev tree (`~/projects/bookbarge`) — a deliberate exception to this server's usual `~/deploy/<name>` convention for hosted apps, because this VPS is only Bookbarge's development home, not its permanent deployment target. Secrets (`.env`) still stay out of version control.

---

## 5. Data Model (SQLite)

```
users
  id, email (unique), password_hash, totp_secret, totp_confirmed (bool),
  is_approved (bool, default false), approved_at, approved_by (FK to users.id, nullable),
  created_at, is_admin (bool)

projects
  id, user_id (FK), title, author, status (draft/generating/reviewing/assembled),
  created_at, updated_at

chapters
  id, project_id (FK), chapter_number, filename, raw_text, version (int, default 1),
  processing_status (pending/generating/ready_for_review/recasting/error),
  approved (bool, default false), approved_at,
  assembled_audio_path, assembled_at

chunks
  id, chapter_id (FK), chapter_version (int, ties chunk to the chapter text version it was generated from),
  chunk_index, text, status (pending/processing/done/error),
  runpod_job_id, audio_file_path, created_at, updated_at

voice_references
  id, project_id (FK), filename, file_path, uploaded_at

generation_jobs
  id, project_id (FK), chapter_id (FK, nullable — null means full-project generation, set means a single-chapter recast),
  started_at, completed_at, total_chunks, completed_chunks, status
```

Every table with a `project_id` or reachable through one must be scoped to `user_id` at the query level — no cross-user data access, enforced in the application layer (not just relying on UI hiding).

**Recast behavior**: when a chapter is edited or its text file replaced, increment `chapters.version`, set `approved = false` (since the audio backing any prior approval is being replaced), delete the chapter's existing `chunks` rows, and regenerate fresh chunks tied to the new version. This is a full-chapter regeneration rather than a diff-based partial re-chunk — simpler to implement correctly and cheap enough given the per-chapter RunPod cost (well under $1 even for a long chapter). No history of previous chapter text is retained — each edit overwrites `raw_text` and `source.txt` outright.

**Approval behavior**: `approved` is a simple boolean the user can toggle freely (approve, unapprove, re-approve) at any time up until the project is deleted, independent of whether the text has changed. Editing the text automatically flips it to `false` because the previous approval no longer reflects the current audio, but the user can also toggle it manually just from re-listening and changing their mind — no text edit required. Final assembly only pulls in chapters where `approved = true` at the moment the user clicks Assemble.

---

## 6. Filesystem Layout

```
/srv/bookbarge/
  data/app.db                        ← SQLite database
  users/{user_id}/
    projects/{project_id}/
      voice/                         ← uploaded reference clip(s)
      chapters/{chapter_id}/
        source.txt                   ← current chapter text (overwritten on edit/re-upload, version tracked in DB)
        chunks/                      ← per-chunk audio returned from RunPod for the current version
        assembled.mp3                ← per-chapter assembled audio (streamed and/or downloaded for review)
      output/                        ← final M4B + copies of approved chapter MP3s (survives cleanup)
```

Cleanup is user-driven, not fixed: at cleanup time the user chooses, per chapter, whether to keep or delete that chapter's `assembled.mp3` and any remaining chunk audio. Intermediate chunk files no longer needed for playback are the main cleanup candidates; `output/` (the final M4B) and any chapter files the user chose to keep persist until manually deleted. There are no storage quotas yet — this is planned for later, at which point cleanup choices may need to be constrained by remaining quota.

---

## 7. Text Preparation & Naturalness (User Responsibility)

Chatterbox **Turbo** supports inline paralinguistic tags that render as actual vocalized reactions in the cloned voice — the documented set is `[clear throat]`, `[sigh]`, `[shush]`, `[cough]`, `[groan]`, `[sniff]`, `[gasp]`, `[chuckle]`, `[laugh]` — and generally produces more natural-sounding delivery when source text is well-punctuated and free of formatting artifacts.

**This is a Turbo-only feature.** The standard and multilingual Chatterbox models treat these tags as ordinary text and read them aloud, which would ruin a tagged manuscript. This is why the stack (Section 3) and the RunPod handler (Section 8) pin the Turbo variant specifically.

**Bookbarge does not perform automated text enrichment.** Inserting these tags well requires either a human read-through or an LLM pass over the manuscript, and running chapter text through an external LLM API for every project adds an ongoing per-book cost on top of RunPod compute — cost this project is intentionally avoiding. This is a deliberate scope boundary, not an oversight.

Instead:

- Users are expected to format and/or tag their chapter `.txt` files themselves before upload, using whatever external tool or manual process they prefer (including running text through Claude or another LLM outside of Bookbarge, at their own discretion and cost, before upload).
- The chunking pipeline (build-order step 6 in Section 13) must treat bracketed tags as literal text and **pass them through unmodified** — the sentence-aware chunker should not strip, escape, or split in the middle of a `[tag]`, since Chatterbox needs to receive them intact to render the effect.
- No UI is planned for tag insertion or suggestion in this build. If this becomes a frequent pain point after real use, an optional non-LLM heuristic pass (e.g. simple punctuation-based cues) could be considered later, but that's out of scope for now.

---

## 8. RunPod Serverless Handler (to be built)

Since RunPod is not yet set up, this needs to be built and deployed as part of the project:

1. Create a RunPod account and generate an API key (manual, outside Claude Code's scope — the project owner does this)
2. Build a handler (`handler.py`) that:
   - Loads Chatterbox **Turbo** (not the standard or multilingual variant — see Sections 3 and 7) once at cold start, cached against a RunPod Network Volume (`HF_HOME` pointed at the volume so the model isn't re-downloaded every cold start)
   - Accepts a JSON payload: `{ text: str, voice_reference_base64: str }`
   - Returns: `{ audio_base64: str, sample_rate: int }`
3. Package as a Docker image (RunPod provides PyTorch base images) and deploy as a Serverless endpoint
4. Store the endpoint ID and API key in the VPS app's environment config (`.env`, never committed to source control)
5. Backend dispatches chunks to the endpoint via RunPod's REST API and polls job status until each chunk resolves

GPU sizing reference: an RTX 3090 (24GB) comfortably runs Chatterbox Turbo (350M params). Expect roughly $0.55–$0.75 in RunPod compute per 70,000-word novel at current spot pricing — this may drift and should be re-verified once the endpoint is live.

**Input length ceiling**: Chatterbox output truncates at roughly 40 seconds of audio per generation, and quality (voice drift, pacing) degrades on long inputs well before any hard failure — Resemble's own demos cap input at 300 characters. While testing the deployed endpoint standalone, empirically find the longest chunk text that generates reliably without truncation or drift, and feed that measured cap into the chunking logic (build-order step 6) rather than hardcoding a guess.

---

## 9. Chapter Review, Streaming & Recast

Reviewing full audiobook chapters by ear takes real time regardless of how fast generation is — this needs to be treated as a workflow spanning multiple sessions, not a synchronous in-browser step.

- **Streaming**: chapter audio should be served with HTTP Range support so the browser's `<audio>` element can seek/scrub without downloading the whole file first. Starlette's `FileResponse` supports Range requests natively; if performance under load ever becomes a concern, this can be moved to an Nginx-served static location with authenticated, short-lived signed URLs.
- **Download**: the same `assembled.mp3` file is offered as a direct download link so the user can review it in any external media player (car stereo, phone, etc.) on their own schedule.
- **Review decision**: each chapter has an **Approved** toggle (on/off) rather than two terminal, one-way states. The user can flip it freely — approve, unapprove, re-approve — any time up until the project is deleted, whether or not they've made any text changes. There's no partial/per-chunk approval at this stage; the whole chapter is judged as a unit.
- **Recast input**: the user can either edit the chapter text directly in a textarea in the UI, or upload a replacement `.txt` file. Both paths converge on the same recast pipeline (Section 5's versioning behavior) and automatically clear the Approved toggle, since the underlying audio is being replaced.
- **Recast scope**: recasting regenerates only the affected chapter — other chapters' chunks, audio, and approval status are untouched.
- **Final assembly gate**: the "Assemble Final Audiobook" action stays disabled until every chapter in the project is currently marked Approved. The UI should show a clear per-chapter checklist so the user can see at a glance what's still outstanding. Because approval can be toggled at any time, this is evaluated fresh at the moment the user clicks Assemble, not cached from an earlier check.

---

## 10. Authentication & Security Requirements

- **Registration**: open self-signup, email + password. No email verification required for this POC phase, but the schema should accommodate adding it later (leave room for an `email_verified` column if not included initially).
- **Admin approval gate**: new accounts default to `is_approved = false`. Login is blocked entirely (both password step and TOTP step) until an admin flips this flag. Registration should end on a clear "your account is pending approval" screen rather than silently failing later at login. Approval is done via direct DB access for now — no admin UI needed, since this gate is only expected to matter for a handful of beta testers and will be removed once approval is no longer required post-beta. Attempting to log in to an unapproved account should return a clear, non-revealing message ("account pending approval") rather than a generic auth error, so legitimate users aren't confused with failed logins.
- **Password storage**: bcrypt via `passlib`, never plaintext, never reversible.
- **2FA**: TOTP via `pyotp`, using an authenticator app (Google Authenticator, Authy, etc.). Flow: after password login, if `totp_confirmed` is true, prompt for a 6-digit code before establishing a full session. Provide a QR code (via `qrcode` library) during TOTP setup.
- **Sessions**: server-side or signed-cookie sessions, `Secure` and `HttpOnly` flags set (only possible because TLS is already configured), reasonable expiry (e.g., 7 days sliding).
- **Authorization**: every route that touches project data must verify the requesting user owns the resource — no relying on obscurity of IDs.
- **File upload validation**: restrict chapter uploads to `.txt`, restrict voice reference to WAV only (5–30 seconds, per Section 2), enforce a reasonable max file size even though no storage quota is enforced yet (prevents a single bad upload from being catastrophic).
- **Secrets**: RunPod API key and any future secrets live in environment variables / `.env`, excluded from version control via `.gitignore`.

---

## 11. Beta Notice & Landing Page

The public landing page (pre-login) must display, prominently and above the fold:

- A clear **"Beta" label/badge** identifying this as an early-access product, not a finished commercial service
- A **disclaimer stating that seats are limited during the beta period**, and that new registrations require admin approval before the account becomes usable — set expectations that approval is not instant
- Basic framing of what the product does (book → audiobook via AI voice cloning), so a visitor understands the context of the beta before registering

This doesn't need to be elaborate — a static banner or callout block on the login/registration page satisfies the requirement. It should be easy to update or remove later once the beta gate is lifted, so keep the copy in a single template partial rather than duplicated across pages.

---

## 12. Explicitly Out of Scope (For Now)

- Payment processing / billing — deferred, model not yet decided (flat fee per project vs. bring-your-own RunPod token vs. other)
- Per-user storage quotas — no limits enforced initially
- Email verification on signup
- Password reset via email — confirmed as manual DB intervention for now (update `password_hash` directly). Acceptable at the current expected scale of 3–4 test users; revisit once the beta gate is lifted and self-serve signup is expected to bring in users an admin can't reasonably reset by hand.
- Admin dashboard / user management UI (schema includes `is_admin` for future use, but no UI yet)
- Distribution to ACX/Audible (policy currently disfavors external AI narration — Findaway Voices and direct sales are the viable channels; not part of this build regardless)
- Automated paralinguistic tag insertion / LLM-based text enrichment — deferred to avoid recurring external API costs; see Section 7

---

## 13. Suggested Build Order for Claude Code

1. **RunPod handler** — build and deploy the Chatterbox **Turbo** Serverless endpoint first, before any app code: it's the project's biggest unknown (cloned-voice quality, cost, latency), it's fully standalone, and it scales to zero while the rest is built. Test in isolation via `curl` or a small script; while it's up, measure the input-length ceiling per Section 8
2. **Project scaffolding** — FastAPI app structure, SQLite schema + migrations, `.env` handling
3. **Nginx / HTTPS exposure** — server block for the app's subdomain (hostname in `PRIVATE.md`; port per existing server conventions) with the static offline fallback page, so every later step is tested over real TLS (required for `Secure` cookies) at the production URL
4. **Auth system & beta landing page** — registration (ending on a pending-approval screen), admin approval gate, login, TOTP setup/verification, session middleware, route protection, plus the pre-login beta badge / limited-seats disclaimer / product framing (Section 11)
5. **Project & chapter management** — CRUD for projects, chapter upload, voice reference upload, per-user file isolation
6. **Chunking logic** — sentence-aware text splitting using `nltk`'s sentence tokenizer (handles abbreviations/edge cases better than a raw regex, lighter than spaCy), targeting ~300–500 characters per chunk with a hard cap set from the ceiling measured in step 1 (each chunk's audio must stay under Chatterbox's ~40-second generation limit); chunk records in DB, tied to chapter version; bracketed paralinguistic tags (Section 7) must pass through unmodified and never be split mid-tag
7. **Generation pipeline** — dispatch chunks to RunPod, background polling, status updates, audio storage
8. **Per-chapter assembly** — FFmpeg concatenation into `assembled.mp3` as soon as all of a chapter's chunks complete
9. **Streaming & download** — Range-enabled chapter audio serving, download links
10. **Review & recast UI** — chapter-level Approved toggle (freely flippable, per Section 9), recast via inline text edit or file re-upload, per-chapter approval checklist
11. **Final assembly pipeline** — gated on all chapters approved; FFmpeg concatenation of approved chapters into final M4B with chapter markers
12. **Download & cleanup** — serve final files, cleanup routine with confirmation
13. **Bring-online/offline tooling** — `bookbarge` systemd unit and simple start/stop scripts; prove the full start/stop toggle end-to-end against the Nginx block that has existed since step 3

---

## 14. Open Items to Resolve Before or During Build

- **Recast text history**: defaulting to simple overwrite with no version history retained on chapter edits, per Section 6. Flag if this should instead retain prior versions.
- Whether disk quotas (once added later) should constrain what a user is allowed to keep at cleanup time, or simply block new uploads once exceeded
- **Paragraph-boundary pacing in chunking** (noted 2026-07-11, needs real-listening data): the chunker splits at sentence boundaries and ignores paragraph breaks, so a chunk can pack narration and dialogue from adjacent paragraphs together. TTS pauses derive from punctuation, so this may well be inaudible — but if full-chapter audio feels rushed at paragraph turns, add a prefer-paragraph-break rule to the chunker (break at paragraph boundaries when a chunk is already past the ~250-char target, even if more would fit). Assess after a few real chapters have been generated and listened to, not before.
