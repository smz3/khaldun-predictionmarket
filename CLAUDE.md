# khaldun-predictionmarket

Prediction market app. This repo also carries the standard Claude-collaboration
infra (DB, handover, hooks) meant to be copied into other projects.

## Session continuity

- `.claude/state.db` (SQLite, gitignored, local-only) holds session handover
  notes. View it with the VS Code SQLite Viewer extension.
- SessionStart hook auto-injects the last handover into context.
- Stop hook blocks once if no handover was logged this session — follow the
  command it prints (`.claude/scripts/db.py log --session ...`).
- **`/handover`** — say this (or just ask to "wrap up") to end a session
  cleanly: it saves uncommitted work via `git_safe.py`, writes a summary/
  next-steps/open-questions handover to state.db, and confirms it's safe to
  close. "Handover logged" only means the note was saved — it does not close
  the session by itself; closing the tab/window is still on you.
- SessionEnd hook clears a session's row on explicit exit/`/clear`/logout.
  It's not confirmed to fire when a session is abandoned by jumping straight
  to a new one without closing the old one — that case is instead covered by
  a grace period (a session stuck on a single heartbeat for more than 3min
  stops counting as "active").
- **`todos` table** — the source of truth for cross-session work items
  (columns in order: id, status, priority, category, task_title,
  task_details, created_ts, updated_ts). Unlike handover's free-text "next
  steps", a todo keeps showing up at every SessionStart until it's
  explicitly closed/rejected — nothing gets silently dropped just because a
  later handover's prose didn't repeat it.
  - `status`: open / discussing / rejected / closed.
  - `priority`: 1 (blocking, do next) / 2 (important, soon) / 3 (worth
    doing) / 4 (someday) — required (NOT NULL, DB-constrained), no
    untriaged state; our own plain-language levels, deliberately not
    Eisenhower's quadrants (its "delegate" category doesn't apply to a
    two-party repo).
  - `category`: `infra` (the Claude-collaboration tooling) or `app` (the
    prediction-market product) — required, same as priority.
  - Sort order everywhere is priority-first, then id.
  - Soft cap: once open+discussing hits 6, `todo-add`/`todo-status`/
    SessionStart print a reminder to triage — not a hard block.
  - Manage via `db.py todo-add`, `todo-status` (note gets appended, not
    overwritten — use it to record why something was rejected/closed),
    `todo-list`. `/handover` syncs this every time it runs.
- DB helper: `.claude/scripts/db.py` (stdlib-only, run with `python`). Has a
  `prune` subcommand (manual only) to delete old session_start/facts/
  context_watch/dead-session rows once state.db grows large — handover and
  todos rows are never pruned.
- UserPromptSubmit hook warns once at 100k tokens (soft) and once at 145k
  (hard) — a real reading of the transcript's own usage numbers, not a guess.
  On the hard warning: finish only what's in flight, log a handover, start a
  fresh session.

## Git workflow

- Rule: never two sessions writing the same folder at once. If another
  session might be active here, work in a worktree instead of `main`.
- SessionStart already tells you the answer — it reports the current branch,
  any other worktrees, and any other session with a heartbeat in the last
  30 minutes. If it flags another session, run `EnterWorktree` before
  touching files.
- Nothing flagged and working solo -> just use `main` directly.
- Worktree task done -> merge into `main`, then remove the worktree.
- Commit/push through `.claude/scripts/git_safe.py`, not raw `git commit`/
  `git push` — it refuses to stage secret-looking files (`.env`, `*.pem`,
  `*.key`, `*credentials*.json`, ...), never force-pushes, and checks the
  branch isn't behind its upstream. Policy: auto-push after every commit, any
  branch, no per-push confirmation needed (solo repo, no collaborators/CI
  yet — revisit if that changes). Push takes `--session <id>` so it can warn/
  block on `main` if another session looks active; `--override-session-guard`
  is for a human-confirmed manual override when that session is actually
  dead.

## Stack

Not yet decided — no app code exists yet.
