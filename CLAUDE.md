# khaldun-predictionmarket

Prediction market app. This repo also carries the standard Claude-collaboration
infra (DB, handover, hooks) meant to be copied into other projects.

## Session continuity

- `.claude/state.db` (SQLite, gitignored, local-only) holds session handover
  notes. View it with the VS Code SQLite Viewer extension.
- SessionStart hook auto-injects the last handover into context.
- Stop hook blocks once if no handover was logged this session — follow the
  command it prints (`.claude/scripts/db.py log --session ...`).
- DB helper: `.claude/scripts/db.py` (stdlib-only, run with `python`).
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

## Stack

Not yet decided — no app code exists yet.
