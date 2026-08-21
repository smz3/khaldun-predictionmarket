"""Context-window watch: reads real token usage from the transcript and
fires a one-time soft/hard nudge to log a handover before auto-compact.
Wired to UserPromptSubmit and PostToolUse in settings.json (see db.py's
docstring for why both).
"""
import json
from pathlib import Path

from lib.schema import get_conn, now, read_stdin_json
from lib.sessions import touch_session

CONTEXT_SOFT = 100_000
CONTEXT_HARD = 145_000
CONTEXT_WINDOW = 200_000


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
        # systemMessage is shown directly to the user in the terminal
        # (independent of hookSpecificOutput, which only the model sees) -
        # without it the reminder was easy to miss, buried in hook JSON.
        print(json.dumps({
            "systemMessage": message,
            "hookSpecificOutput": {
                "hookEventName": hook_event_name,
                "additionalContext": message,
            }
        }))
    except Exception:
        return
