"""
Local state DB for session continuity. Stdlib only (sqlite3), no deps.

Table: handovers(id, ts, summary, next_steps, questions, session_id, delivered)
  Plain columns, no JSON - written ONLY on-command (the `log` subcommand,
  normally via the /handover skill), never automatically. A session that
  ends without the user saying "handover" leaves no row here - that's
  deliberate, not a bug (see cmd_stop_check). One row per log call, not one
  per session - a session can log more than once. SessionStart surfaces the
  latest row from up to HANDOVER_SHOW_COUNT distinct sessions that haven't
  been delivered yet (delivered=0), most recent first, then marks exactly
  those rows delivered=1 - so a handover is replayed to the next session
  once and never again, instead of re-showing the same stale context to
  every SessionStart for a rolling time window. Never pruned.
Table: sessions(name, started_ts, last_seen_ts, status, session_id)
  One row per session, updated in place. name is a random adjective-noun
  label assigned once on first touch, purely so a human can tell sessions
  apart at a glance - not an identifier, session_id still is. started_ts set
  once; last_seen_ts bumped on every session-start and stop-check call (a
  heartbeat, since stop-check fires every turn - this is the ONLY thing
  stop-check still does). status = 'live' | 'dead': set to 'live' on every
  heartbeat, set to 'dead' by session-end (which used to delete the row
  outright - now it marks it dead instead, so closed sessions stay visible).
  A session abandoned without SessionEnd firing is caught by
  reap_stale_sessions (called on every heartbeat, see STALE_MINUTES /
  LIKELY_DEAD_GRACE_MINUTES), which writes status='dead' back to the row
  itself - not just a display-time filter, so the table is trustworthy on
  its own (e.g. read directly in the SQLite viewer).
Table: context_watch(id, ts, session_id, level)
  level = 'soft' | 'hard'. One row per threshold crossed per session -
  written once each by context-check so the same token-usage nudge doesn't
  repeat every turn. Low volume by construction (at most 2 rows/session).
Table: todos(id, status, priority, category, task_title, task_details,
             created_ts, updated_ts)
  status = 'open' | 'discussing' | 'rejected' | 'closed' (CHECK-constrained).
  priority = 1 (blocking/do next) | 2 (important, soon) | 3 (worth doing, no
  rush) | 4 (someday). Required (NOT NULL, CHECK-constrained) - no untriaged
  state, every todo gets a real priority at creation. Not Eisenhower's
  quadrants on purpose — "delegate" doesn't mean anything in a two-party
  repo, so these are our own plain-language levels instead.
  category = 'infra' (the Claude-collaboration tooling, meant to be portable
  to other projects) | 'app' (the prediction-market product itself). Required,
  same as priority.
  The source of truth for cross-session work items — unlike handover's free-
  text "next steps", a todo persists and keeps showing up at every
  SessionStart until it's explicitly moved to rejected/closed, so nothing
  gets silently dropped just because a later handover's prose didn't repeat
  it. task_details doubles as the running note: status changes append a
  timestamped note there (e.g. why something was rejected) rather than
  overwriting the original description. Sort order everywhere is priority,
  then id. See TODO_BLOAT_THRESHOLD for the soft-cap reminder on
  open+discussing count.

Subcommands:
  init                                        create db/table if missing
  session-start                                stdin: hook JSON (session_id).
                                                Prints SessionStart
                                                hookSpecificOutput JSON with
                                                undelivered handovers (see
                                                HANDOVER_SHOW_COUNT).
  stop-check                                   stdin: hook JSON (session_id).
                                                Just bumps this session's
                                                heartbeat in `sessions`.
                                                Never blocks, never logs
                                                anything else - handover
                                                logging is purely on-command
                                                (see `log` below), by design
                                                there is no automatic
                                                safety-net for an abandoned
                                                session.
  context-check                                stdin: hook JSON (session_id,
                                                transcript_path). Wired to BOTH
                                                UserPromptSubmit and PostToolUse
                                                - a single long turn can run many
                                                tool calls with no new user
                                                prompt in between, and
                                                UserPromptSubmit alone would
                                                never get a chance to run until
                                                that turn finally ended, letting
                                                usage blow straight past 145k
                                                unnoticed (observed: not caught
                                                until 195k). PostToolUse re-runs
                                                the same check after every tool
                                                call so growth mid-turn gets
                                                seen too. Reads the real token
                                                usage of the last assistant turn
                                                from the transcript; once per
                                                session, prints a soft (100k)
                                                then a hard (145k) handover-now
                                                nudge. Never blocks; silent on
                                                any error.
  todo-remind                                  PostToolUse hook, matcher
                                                "TodoWrite" (see settings.json).
                                                Fires every time the built-in
                                                TodoWrite tool is used and
                                                injects a static reminder to
                                                mirror any durable, cross-
                                                session items into this table
                                                via todo-add - TodoWrite itself
                                                is per-session/ephemeral and
                                                cannot supply the required
                                                priority/category, so this
                                                cannot be fully automatic; it's
                                                a nudge, not a sync. Always
                                                fires, never dedups - cheap and
                                                the repetition on multi-call
                                                turns is an accepted tradeoff
                                                over building dedup logic for
                                                it.
  session-end [--session ID]                   No --session: stdin hook JSON
                                                (session_id) - SessionEnd
                                                hook, marks that session's row
                                                dead immediately. Confirmed to
                                                fire on explicit exit, /clear,
                                                and logout; NOT confirmed to
                                                fire when an IDE tab is
                                                abandoned by jumping straight
                                                to a new session - that gap is
                                                covered by reap_stale_sessions
                                                instead. With --session ID:
                                                manual escape hatch - mark a
                                                specific session dead on the
                                                spot (e.g. to unblock
                                                git_safe.py's push guard
                                                without waiting out
                                                STALE_MINUTES or reaching for
                                                --override-session-guard).
                                                Silent on the hook path; never
                                                blocks.
  log --session ID --summary S --next N [--questions Q]
                                                Insert a handover row. The
                                                only way a handover ever gets
                                                written - normally invoked
                                                via the /handover skill.
  prune [--days N] [--vacuum]                  Delete context_watch rows and
                                                dead sessions rows older than
                                                N days (default 30).
                                                handovers are never deleted.
                                                Manual only - not wired to a
                                                hook.
  todo-add --title T --category infra|app       Insert a todo, status='open'.
    --priority 1-4 [--details D]                Priority is required, same
                                                 as category. Prints its id
                                                 (+ a bloat reminder once
                                                 open+discussing reaches
                                                 TODO_BLOAT_THRESHOLD).
  todo-status --id N --status S [--note N]      Update a todo's status
    [--priority 1-4] [--category infra|app]     ('open'|'discussing'|
                                                 'rejected'|'closed'). --note
                                                 is appended (timestamped) to
                                                 task_details, not a
                                                 replacement. Omit --priority/
                                                 --category to leave unchanged.
  todo-list [--status S1,S2,...]               Print todos as JSON, sorted
    [--category infra|app]                     priority-first. Default
                                                status filter: open,discussing.
"""
import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.db")

