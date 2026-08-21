"""Tasks table helpers shared by hooks/sessions.py (tasks_note, for the
SessionStart summary) and cli/tasks.py (validation + bloat check). See
lib/schema.py for the tasks table's shape/status-lifecycle/priority-levels.
"""
import json
import sys

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
