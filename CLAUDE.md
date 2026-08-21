# khaldun-predictionmarket

Prediction market app. Also carries the standard Claude-collaboration infra
(DB, handover, hooks) meant to be copied into other projects.

## Session continuity

- `.claude/state.db` (SQLite, gitignored) holds `handovers`, `context_watch`,
  `sessions`, `tasks`. Full schema + rationale: docstring in
  `.claude/scripts/lib/schema.py`. `db.py` is just the CLI entrypoint/router;
  logic lives under `.claude/scripts/{hooks,cli,lib}/` by who calls it -
  `hooks/` = only ever invoked by a settings.json hook, `cli/` = only ever
  invoked manually, `lib/` = shared helpers used by both.
- SessionStart shows the current + 1 previous undelivered handover, then
  marks them delivered so they don't repeat.
- `/handover` — wrap up a session: saves work, syncs tasks, logs a handover.
- `tasks` table is the source of truth for cross-session work items. Manage
  via `db.py task-add` / `task-status` / `task-list`. When the user hands you
  a new work item in conversation, log it immediately in the same turn -
  don't wait to be asked.
- Before `task-add`, always run `task-list` first and check by *meaning*,
  not just title text, whether an open/discussing task already covers it -
  a root cause you just found is usually an update to the task that
  prompted the investigation, not a new task. (Task #3 and #5 were the same
  session-death bug filed twice because their titles shared almost no words
  - "Fix session-end: ... tab-close" vs "Reap stale sessions ... touch_session"
  - so don't rely on a text/fuzzy match here, actually read the list.) This
  is a behavioral rule on purpose, not a code-level dedup check - matching
  by meaning needs understanding, not a string-similarity heuristic.
- Context tripwire: warns at 100k tokens (soft), 145k (hard). Checked on
  both UserPromptSubmit and PostToolUse.
- `sessions.status` self-heals on your next heartbeat — safe to resume a
  session after being idle past 5min.

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