CONTEXT_SOFT = 100_000
CONTEXT_HARD = 145_000
CONTEXT_WINDOW = 200_000
STALE_MINUTES = 5
# Safe at 5 (down from 30) because touch_session now also runs on PostToolUse
# (see cmd_context_check) - heartbeat has tool-call granularity, not just
# once-per-turn, so a long-running turn keeps refreshing last_seen_ts instead
# of going quiet until Stop. Was 30 specifically to tolerate the old
# once-per-turn cadence; a real dead session sitting 'live' (and blocking
# git_safe.py pushes) for up to 30min was the actual cost of that, confirmed
# on session 6fcf78ff (happy-falcon) 2026-08-20.
# A session whose last_seen_ts still equals its started_ts (i.e. it never
# ticked past its first heartbeat) is only "possibly active" for this long -
# past this, one frozen heartbeat is a stronger signal of an abandoned
# session (e.g. closed by jumping straight to a new session) than of a
# session still working its first turn.
LIKELY_DEAD_GRACE_MINUTES = 3
# Soft cap, not a hard block: once open+discussing todos reach this count,
# todo-add/todo-status/SessionStart surface a reminder to close/reject/
# deprioritize before piling on more, instead of letting the list bloat
# silently.
TODO_BLOAT_THRESHOLD = 6
# SessionStart shows at most this many distinct sessions' latest undelivered
# handover (current + 1 previous) - not a time window. Combined with the
# delivered flag, a handover is shown once (to whichever session starts
# next) and then never replayed again.
HANDOVER_SHOW_COUNT = 2
SESSION_NAME_ADJECTIVES = [
    "brave", "calm", "eager", "fuzzy", "gentle", "happy", "jolly", "keen",
    "lively", "mighty", "nimble", "proud", "quiet", "rapid", "sunny",
    "witty", "zesty", "bold", "cozy", "daring",
]
SESSION_NAME_NOUNS = [
    "falcon", "otter", "tiger", "panda", "eagle", "dolphin", "wolf", "fox",
    "lynx", "hawk", "bear", "heron", "koala", "raven", "stag", "orca",
    "puma", "crane", "gecko", "moth",
]


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
        """CREATE TABLE IF NOT EXISTS todos (
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
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def current_context_tokens(transcript_path):
    """Real token usage of the latest assistant turn, from the transcript's own
    API usage field (input + cache_read + cache_creation). None if unavailable.
    """
    p = Path(transcript_path)
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        usage = (obj.get("message") or {}).get("usage")
        if not usage:
            continue
        return (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
    return None


def git_worktree_summary():
    def run(args):
        try:
            out = subprocess.run(
                ["git"] + args, capture_output=True, text=True, timeout=10
            )
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    branch = run(["branch", "--show-current"]) or "(detached HEAD)"
    worktrees = run(["worktree", "list"])
    lines = [l for l in worktrees.splitlines() if l.strip()]
    if len(lines) > 1:
        listing = "\n".join(f"  {l}" for l in lines)
        note = (
            f"Other worktrees exist for this repo - check whether they're in "
            f"use by another session before editing files here:\n{listing}"
        )
    else:
        note = "No other worktrees for this repo right now."
    return f"Current branch: {branch}\n{note}"


def random_session_name():
    return f"{random.choice(SESSION_NAME_ADJECTIVES)}-{random.choice(SESSION_NAME_NOUNS)}"


def reap_stale_sessions(conn):
    """Flip clearly-abandoned sessions to status='dead' in the DB itself,
    not just in the transient note shown at SessionStart. Two cases:
    - never ticked past its first heartbeat, and that heartbeat is older
      than LIKELY_DEAD_GRACE_MINUTES (abandoned before finishing a turn);
    - any session (ticked or not) whose last heartbeat is older than
      STALE_MINUTES (no longer actively running).
    Without this, a session closed by jumping straight to a new one (no
    SessionEnd) stays 'live' forever - session_activity_note only filtered
    it out of the printed note, it never corrected the stored status.
    """
    now_dt = datetime.now(timezone.utc)
    stale_cutoff = (now_dt - timedelta(minutes=STALE_MINUTES)).isoformat()
    grace_cutoff = (now_dt - timedelta(minutes=LIKELY_DEAD_GRACE_MINUTES)).isoformat()
    conn.execute(
        "UPDATE sessions SET status = 'dead' WHERE status = 'live' AND "
        "(last_seen_ts < ? OR (last_seen_ts = started_ts AND started_ts < ?))",
        (stale_cutoff, grace_cutoff),
    )


def touch_session(conn, session_id):
    """Upsert this session's heartbeat. name/started_ts set once (on insert);
    last_seen_ts and status always bumped to 'live' - session-end is the only
    other thing that sets status to 'dead' (reap_stale_sessions is the rest).
    """
    if not session_id:
        return
    reap_stale_sessions(conn)
    ts = now()
    conn.execute(
        "INSERT INTO sessions (name, started_ts, last_seen_ts, status, session_id) "
        "VALUES (?, ?, ?, 'live', ?) "
        "ON CONFLICT(session_id) DO UPDATE SET last_seen_ts = excluded.last_seen_ts, status = 'live'",
        (random_session_name(), ts, ts, session_id),
    )
    conn.commit()


def session_activity_note(conn, session_id):
    """Other sessions whose heartbeat is fresher than STALE_MINUTES, excluding this one.

    A session that never ticked past its first heartbeat (last_seen_ts ==
    started_ts) is downgraded to "likely dead" - and excluded from the
    blocking count - once it's older than LIKELY_DEAD_GRACE_MINUTES. Real
    sessions accumulate a Stop-hook tick roughly once per turn; a session
    stuck on exactly one heartbeat for several minutes was almost always
    abandoned (e.g. closed by jumping straight to a new session), not left
    mid-turn.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    dead_grace_cutoff = datetime.now(timezone.utc) - timedelta(minutes=LIKELY_DEAD_GRACE_MINUTES)
    rows = conn.execute(
        "SELECT session_id, name, started_ts, last_seen_ts FROM sessions "
        "WHERE session_id != ? AND status = 'live'",
        (session_id,),
    ).fetchall()

    def label(sid, name):
        return f"{name} ({sid})" if name else sid

    active, likely_dead = [], []
    for sid, name, started, last_seen in rows:
        try:
            last_ts = datetime.fromisoformat(last_seen)
            start_ts = datetime.fromisoformat(started)
        except ValueError:
            continue
        if last_ts < cutoff:
            continue
        never_ticked = last_seen == started
        if never_ticked and start_ts < dead_grace_cutoff:
            likely_dead.append((sid, name, last_seen))
        else:
            active.append((sid, name, last_seen))

    if not active:
        base = f"No other sessions seen active in this repo in the last {STALE_MINUTES}min."
        if likely_dead:
            lines = "\n".join(f"  {label(sid, name)} (last seen {ts})" for sid, name, ts in likely_dead)
            base += (
                f" ({len(likely_dead)} session(s) have a single stale heartbeat and "
                f"are treated as likely-dead, not blocking:\n{lines})"
            )
        return base

    lines = "\n".join(f"  {label(sid, name)} (last seen {ts})" for sid, name, ts in active)
    note = (
        f"{len(active)} other session(s) possibly active in this repo (heartbeat "
        f"within {STALE_MINUTES}min) - check before editing outside a worktree:\n{lines}"
    )
    if likely_dead:
        dead_lines = "\n".join(f"  {label(sid, name)} (last seen {ts})" for sid, name, ts in likely_dead)
        note += (
            f"\n{len(likely_dead)} other session(s) look likely-dead (single stale "
            f"heartbeat, not counted above):\n{dead_lines}"
        )
    return note


