"""
Local state DB for session continuity. Stdlib only (sqlite3), no deps.

Table: log(id, ts, session_id, type, content)
  type = 'session_start' | 'facts' | 'handover' | 'context_watch'
Table: sessions(session_id, started_ts, last_seen_ts)
  One row per session, updated in place. started_ts set once; last_seen_ts
  bumped on every session-start and stop-check call (a heartbeat, since
  stop-check fires every turn). Used to warn a new session if another
  session was seen recently in this repo (see STALE_MINUTES) — crashed
  sessions age out instead of leaving a permanent false "active" flag.
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
                                                Records session start, prints
                                                SessionStart hookSpecificOutput
                                                JSON with the last handover.
  stop-check                                   stdin: hook JSON (session_id).
                                                Records git facts. If no
                                                handover logged this session,
                                                prints a blocking decision.
  context-check                                stdin: hook JSON (session_id,
                                                transcript_path). UserPromptSubmit
                                                hook. Reads the real token usage
                                                of the last assistant turn from
                                                the transcript; once per session,
                                                prints a soft (100k) then a hard
                                                (145k) handover-now nudge. Never
                                                blocks; silent on any error.
  session-end                                  stdin: hook JSON (session_id).
                                                SessionEnd hook. Deletes this
                                                session's row from `sessions`
                                                immediately, so it stops
                                                counting as active. Confirmed
                                                to fire on explicit exit,
                                                /clear, and logout; NOT
                                                confirmed to fire when an IDE
                                                tab is abandoned by jumping
                                                straight to a new session -
                                                that gap is instead covered
                                                by the single-heartbeat
                                                grace period in
                                                session_activity_note (see
                                                LIKELY_DEAD_GRACE_MINUTES).
                                                Silent; never blocks.
  log --session ID --summary S --next N [--questions Q]
                                                Insert a handover row.
  prune [--days N] [--vacuum]                  Delete session_start/facts/
                                                context_watch rows and dead
                                                sessions rows older than N
                                                days (default 30). handover
                                                rows are never deleted. Manual
                                                only - not wired to a hook.
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
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.db")

CONTEXT_SOFT = 100_000
CONTEXT_HARD = 145_000
CONTEXT_WINDOW = 200_000
STALE_MINUTES = 30
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


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT,
            type TEXT NOT NULL,
            content TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            started_ts TEXT NOT NULL,
            last_seen_ts TEXT NOT NULL
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


def git_facts():
    def run(args):
        try:
            out = subprocess.run(
                ["git"] + args, capture_output=True, text=True, timeout=10
            )
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    branch = run(["branch", "--show-current"])
    status = run(["status", "--short"])
    last_commit = run(["log", "-1", "--oneline"])
    return {
        "branch": branch,
        "status": status or "(clean)",
        "last_commit": last_commit or "(no commits yet)",
    }


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


def touch_session(conn, session_id):
    """Upsert this session's heartbeat. started_ts set once; last_seen_ts always bumped."""
    if not session_id:
        return
    ts = now()
    conn.execute(
        "INSERT INTO sessions (session_id, started_ts, last_seen_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET last_seen_ts = excluded.last_seen_ts",
        (session_id, ts, ts),
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
        "SELECT session_id, started_ts, last_seen_ts FROM sessions WHERE session_id != ?",
        (session_id,),
    ).fetchall()

    active, likely_dead = [], []
    for sid, started, last_seen in rows:
        try:
            last_ts = datetime.fromisoformat(last_seen)
            start_ts = datetime.fromisoformat(started)
        except ValueError:
            continue
        if last_ts < cutoff:
            continue
        never_ticked = last_seen == started
        if never_ticked and start_ts < dead_grace_cutoff:
            likely_dead.append((sid, last_seen))
        else:
            active.append((sid, last_seen))

    if not active:
        base = f"No other sessions seen active in this repo in the last {STALE_MINUTES}min."
        if likely_dead:
            lines = "\n".join(f"  {sid} (last seen {ts})" for sid, ts in likely_dead)
            base += (
                f" ({len(likely_dead)} session(s) have a single stale heartbeat and "
                f"are treated as likely-dead, not blocking:\n{lines})"
            )
        return base

    lines = "\n".join(f"  {sid} (last seen {ts})" for sid, ts in active)
    note = (
        f"{len(active)} other session(s) possibly active in this repo (heartbeat "
        f"within {STALE_MINUTES}min) - check before editing outside a worktree:\n{lines}"
    )
    if likely_dead:
        dead_lines = "\n".join(f"  {sid} (last seen {ts})" for sid, ts in likely_dead)
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


