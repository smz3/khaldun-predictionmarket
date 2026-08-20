"""Session lifecycle: heartbeat tracking, staleness reaping, the
SessionStart/stop-check/session-end hook handlers, and the "other active
sessions" note used by both SessionStart and git_safe.py's push guard. See
db_schema.py for the sessions table's shape.
"""
import json
import random
import subprocess
from datetime import datetime, timedelta, timezone

from db_handovers import format_handovers, recent_handovers
from db_schema import get_conn, now, read_stdin_json
from db_tasks import tasks_note

STALE_MINUTES = 5
# Safe at 5 (down from 30) because touch_session now also runs on PostToolUse
# (see cmd_context_check in db_context.py) - heartbeat has tool-call
# granularity, not just once-per-turn, so a long-running turn keeps
# refreshing last_seen_ts instead of going quiet until Stop. Was 30
# specifically to tolerate the old once-per-turn cadence; a real dead
# session sitting 'live' (and blocking git_safe.py pushes) for up to 30min
# was the actual cost of that, confirmed on session 6fcf78ff (happy-falcon)
# 2026-08-20.
# A session whose last_seen_ts still equals its started_ts (i.e. it never
# ticked past its first heartbeat) is only "possibly active" for this long -
# past this, one frozen heartbeat is a stronger signal of an abandoned
# session (e.g. closed by jumping straight to a new session) than of a
# session still working its first turn.
LIKELY_DEAD_GRACE_MINUTES = 3
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
    sessions accumulate a heartbeat tick roughly once per turn or tool call; a
    session stuck on exactly one heartbeat for several minutes was almost
    always abandoned (e.g. closed by jumping straight to a new session), not
    left mid-turn.
    """
    # Reap first: this is the only path git_safe.py's push guard reads
    # (it calls this function directly, never touch_session()), so without
    # reaping here a dead session's row can read 'live' forever regardless
    # of wall-clock time, until some unrelated session's heartbeat happens
    # to reap it first. Root cause of task #3's failed live-test.
    reap_stale_sessions(conn)
    conn.commit()
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