def check_todo_bloat(conn):
    """Soft-cap reminder, not a block: nudge once open+discussing todos reach
    TODO_BLOAT_THRESHOLD, so the list gets triaged before it grows unreadable.
    """
    count = conn.execute(
        "SELECT COUNT(*) FROM todos WHERE status IN ('open', 'discussing')"
    ).fetchone()[0]
    if count < TODO_BLOAT_THRESHOLD:
        return None
    return (
        f"{count} open/discussing todos - at or above the soft limit of "
        f"{TODO_BLOAT_THRESHOLD}. Not a hard block, but close, reject, or "
        f"deprioritize some before adding more."
    )


def todos_note(conn):
    """Every todo still open or discussing, for SessionStart - so an item can't
    silently drop off just because a later handover's prose didn't repeat it.
    Sorted priority-first, then by id.
    """
    rows = conn.execute(
        "SELECT id, status, priority, category, task_title FROM todos "
        "WHERE status IN ('open', 'discussing') ORDER BY priority, id"
    ).fetchall()
    if not rows:
        return "No open or discussing todos."
    lines = "\n".join(
        f"  #{tid} [{status}] P{priority} [{category}] {title}"
        for tid, status, priority, category, title in rows
    )
    note = f"{len(rows)} open/discussing todo(s):\n{lines}"
    bloat = check_todo_bloat(conn)
    if bloat:
        note = f"[REMINDER] {bloat}\n{note}"
    return note


