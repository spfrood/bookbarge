# AGENTS.md

Instructions for AI agents working on **bookbarge** (audiobook generation pipeline).

---

## Git workflow & safety (applies to every agent and every human)

These rules are identical across all of this owner's active repos. The full
reference lives outside this repo, in `~/GIT-WORKFLOW.md` on the dev box.

### The invariant

**GitHub `main` is the single source of truth.** Every writer reads from and
writes to GitHub. Never assume a local checkout is current — always pull first;
another agent may have moved `main` since you last looked.

### Branch and PR flow

- **Never commit directly to `main`.** Every change goes on a branch and lands
  through a pull request, no matter how small or who is making it.
- Branch naming: `feat/…`, `fix/…`, `docs/…`, `chore/…` plus a short slug
  (`feat/red-ink-reservoir`, not `feature-red-ink-17588214011985568848`).
- **One logical change per commit.** Do not bundle unrelated work — a commit
  touching scoring, timing, and enemy behaviour at once cannot be reviewed or
  reverted. Split it.
- Commit subject: imperative mood, ≤72 characters, no trailing period
  ("Add red ink reservoir", not "Added red ink reservoir.").
- PRs squash-merge, so the PR title becomes the commit on `main` — write it as
  the commit message you want to keep.

### Never commit secrets or PII

This is a hard rule with no exceptions.

- **No credentials**: API keys, tokens, passwords, private keys, connection
  strings. They belong in `.env` (gitignored) or in deploy-only config.
- **No personal information**: real email addresses, phone numbers, street
  addresses, or any end-user data. Role addresses on the project's own domain
  (`support@…`, `noreply@…`) are fine; personal mailboxes are not.
- A `gitleaks` check runs on every PR and will fail the build. On the dev box a
  pre-commit hook blocks it earlier. If you hit a false positive, add a trailing
  `gitleaks:allow` comment on the line or a fingerprint to `.gitleaksignore` —
  never disable the check itself.

### Never touch runtime state or deploy config

- Anything gitignored is runtime state that lives only on the server —
  databases, `data/`, `.env`, uploads, salts. Do not add, move, or "clean up"
  these paths.
- Do not edit PM2 configs, nginx configs, systemd units, or port numbers. The
  deploy environment is managed outside this repo.
- Do not change the port an app listens on, or add a build step to a project
  that deliberately has none.

### Before you finish

- Ensure the app still starts and the primary flow works (see the testing
  section above for this project's specifics).
- Leave the working tree clean — no stray scratch files, no commented-out
  debris, no `console.log` left from debugging.
