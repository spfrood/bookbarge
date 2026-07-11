# Bookbarge — Claude Code Build Prompts

Use these with Fable 5 (`/model fable`, or `claude --model claude-fable-5`, or set `"model": "claude-fable-5"` in `.claude/settings.json` for this project so it's the default without retyping it every session).

## How to use this document

Place `PROJECT_BIBLE.md` at the root of the repo before starting. Every prompt below tells Fable to read it — Fable can hold the whole document in context, so there's no need to re-paste sections manually.

Run the prompts in order, one per phase. The order front-loads risk: the GPU handler comes first because it's the project's biggest unknown (voice quality, cost, latency) and is fully standalone, and HTTPS exposure comes right after scaffolding so every browser-facing phase is tested over real TLS rather than through tunnels. Let Fable finish and self-verify before moving to the next one — it tests its own work with less prompting than other models, so these are written as outcomes and constraints rather than step-by-step instructions. Don't switch models mid-phase; each switch re-reads full history and costs a one-time token hit, so stay on Fable for the whole phase and only branch to Opus/Sonnet if a fallback happens automatically (safety classifier) or you deliberately want a cheaper model for a small follow-up fix.

Start a fresh session (or `/clear`) between phases once one is confirmed working — keeps context focused on the current phase rather than dragging the full history of everything already built.

---

## Phase 0 — Orientation and Plan Review

```
Read PROJECT_BIBLE.md in full before doing anything else. This is the design
document for Bookbarge, an audiobook production pipeline we're about to build
together on this VPS.

Do not write any code yet. First:

1. Summarize your understanding of the system back to me in your own words —
   what it does, the core workflow, and the parts you think are riskiest to
   get right.
2. Ask me anything genuinely unclear or underspecified in the document.
3. Propose the repository structure and initial file/folder layout you'd use,
   consistent with /srv/bookbarge/ from the bible.
4. Check this VPS for any existing Claude Code directives or conventions
   already in place (port allocation rules, deployment patterns, standards
   for other projects on this box) and tell me what you find, since the bible
   defers to those for port selection.

Wait for my go-ahead before building anything.
```

---

## Phase 1 — RunPod Chatterbox Turbo Handler

```
Read PROJECT_BIBLE.md Section 8.

Build and deploy the RunPod Serverless handler for Chatterbox Turbo TTS —
it must be the Turbo variant specifically, since it's the only Chatterbox
model that renders paralinguistic tags rather than reading them aloud.
Model loaded once at cold start and cached against a Network Volume so it
isn't re-downloaded every invocation, accepting
{text, voice_reference_base64} and returning {audio_base64, sample_rate}.

This phase deliberately runs first, before any of the web app exists,
because it's the project's biggest unknown: get it working and verified as
a fully standalone endpoint — test it directly with a script or curl
against the deployed endpoint, using a real voice reference clip I'll
provide — so we learn whether the cloned voice quality, cost, and latency
hold up before building anything on top of them. I have a RunPod account
with credits loaded but nothing deployed and no API key generated yet, so
this is a from-scratch build. Tell me if anything requires manual action in
the RunPod dashboard that you can't do yourself (creating the API key will
be one — I'll do that when you're ready for it).

While it's up, measure the input-length ceiling per Section 8: find the
longest chunk text that generates reliably without hitting the ~40-second
output truncation or audible voice drift, and record that number — Phase 6
sets its chunk cap from it.
```

---

## Phase 2 — Project Scaffolding

```
Read PROJECT_BIBLE.md for full context if you haven't already this session.

Build the foundational skeleton of Bookbarge: the FastAPI application
structure, the SQLite schema and migration approach from Section 5, and
.env-based config handling for secrets (RunPod API key, session secret,
etc.) per Section 10.

I want this to boot cleanly, connect to a fresh SQLite database, apply the
schema, and serve a placeholder homepage — a solid foundation the rest of
the build sits on, not a full feature yet. Use your judgment on project
layout conventions within the /srv/bookbarge/ structure the bible specifies.

Verify it actually runs before telling me it's done.
```

---

## Phase 3 — Nginx / HTTPS Exposure

```
Read PROJECT_BIBLE.md Section 4.

Put the app behind real HTTPS now, before any browser-facing features are
built. Read PRIVATE.md at the repo root (gitignored, local only) for the
production hostname and the other sites this VPS hosts. Create the Nginx
server block for that hostname, reverse-proxying to the app's port (per
the server conventions you found in Phase 0 — check them before picking a
port), and have it serve a static "currently offline" page instead of a
raw 502 whenever the app isn't running. The wildcard origin cert already
covers the subdomain and DNS already resolves, so no cert or DNS work
should be needed.

The other sites this VPS hosts must be completely unaffected by anything
you do here. Anything requiring sudo (installing the site config,
reloading nginx) I'll run in my own terminal — prepare the files and give
me the exact commands.

Prove it: with the Phase 2 placeholder app running, the production URL
serves it over HTTPS; with the app stopped, the offline page appears; and
the other sites work throughout. Every later phase gets tested through
this URL, so cookies and audio streaming behave exactly as they will in
production.
```

---

## Phase 4 — Auth System & Beta Landing Page

```
Read PROJECT_BIBLE.md Sections 5, 10, and 11 for the account/auth model and
the beta-notice requirements before starting.

Build the full authentication system:

- Open self-signup (email + password, bcrypt-hashed)
- New accounts default to unapproved and cannot log in — not even to start
  the TOTP step — until an admin flips is_approved directly in the database
- Registration ends on a clear "pending approval" screen, and a login
  attempt on an unapproved account gets an honest, non-generic message
  saying so
- TOTP two-factor via an authenticator app, with QR code setup, required
  before a session is fully established once enrolled
- Server-side sessions with Secure/HttpOnly cookies — TLS has been live at
  the production subdomain since Phase 3, so test the real cookie behavior
  through that URL
- Every authenticated route enforces that the requesting user owns whatever
  they're trying to access — no relying on obscure IDs

In the same pass, build the pre-login landing page per Section 11: a clear
beta badge, a disclaimer that seats are limited during the beta and new
accounts need admin approval before they're usable, and enough
plain-language framing that a first-time visitor understands this converts
a book into an audiobook via AI voice cloning before they register. Keep
that copy in one template partial so it's trivial to update or remove once
the beta gate comes off later. Use your own judgment on tone and layout —
I haven't specified exact wording.

I want to end this phase able to register an account, approve it myself via
direct DB access, log in with password + TOTP, and see that an unapproved
account is correctly refused. Confirm all of that works before moving on.
```

---

## Phase 5 — Project & Chapter Management

```
Read PROJECT_BIBLE.md Sections 5 and 6.

Build project and chapter management: users can create a project (title,
author), upload chapters as individual .txt files, and upload a voice
reference clip (WAV). Enforce the filesystem layout from Section 6 exactly,
scoped per user per project. Validate uploads (file type, reasonable size
ceiling) even though there's no storage quota system yet.

Every project, chapter, and voice reference must be invisible and
inaccessible to any user other than its owner — treat this as a hard
requirement, not a nice-to-have, and prove it with a quick cross-user
access test before calling this phase done.
```

---

## Phase 6 — Chunking Logic

```
Read PROJECT_BIBLE.md Section 5's recast/versioning note, Section 7 on text
preparation, and the build order's chunking step.

Build the text chunking pipeline: split a chapter's raw text into
sentence-aware chunks of roughly 300–500 characters using nltk's sentence
tokenizer, with a hard per-chunk cap set from the input-length ceiling
measured in Phase 1 (each chunk's audio must stay safely under Chatterbox's
~40-second generation limit), and persist them as chunk records tied to the
chapter's current version number. This needs to handle a full chapter of
pulp-novel prose correctly — don't let it cut mid-sentence or mishandle
common abbreviations.

Bracketed paralinguistic tags users may have added to their source text
(e.g. [sigh], [laugh], [gasp]) must pass through completely unmodified —
never split, strip, or escape a tag, even if it falls near a chunk boundary.
If a tag would land at a chunk boundary, keep it attached to the
sentence/clause it belongs to rather than cutting between them.

Show me chunked output on a real sample chapter — ideally one with a few
tags inserted — so I can sanity-check both the splits and tag preservation
before we connect this to actual TTS generation.
```

---

## Phase 7 — Generation Pipeline

```
Read PROJECT_BIBLE.md Sections 2 and 8.

Wire the chunking pipeline to the RunPod handler from Phase 1: dispatch a
chapter's pending chunks to the Serverless endpoint, poll for completion in
the background, store returned audio per chunk, and keep chunk and chapter
statuses accurate throughout, using the exact enums from Section 5: chunks
move pending → processing → done, the chapter moves pending → generating →
ready_for_review, each with a sane error state if a chunk fails.

I want to be able to click "Generate" on a chapter and watch its chunks
move through these states to completion, using a real chapter and my
actual voice reference clip.
```

---

## Phase 8 — Per-Chapter Assembly

```
Read PROJECT_BIBLE.md Section 9.

Build automatic per-chapter audio assembly: the moment every chunk in a
chapter reaches "done," concatenate them with FFmpeg into assembled.mp3 for
that chapter, update assembled_audio_path and assembled_at, and move the
chapter to ready_for_review.

Confirm this triggers correctly and produces a clean, correctly-ordered MP3
on a real generated chapter.
```

---

## Phase 9 — Streaming and Download

```
Read PROJECT_BIBLE.md Section 9's streaming/download bullet.

Add chapter audio playback: an in-browser streaming player backed by HTTP
Range support so scrubbing works without downloading the whole file first,
plus a direct download link for the same file for review in an external
media player.

Test that scrubbing actually works and that the download produces a file
that plays correctly in a standard media player, not just in-browser.
```

---

## Phase 10 — Review and Recast UI

```
Read PROJECT_BIBLE.md Sections 2, 5, and 9 closely — this is the most
detail-sensitive phase.

Build the chapter review interface:

- An Approved toggle per chapter, freely flippable at any time up until the
  project is deleted, independent of whether the text has changed
- An inline text editor and a "replace with new .txt file" option, either
  of which triggers: version increment, approved reset to false, existing
  chunks deleted, full chapter re-chunked and regenerated through the
  Phase 6–8 pipeline, landing back at ready_for_review when done
- A per-chapter checklist view showing approval status across the whole
  project at a glance

Walk through the full loop yourself as a check: generate a chapter, approve
it, edit its text, confirm it correctly resets and regenerates, re-approve
it. Tell me if anything in the bible was ambiguous about how this should
behave and how you resolved it.
```

---

## Phase 11 — Final Assembly Pipeline

```
Read PROJECT_BIBLE.md Section 2, steps 10–11.

Build final audiobook assembly: the "Assemble Final Audiobook" action stays
disabled until every chapter in the project currently has approved = true,
evaluated fresh at click time rather than cached. When available and
triggered, concatenate the approved chapters' audio into a single M4B with
embedded chapter markers using FFmpeg, saved to the project's output/
directory.

Verify the gating actually blocks assembly with an unapproved chapter
present, and that the resulting M4B has correct chapter markers when you
play it back.
```

---

## Phase 12 — Download and Cleanup

```
Read PROJECT_BIBLE.md Section 6's cleanup note.

Build the finished-audiobook download and the cleanup flow. Cleanup is
user-driven: let the user choose, per chapter, whether to keep or delete
that chapter's assembled.mp3 and any leftover chunk audio — nothing gets
deleted without an explicit choice. The final M4B in output/ and anything
the user chose to keep should persist until they manually delete the
project.

Confirm a full cycle works: generate, approve everything, assemble,
download the M4B, run cleanup with a mix of keep/delete choices, and verify
the filesystem matches what was chosen.
```

---

## Phase 13 — Bring-Online/Offline Tooling

```
Read PROJECT_BIBLE.md Section 4.

Finish deployment: a systemd unit for the FastAPI app so it can be started
and stopped as a unit (sudo systemctl start/stop bookbarge — I'll run the
privileged commands in my own terminal, so prepare the unit file and give
me the exact commands), plus simple start/stop scripts if useful. The Nginx
server block and offline page have been in place since Phase 3 — don't
touch Nginx's other sites.

Prove the full toggle: start the service, confirm the production subdomain
serves the app and the other sites on this VPS (listed in PRIVATE.md)
still work; stop the service, confirm the subdomain shows the offline page
and the other sites are still completely unaffected.
```

---

## After Phase 13

The bible flags two open items that aren't blocking but worth deciding once you're using the real thing: whether recast edits should keep version history (currently: no), and how future disk quotas should constrain cleanup vs. new uploads. Revisit those once Bookbarge has been used on a real book or two and you have a feel for whether the current defaults are actually annoying you.