def cmd_session_start(_args):
    data = read_stdin_json()
    session_id = data.get("session_id", "")
    conn = get_conn()
    conn.execute(
        "INSERT INTO log (ts, session_id, type, content) VALUES (?, ?, 'session_start', ?)",
        (now(), session_id, ""),
    )
    conn.commit()
    activity_note = session_activity_note(conn, session_id)
    todos_line = todos_note(conn)
    touch_session(conn, session_id)
    row = conn.execute(
        "SELECT ts, content FROM log WHERE type = 'handover' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    git_line = git_worktree_summary()

    if row:
        ts, content = row
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parsed = {"summary": content, "next": "", "questions": ""}
        context = (
            f"# Handover (last saved {ts})\n"
            f"Summary: {parsed.get('summary', '')}\n"
            f"Next steps: {parsed.get('next', '')}\n"
            f"Open questions: {parsed.get('questions', '')}\n\n"
            f"{git_line}\n"
            f"{activity_note}\n\n"
            f"{todos_line}\n\n"
            f"This session id: {session_id}\n"
            f"Before finishing this session, log a handover:\n"
            f'python .claude/scripts/db.py log --session {session_id} '
            f'--summary "..." --next "..." [--questions "..."]'
        )
    else:
        context = (
            "No prior handover found (first session on this repo).\n"
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
    conn.execute(
        "INSERT INTO log (ts, session_id, type, content) VALUES (?, ?, 'facts', ?)",
        (now(), session_id, json.dumps(git_facts())),
    )
    conn.commit()
    touch_session(conn, session_id)
    row = conn.execute(
        "SELECT id FROM log WHERE type = 'handover' AND session_id = ? LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()

    if row:
        print(json.dumps({"suppressOutput": True}))
        return

    print(json.dumps({
        "decision": "block",
        "reason": (
            "No handover logged yet for this session. This is a one-time safety-net "
            "checkpoint (so an abandoned session still leaves a note), NOT a signal "
            "that the session is ending - it can fire after the very first turn. Log "
            "one now, silently, then finish your response normally:\n"
            f'python .claude/scripts/db.py log --session {session_id} '
            '--summary "<what happened so far>" --next "<what to do next>" '
            '[--questions "<open questions>"]\n'
            "Do not tell the user the session is over or ready to close because of "
            "this checkpoint - only say that if they actually asked to wrap up."
        ),
    }))


def cmd_context_check(_args):
    try:
        data = read_stdin_json()
        session_id = data.get("session_id", "")
        tokens = current_context_tokens(data.get("transcript_path", ""))
        if tokens is None:
            return

        conn = get_conn()
        fired = {
            row[0] for row in conn.execute(
                "SELECT content FROM log WHERE type = 'context_watch' AND session_id = ?",
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
            "INSERT INTO log (ts, session_id, type, content) VALUES (?, ?, 'context_watch', ?)",
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
            print(
                f"[CONTEXT WATCH] ~{k}k tokens (~{pct}% of {CONTEXT_WINDOW // 1000}k). "
                f"SOFT threshold ({CONTEXT_SOFT // 1000}k) crossed - start wrapping up, "
                f"log a handover at the next natural stopping point:\n{log_cmd}"
            )
        else:
            print(
                f"[CONTEXT WATCH] ~{k}k tokens (~{pct}% of {CONTEXT_WINDOW // 1000}k). "
                f"HARD threshold ({CONTEXT_HARD // 1000}k) crossed - auto-compact fires "
                f"around 160k. Finish only what's in flight, then log a handover now and "
                f"start a fresh session:\n{log_cmd}"
            )
    except Exception:
        return


def cmd_log(args):
    conn = get_conn()
    content = json.dumps({
        "summary": args.summary,
        "next": args.next,
        "questions": args.questions or "",
    })
    conn.execute(
        "INSERT INTO log (ts, session_id, type, content) VALUES (?, ?, 'handover', ?)",
        (now(), args.session, content),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"systemMessage": "Handover logged."}))


def cmd_session_end(_args):
    try:
        data = read_stdin_json()
        session_id = data.get("session_id", "")
        if not session_id:
            return
        conn = get_conn()
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
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
    for log_type in ("session_start", "facts", "context_watch"):
        cur = conn.execute(
            "DELETE FROM log WHERE type = ? AND ts < ?", (log_type, cutoff)
        )
        deleted[log_type] = cur.rowcount
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
    sub.add_parser("session-end").set_defaults(func=cmd_session_end)

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