def cmd_init(_args):
    get_conn().close()


def recent_handovers(conn, exclude_session_id, limit=HANDOVER_SHOW_COUNT):
    """Latest undelivered handover from up to `limit` distinct sessions (other
    than exclude_session_id), most recent first. Marks exactly the returned
    rows delivered=1, so this same handover never gets shown again.
    """
    rows = conn.execute(
        "SELECT id, session_id, ts, summary, next_steps, questions FROM handovers "
        "WHERE delivered = 0 AND session_id != ? ORDER BY ts DESC",
        (exclude_session_id,),
    ).fetchall()
    picked_ids, out, seen_sessions = [], [], set()
    for row_id, session_id, ts, summary, next_steps, questions in rows:
        if session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)
        picked_ids.append(row_id)
        out.append((session_id, (ts, summary, next_steps, questions)))
        if len(out) >= limit:
            break
    if picked_ids:
        placeholders = ",".join("?" for _ in picked_ids)
        conn.execute(f"UPDATE handovers SET delivered = 1 WHERE id IN ({placeholders})", picked_ids)
        conn.commit()
    return out


def format_handovers(handovers):
    if not handovers:
        return "No undelivered handover (first session on this repo, or nothing new since the last one)."
    blocks = []
    for session_id, (ts, summary, next_steps, questions) in handovers:
        blocks.append(
            f"# Handover from session {session_id} (saved {ts})\n"
            f"Summary: {summary}\n"
            f"Next steps: {next_steps}\n"
            f"Open questions: {questions or ''}"
        )
    return "\n\n".join(blocks)


