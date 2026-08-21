"""Manual handover logging (normally invoked via the /handover skill). Read
helpers used by the SessionStart hook live in lib/handovers.py.
"""
import json

from lib.schema import get_conn, now


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
