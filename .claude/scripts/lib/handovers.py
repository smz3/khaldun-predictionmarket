"""Handover read helpers shared with hooks/sessions.py's SessionStart
handler. Writing a handover (cmd_log) is CLI-only - see cli/handovers.py.
See lib/schema.py for the handovers table's shape/lifecycle.
"""

# SessionStart shows at most this many distinct sessions' latest undelivered
# handover (current + 1 previous) - not a time window. Combined with the
# delivered flag, a handover is shown once (to whichever session starts
# next) and then never replayed again.
HANDOVER_SHOW_COUNT = 2


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