def cmd_session_start(_args):
    data = read_stdin_json()
    session_id = data.get("session_id", "")
    conn = get_conn()
    activity_note = session_activity_note(conn, session_id)
    todos_line = todos_note(conn)
    touch_session(conn, session_id)
    handovers = recent_handovers(conn, session_id)
    conn.close()

    git_line = git_worktree_summary()
    handover_block = format_handovers(handovers)

    context = (
        f"{handover_block}\n\n"
        f"{git_line}\n"
        f"{activity_note}\n\n"
        f"{todos_line}\n\n"
        f"This session id: {session_id}\n"
        f"Before finishing this session, log a handover:\n"
        f'python .claude/scripts/db.py log --session {session_id} '
        f'--summary "..." --next "..." [--questions "..."]'
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


def cmd_stop_check(_args):
    data = read_stdin_json()
    session_id = data.get("session_id", "")
    conn = get_conn()
    touch_session(conn, session_id)
    conn.close()
    print(json.dumps({"suppressOutput": True}))


def cmd_context_check(_args):
    try:
        data = read_stdin_json()
        hook_event_name = data.get("hook_event_name", "UserPromptSubmit")
        session_id = data.get("session_id", "")

        conn = get_conn()
        # Piggyback the heartbeat on PostToolUse (this hook already fires
        # there, see settings.json) so staleness has tool-call granularity,
        # not just once-per-turn (Stop). Without this, STALE_MINUTES has to
        # stay large to avoid falsely reaping a session mid-turn during a
        # long tool-call sequence - with it, a genuinely dead session gets
        # caught much faster without that false-positive risk.
        touch_session(conn, session_id)

        tokens = current_context_tokens(data.get("transcript_path", ""))
        if tokens is None:
            conn.close()
            return

        fired = {
            row[0] for row in conn.execute(
                "SELECT level FROM context_watch WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        }

        level = None
        if tokens >= CONTEXT_HARD and "hard" not in fired:
            level = "hard"
        elif tokens >= CONTEXT_SOFT and "soft" not in fired:
            level = "soft"

        if level is None:
            conn.close()
            return

        conn.execute(
            "INSERT INTO context_watch (ts, session_id, level) VALUES (?, ?, ?)",
            (now(), session_id, level),
        )
        conn.commit()
        conn.close()

        pct = round(100 * tokens / CONTEXT_WINDOW)
        k = round(tokens / 1000)
        log_cmd = (
            f'python .claude/scripts/db.py log --session {session_id} '
            f'--summary "..." --next "..." [--questions "..."]'
        )
        if level == "soft":
            message = (
                f"[CONTEXT WATCH] ~{k}k tokens (~{pct}% of {CONTEXT_WINDOW // 1000}k). "
                f"SOFT threshold ({CONTEXT_SOFT // 1000}k) crossed - start wrapping up, "
                f"log a handover at the next natural stopping point:\n{log_cmd}"
            )
        else:
            message = (
                f"[CONTEXT WATCH] ~{k}k tokens (~{pct}% of {CONTEXT_WINDOW // 1000}k). "
                f"HARD threshold ({CONTEXT_HARD // 1000}k) crossed - auto-compact fires "
                f"around 160k. Finish only what's in flight, then log a handover now and "
                f"start a fresh session:\n{log_cmd}"
            )
        # Plain stdout only auto-injects into model context for
        # UserPromptSubmit. PostToolUse (also wired to this check, see
        # settings.json) silently drops plain text - needs this JSON form
        # or the warning never reaches the model at all.
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": hook_event_name,
                "additionalContext": message,
            }
        }))
    except Exception:
        return


