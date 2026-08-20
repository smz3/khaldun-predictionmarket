"""
Local state DB for session continuity. Stdlib only (sqlite3), no deps.

Table: log(id, ts, session_id, type, content)
  type = 'session_start' | 'facts' | 'handover'

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
  log --session ID --summary S --next N [--questions Q]
                                                Insert a handover row.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.db")


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
    row = conn.execute(
        "SELECT ts, content FROM log WHERE type = 'handover' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

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
            f"This session id: {session_id}\n"
            f"Before finishing this session, log a handover:\n"
            f'python .claude/scripts/db.py log --session {session_id} '
            f'--summary "..." --next "..." [--questions "..."]'
        )
    else:
        context = (
            "No prior handover found (first session on this repo).\n"
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


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("session-start").set_defaults(func=cmd_session_start)
    sub.add_parser("stop-check").set_defaults(func=cmd_stop_check)

    log_p = sub.add_parser("log")
    log_p.add_argument("--session", required=True)
    log_p.add_argument("--summary", required=True)
    log_p.add_argument("--next", required=True)
    log_p.add_argument("--questions", default="")
    log_p.set_defaults(func=cmd_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
