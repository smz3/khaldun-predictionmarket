"""Manual task-management commands (task-add/task-status/task-retitle/
task-list). The PostToolUse task-remind nudge is a hook, not a manual
command - see hooks/task_remind.py. Shared validation/bloat-check helpers
live in lib/tasks.py (also used by hooks/sessions.py for the SessionStart
summary).
"""
import json
import sys

from lib.schema import get_conn, now
from lib.tasks import check_task_bloat, validate_task_title


def cmd_task_add(args):
    validate_task_title(args.title)
    conn = get_conn()
    ts = now()
    cur = conn.execute(
        "INSERT INTO tasks (status, priority, category, task_title, task_details, created_ts, updated_ts) "
        "VALUES ('open', ?, ?, ?, ?, ?, ?)",
        (args.priority, args.category, args.title, args.details or "", ts, ts),
    )
    conn.commit()
    task_id = cur.lastrowid
    bloat = check_task_bloat(conn)
    conn.close()
    result = {
        "id": task_id,
        "status": "open",
        "priority": args.priority,
        "category": args.category,
        "task_title": args.title,
    }
    if bloat:
        result["reminder"] = bloat
    print(json.dumps(result))


def cmd_task_status(args):
    conn = get_conn()
    row = conn.execute(
        "SELECT task_details, priority, category FROM tasks WHERE id = ?", (args.id,)
    ).fetchone()
    if row is None:
        print(json.dumps({"error": f"no task with id {args.id}"}))
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
        "UPDATE tasks SET status = ?, priority = ?, category = ?, task_details = ?, updated_ts = ? WHERE id = ?",
        (args.status, priority, category, details, now(), args.id),
    )
    conn.commit()
    bloat = check_task_bloat(conn)
    conn.close()
    result = {"id": args.id, "status": args.status, "priority": priority, "category": category}
    if bloat:
        result["reminder"] = bloat
    print(json.dumps(result))


def cmd_task_retitle(args):
    validate_task_title(args.title)
    conn = get_conn()
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(json.dumps({"error": f"no task with id {args.id}"}))
        conn.close()
        sys.exit(1)
    conn.execute(
        "UPDATE tasks SET task_title = ?, updated_ts = ? WHERE id = ?",
        (args.title, now(), args.id),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"id": args.id, "task_title": args.title}))


def cmd_task_list(args):
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
        f"FROM tasks WHERE status IN ({placeholders}){category_clause} "
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