def cmd_log(args):
    conn = get_conn()
    conn.execute(
        "INSERT INTO handovers (ts, session_id, summary, next_steps, questions) "
        "VALUES (?, ?, ?, ?, ?)",
        (now(), args.session, args.summary, args.next, args.questions or ""),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"systemMessage": "Handover logged."}))


def cmd_todo_remind(_args):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "[TODO SYNC] If any item just written to the TodoWrite list is a "
                "durable, cross-session work item (not just a step for the task "
                "in front of you), mirror it into the persistent backlog now: "
                "python .claude/scripts/db.py todo-add --title T --category "
                "infra|app --priority 1-4 [--details D]"
            ),
        }
    }))


def cmd_session_end(args):
    try:
        session_id = args.session
        if not session_id:
            data = read_stdin_json()
            session_id = data.get("session_id", "")
        if not session_id:
            return
        conn = get_conn()
        conn.execute("UPDATE sessions SET status = 'dead' WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
        if args.session:
            print(json.dumps({"session_id": session_id, "status": "dead"}))
    except Exception:
        return


def cmd_todo_add(args):
    conn = get_conn()
    ts = now()
    cur = conn.execute(
        "INSERT INTO todos (status, priority, category, task_title, task_details, created_ts, updated_ts) "
        "VALUES ('open', ?, ?, ?, ?, ?, ?)",
        (args.priority, args.category, args.title, args.details or "", ts, ts),
    )
    conn.commit()
    todo_id = cur.lastrowid
    bloat = check_todo_bloat(conn)
    conn.close()
    result = {
        "id": todo_id,
        "status": "open",
        "priority": args.priority,
        "category": args.category,
        "task_title": args.title,
    }
    if bloat:
        result["reminder"] = bloat
    print(json.dumps(result))


def cmd_todo_status(args):
    conn = get_conn()
    row = conn.execute(
        "SELECT task_details, priority, category FROM todos WHERE id = ?", (args.id,)
    ).fetchone()
    if row is None:
        print(json.dumps({"error": f"no todo with id {args.id}"}))
        conn.close()
        sys.exit(1)

    details, priority, category = row
    details = details or ""
    if args.note:
        addition = f"[{now()} -> {args.status}] {args.note}"
        details = f"{details}\n{addition}" if details else addition

    if args.priority is not None:
        priority = args.priority
    if args.category is not None:
        category = args.category

    conn.execute(
        "UPDATE todos SET status = ?, priority = ?, category = ?, task_details = ?, updated_ts = ? WHERE id = ?",
        (args.status, priority, category, details, now(), args.id),
    )
    conn.commit()
    bloat = check_todo_bloat(conn)
    conn.close()
    result = {"id": args.id, "status": args.status, "priority": priority, "category": category}
    if bloat:
        result["reminder"] = bloat
    print(json.dumps(result))


def cmd_todo_list(args):
    statuses = [s.strip() for s in (args.status or "open,discussing").split(",") if s.strip()]
    placeholders = ",".join("?" for _ in statuses)
    params = list(statuses)
    category_clause = ""
    if args.category:
        category_clause = " AND category = ?"
        params.append(args.category)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT id, status, priority, category, task_title, task_details, created_ts, updated_ts "
        f"FROM todos WHERE status IN ({placeholders}){category_clause} "
        f"ORDER BY priority, id",
        params,
    ).fetchall()
    conn.close()
    print(json.dumps([
        {
            "id": r[0],
            "status": r[1],
            "priority": r[2],
            "category": r[3],
            "task_title": r[4],
            "task_details": r[5],
            "created_ts": r[6],
            "updated_ts": r[7],
        }
        for r in rows
    ]))


