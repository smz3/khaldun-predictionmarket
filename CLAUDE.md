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

## Stack

Not yet decided — no app code exists yet.
