"""
Schema + connection for state.db (SQLite, gitignored). Stdlib only, no deps.
Shared by every hooks/cli module - nothing here depends on them.

Table: handovers(id, ts, summary, next_steps, questions, session_id, delivered)
  Plain columns, no JSON - written ONLY on-command (the `log` subcommand,
  normally via the /handover skill), never automatically. A session that
  ends without the user saying "handover" leaves no row here - that's
  deliberate, not a bug (see cmd_stop_check in hooks/sessions.py). One row
  per log call, not one per session - a session can log more than once.
  SessionStart surfaces the latest row from up to HANDOVER_SHOW_COUNT
  distinct sessions that haven't been delivered yet (delivered=0), most
  recent first, then marks exactly those rows delivered=1 - so a handover
  is replayed to the next session once and never again, instead of
  re-showing the same stale context to every SessionStart for a rolling
  time window. Never pruned.
Table: sessions(name, started_ts, last_seen_ts, status, session_id)
  One row per session, updated in place. name is a random adjective-noun
  label assigned once on first touch, purely so a human can tell sessions
  apart at a glance - not an identifier, session_id still is. started_ts set
  once; last_seen_ts bumped on every session-start, stop-check, and
  context-check call (a heartbeat - context-check is wired to both
  UserPromptSubmit and PostToolUse, so it also gives tool-call granularity,
  not just once per turn). status = 'live' | 'dead': set to 'live' on every
  heartbeat, set to 'dead' by session-end (which used to delete the row
  outright - now it marks it dead instead, so closed sessions stay visible).
  A session abandoned without SessionEnd firing is caught by
  reap_stale_sessions (called on every heartbeat via touch_session, AND at
  the top of session_activity_note - see STALE_MINUTES /
  LIKELY_DEAD_GRACE_MINUTES in lib/sessions.py), which writes status='dead'
  back to the row itself - not just a display-time filter, so the table is
  trustworthy on its own (e.g. read directly in the SQLite viewer). The
  session_activity_note call matters specifically because git_safe.py's
  push guard reads that function directly and never calls touch_session -
  without a reap there too, a dead session's row could read 'live' forever
  regardless of wall-clock time (task #3).
Table: context_watch(id, ts, session_id, level)
  level = 'soft' | 'hard'. One row per threshold crossed per session -
  written once each by context-check so the same token-usage nudge doesn't
  repeat every turn. Low volume by construction (at most 2 rows/session).
Table: tasks(id, status, priority, category, task_title, task_details,
             created_ts, updated_ts)
  Named `todos` before 2026-08-20 - renamed for how much easier "task" is to
  type/say than "todo"; get_conn() migrates any pre-existing `todos` table's
  rows across once, then drops it.
  status = 'open' | 'discussing' | 'rejected' | 'closed' (CHECK-constrained).
  priority = 1 (blocking/do next) | 2 (important, soon) | 3 (worth doing, no
  rush) | 4 (someday). Required (NOT NULL, CHECK-constrained) - no untriaged
  state, every task gets a real priority at creation. Not Eisenhower's
  quadrants on purpose — "delegate" doesn't mean anything in a two-party
  repo, so these are our own plain-language levels instead.
  category = 'infra' (the Claude-collaboration tooling, meant to be portable
  to other projects) | 'app' (the prediction-market product itself). Required,
  same as priority.
  The source of truth for cross-session work items — unlike handover's free-
  text "next steps", a task persists and keeps showing up at every
  SessionStart until it's explicitly moved to rejected/closed, so nothing
  gets silently dropped just because a later handover's prose didn't repeat
  it. task_details doubles as the running note: status changes append a
  timestamped note there (e.g. why something was rejected) rather than
  overwriting the original description. Sort order everywhere is priority,
  then id. See TASK_BLOAT_THRESHOLD (lib/tasks.py) for the soft-cap reminder
  on open+discussing count.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# This file lives at .claude/scripts/lib/schema.py - parents[2] is .claude,
# so state.db always resolves next to the .claude directory regardless of
# which subfolder (lib/hooks/cli) ends up importing it.
DB_PATH = str(Path(__file__).resolve().parents[2] / "state.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS handovers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            summary TEXT NOT NULL,
            next_steps TEXT NOT NULL,
            questions TEXT,
            session_id TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0
        )"""
    )
    try:
        conn.execute("ALTER TABLE handovers ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
        # One-time: don't replay everything logged before this migration existed.
        # Committed here, not left to the caller - some callers (e.g.
        # cmd_context_check's no-op path) close the connection without ever
        # calling commit(), which would silently roll this back.
        conn.execute("UPDATE handovers SET delivered = 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS context_watch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT NOT NULL,
            level TEXT NOT NULL CHECK(level IN ('soft', 'hard'))
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            name TEXT,
            started_ts TEXT NOT NULL,
            last_seen_ts TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'live' CHECK(status IN ('live', 'dead')),
            session_id TEXT PRIMARY KEY
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL CHECK(status IN ('open', 'discussing', 'rejected', 'closed')),
            priority INTEGER NOT NULL CHECK(priority IN (1, 2, 3, 4)),
            category TEXT NOT NULL CHECK(category IN ('infra', 'app')),
            task_title TEXT NOT NULL,
            task_details TEXT,
            created_ts TEXT NOT NULL,
            updated_ts TEXT NOT NULL
        )"""
    )
    # One-time migration from the old `todos` table name (renamed 2026-08-20).
    # Idempotent: once the rows are copied and `todos` dropped, this is a
    # no-op on every later call since the SELECT finds no such table.
    had_todos = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='todos'"
    ).fetchone()
    if had_todos:
        conn.execute(
            "INSERT INTO tasks (id, status, priority, category, task_title, "
            "task_details, created_ts, updated_ts) "
            "SELECT id, status, priority, category, task_title, task_details, "
            "created_ts, updated_ts FROM todos"
        )
        conn.execute("DROP TABLE todos")
        conn.commit()
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}