def cmd_prune(args):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    conn = get_conn()
    deleted = {}
    cur = conn.execute("DELETE FROM context_watch WHERE ts < ?", (cutoff,))
    deleted["context_watch"] = cur.rowcount
    cur = conn.execute("DELETE FROM sessions WHERE last_seen_ts < ?", (cutoff,))
    deleted["sessions"] = cur.rowcount
    conn.commit()
    if args.vacuum:
        conn.execute("VACUUM")
    conn.close()
    print(json.dumps({
        "cutoff_days": args.days,
        "deleted": deleted,
        "total_deleted": sum(deleted.values()),
    }))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("session-start").set_defaults(func=cmd_session_start)
    sub.add_parser("stop-check").set_defaults(func=cmd_stop_check)
    sub.add_parser("context-check").set_defaults(func=cmd_context_check)

    sub.add_parser("todo-remind").set_defaults(func=cmd_todo_remind)

    session_end_p = sub.add_parser("session-end")
    session_end_p.add_argument(
        "--session", default=None,
        help="mark this session_id dead directly - manual escape hatch for "
             "when SessionEnd didn't fire and you know for a fact the "
             "session is closed. Omit to use the hook's stdin JSON instead.",
    )
    session_end_p.set_defaults(func=cmd_session_end)

    log_p = sub.add_parser("log")
    log_p.add_argument("--session", required=True)
    log_p.add_argument("--summary", required=True)
    log_p.add_argument("--next", required=True)
    log_p.add_argument("--questions", default="")
    log_p.set_defaults(func=cmd_log)

    prune_p = sub.add_parser("prune")
    prune_p.add_argument("--days", type=int, default=30)
    prune_p.add_argument("--vacuum", action="store_true")
    prune_p.set_defaults(func=cmd_prune)

    todo_add_p = sub.add_parser("todo-add")
    todo_add_p.add_argument("--title", required=True)
    todo_add_p.add_argument("--details", default="")
    todo_add_p.add_argument("--category", required=True, choices=["infra", "app"])
    todo_add_p.add_argument("--priority", type=int, required=True, choices=[1, 2, 3, 4])
    todo_add_p.set_defaults(func=cmd_todo_add)

    todo_status_p = sub.add_parser("todo-status")
    todo_status_p.add_argument("--id", type=int, required=True)
    todo_status_p.add_argument(
        "--status", required=True, choices=["open", "discussing", "rejected", "closed"]
    )
    todo_status_p.add_argument("--note", default="")
    todo_status_p.add_argument(
        "--priority", type=int, choices=[1, 2, 3, 4], default=None,
        help="omit to leave unchanged",
    )
    todo_status_p.add_argument(
        "--category", choices=["infra", "app"], default=None, help="omit to leave unchanged"
    )
    todo_status_p.set_defaults(func=cmd_todo_status)

    todo_list_p = sub.add_parser("todo-list")
    todo_list_p.add_argument("--status", default="")
    todo_list_p.add_argument("--category", choices=["infra", "app"], default=None)
    todo_list_p.set_defaults(func=cmd_todo_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
