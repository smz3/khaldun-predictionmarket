"""SessionStart / stop-check / session-end hook handlers. Session-lifecycle
plumbing they share with git_safe.py's push guard lives in lib/sessions.py.
"""
import json

from lib.handovers import format_handovers, recent_handovers
from lib.schema import get_conn, read_stdin_json
from lib.sessions import git_worktree_summary, session_activity_note, touch_session
from lib.tasks import tasks_note


def cmd_session_start(_args):
    data = read_stdin_json()
    session_id = data.get("session_id", "")
    conn = get_conn()
    activity_note = session_activity_note(conn, session_id)
    tasks_line = tasks_note(conn)
    touch_session(conn, session_id)
    handovers = recent_handovers(conn, session_id)
    conn.close()

    git_line = git_worktree_summary()
    handover_block = format_handovers(handovers)

    context = (
        f"{handover_block}\n\n"
        f"{git_line}\n"
        f"{activity_note}\n\n"
        f"{tasks_line}\n\n"
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


def cmd_session_end(args):
    """No --session: stdin hook JSON (session_id) - SessionEnd hook, marks
    that session's row dead immediately. Confirmed to fire on explicit exit,
    /clear, and logout; NOT confirmed to fire when an IDE tab is abandoned by
    jumping straight to a new session - that gap is covered by
    reap_stale_sessions instead. With --session ID: manual escape hatch -
    mark a specific session dead on the spot (e.g. to unblock git_safe.py's
    push guard without waiting out STALE_MINUTES or reaching for
    --override-session-guard). Silent on the hook path; never blocks.
    """
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
