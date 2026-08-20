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
  log --session ID --summary S --next N [--questions Q]
                                                Insert a handover row.
  prune [--days N] [--vacuum]                  Delete session_start/facts/
                                                context_watch rows and dead
                                                sessions rows older than N
                                                days (default 30). handover
                                                rows are never deleted. Manual
                                                only - not wired to a hook.
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
    """Other sessions whose heartbeat is fresher than STALE_MINUTES, excluding this one."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    rows = conn.execute(
        "SELECT session_id, last_seen_ts FROM sessions WHERE session_id != ?",
        (session_id,),
    ).fetchall()

    active = []
    for sid, last_seen in rows:
        try:
            ts = datetime.fromisoformat(last_seen)
        except ValueError:
            continue
        if ts >= cutoff:
            active.append((sid, last_seen))

    if not active:
        return f"No other sessions seen active in this repo in the last {STALE_MINUTES}min."

    lines = "\n".join(f"  {sid} (last seen {ts})" for sid, ts in active)
    return (
        f"{len(active)} other session(s) possibly active in this repo (heartbeat "
        f"within {STALE_MINUTES}min) - check before editing outside a worktree:\n{lines}"
    )


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
            "No handover logged yet for this session. Before stopping, run:\n"
            f'python .claude/scripts/db.py log --session {session_id} '
            '--summary "<what happened>" --next "<what to do next>" '
            '[--questions "<open questions>"]\n'
            "Then finish."
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
