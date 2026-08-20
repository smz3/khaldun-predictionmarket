# khaldun-predictionmarket

Prediction market app. Also carries the standard Claude-collaboration infra
(DB, handover, hooks) meant to be copied into other projects.

## Session continuity

- `.claude/state.db` (SQLite, gitignored) holds `handovers`, `context_watch`,
  `sessions`, `todos`. Full schema + rationale: docstring in
  `.claude/scripts/db.py`.
- SessionStart shows the current + 1 previous undelivered handover, then
  marks them delivered so they don't repeat.
- `/handover` — wrap up a session: saves work, syncs todos, logs a handover.
- `todos` table is the source of truth for cross-session work items. Manage
  via `db.py todo-add` / `todo-status` / `todo-list`.
- Context tripwire: warns at 100k tokens (soft), 145k (hard). Checked on
  both UserPromptSubmit and PostToolUse.
- `sessions.status` self-heals on your next heartbeat — safe to resume a
  session after being idle past 30min.

## Git workflow

- SessionStart flags another active session on `main`? Use a worktree
  (`EnterWorktree`), don't edit directly.
- Nothing flagged, working solo -> use `main` directly.
- Worktree done -> merge into `main`, remove the worktree.
- Commit/push via `.claude/scripts/git_safe.py`, not raw `git commit`/
  `git push`. Auto-push after every commit, no confirmation needed.
- Push takes `--session <id>`. If it's blocked on a session you know is
  actually closed, prefer `db.py session-end --session <id>` to mark it
  dead once (clears the guard for good) over repeating
  `--override-session-guard` on every push.

## Stack

Not yet decided — no app code exists yet.
