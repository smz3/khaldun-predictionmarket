"""Tasks table CRUD: the source of truth for cross-session work items. See
db_schema.py for the table's shape/status-lifecycle/priority-levels.
"""
import json
import sys

from db_schema import get_conn, now

# Soft cap, not a hard block: once open+discussing tasks reach this count,
# task-add/task-status/SessionStart surface a reminder to close/reject/
# deprioritize before piling on more, instead of letting the list bloat
# silently.
TASK_BLOAT_THRESHOLD = 6
# Hard cap on task_title length (task-add and task-retitle both enforce it).
# Titles are meant to be scannable in the one-line SessionStart list
# (tasks_note) - a title that runs long is really a description that
# belongs in --details instead. Root cause of task #6: #3/#4/#5 had
# description-length titles because nothing stopped it.
TASK_TITLE_MAX_LEN = 60


def check_task_bloat(conn):
    """Soft-cap reminder, not a block: nudge once open+discussing tasks reach
    TASK_BLOAT_THRESHOLD, so the list gets triaged before it grows unreadable.
    """
    count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('open', 'discussing')"
    ).fetchone()[0]
    if count < TASK_BLOAT_THRESHOLD:
        return None
    return (
        f"{count} open/discussing tasks - at or above the soft limit of "
        f"{TASK_BLOAT_THRESHOLD}. Not a hard block, but close, reject, or "
        f"deprioritize some before adding more."
    )


def tasks_note(conn):
    """Every task still open or discussing, for SessionStart - so an item can't
    silently drop off just because a later handover's prose didn't repeat it.
    Sorted priority-first, then by id.
    """
    rows = conn.execute(
        "SELECT id, status, priority, category, task_title FROM tasks "
        "WHERE status IN ('open', 'discussing') ORDER BY priority, id"
    ).fetchall()
    if not rows:
        return "No open or discussing tasks."
    lines = "\n".join(
        f"  #{tid} [{status}] P{priority} [{category}] {title}"
        for tid, status, priority, category, title in rows
    )
    note = f"{len(rows)} open/discussing task(s):\n{lines}"
    bloat = check_task_bloat(conn)
    if bloat:
        note = f"[REMINDER] {bloat}\n{note}"
    return note


def validate_task_title(title):
    if len(title) > TASK_TITLE_MAX_LEN:
        print(json.dumps({
            "error": (
                f"--title is {len(title)} chars, max is {TASK_TITLE_MAX_LEN}. "
                f"Keep the title short and put the rest in --details."
            )
        }))
        sys.exit(1)


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


def cmd_task_remind(_args):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "[TASK SYNC] If any item just written to the TodoWrite list is a "
                "durable, cross-session work item (not just a step for the task "
                "in front of you), mirror it into the persistent backlog now: "
                "python .claude/scripts/db.py task-add --title T --category "
                "infra|app --priority 1-4 [--details D]"
            ),
        }
    }))
