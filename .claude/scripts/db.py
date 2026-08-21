"""
CLI entrypoint for state.db (SQLite, gitignored) - session continuity for
Claude Code hooks/skills in this repo. Stdlib only, no deps.

This file (and git_safe.py, its sibling) stays at the top of scripts/ on
purpose - settings.json and the /handover skill reference both by this
fixed path, so moving them buys nothing but broken references. Everything
else is organized by who calls it:
  hooks/   - handlers only ever invoked by a Claude Code hook (settings.json)
  cli/     - commands only ever invoked manually (task-add, log, ...)
  lib/     - shared helpers with no command of their own (schema, session
             heartbeat plumbing, task/handover read helpers) - imported by
             both hooks/ and cli/, and by git_safe.py's push guard.
Table shapes and lifecycle: lib/schema.py. This file only wires argparse to
the handlers above. get_conn and session_activity_note are re-exported here
because git_safe.py does `import db as state_db` and calls
`state_db.get_conn()` / `state_db.session_activity_note(...)` directly.

Subcommands:
  init                                        create db/table if missing
  session-start                                stdin: hook JSON (session_id).
                                                Prints SessionStart
                                                hookSpecificOutput JSON with
                                                undelivered handovers (see
                                                HANDOVER_SHOW_COUNT in
                                                lib/handovers.py).
  stop-check                                   stdin: hook JSON (session_id).
                                                Just bumps this session's
                                                heartbeat in `sessions`.
                                                Never blocks, never logs
                                                anything else - handover
                                                logging is purely on-command
                                                (see `log` below), by design
                                                there is no automatic
                                                safety-net for an abandoned
                                                session.
  context-check                                stdin: hook JSON (session_id,
                                                transcript_path). Wired to BOTH
                                                UserPromptSubmit and PostToolUse
                                                - a single long turn can run many
                                                tool calls with no new user
                                                prompt in between, and
                                                UserPromptSubmit alone would
                                                never get a chance to run until
                                                that turn finally ended, letting
                                                usage blow straight past 145k
                                                unnoticed (observed: not caught
                                                until 195k). PostToolUse re-runs
                                                the same check after every tool
                                                call so growth mid-turn gets
                                                seen too. Also bumps the session
                                                heartbeat on every call before
                                                the token check, so that side
                                                effect still happens even when
                                                token usage can't be read. Reads
                                                the real token usage of the last
                                                assistant turn from the
                                                transcript; once per session,
                                                prints a soft (100k) then a
                                                hard (145k) handover-now nudge.
                                                Never blocks; silent on any
                                                error. See hooks/context.py.
  task-remind                                  PostToolUse hook, matcher
                                                "TodoWrite" (see settings.json,
                                                "TodoWrite" is Claude Code's
                                                own built-in tool name, not
                                                ours to rename). Fires every
                                                time the built-in TodoWrite
                                                tool is used and injects a
                                                static reminder to mirror any
                                                durable, cross-session items
                                                into this table via task-add -
                                                TodoWrite itself is per-
                                                session/ephemeral and cannot
                                                supply the required priority/
                                                category, so this cannot be
                                                fully automatic; it's a nudge,
                                                not a sync. Always fires,
                                                never dedups - cheap and the
                                                repetition on multi-call turns
                                                is an accepted tradeoff over
                                                building dedup logic for it.
  session-end [--session ID]                   No --session: stdin hook JSON
                                                (session_id) - SessionEnd
                                                hook, marks that session's row
                                                dead immediately. Confirmed to
                                                fire on explicit exit, /clear,
                                                and logout; NOT confirmed to
                                                fire when an IDE tab is
                                                abandoned by jumping straight
                                                to a new session - that gap is
                                                covered by reap_stale_sessions
                                                instead. With --session ID:
                                                manual escape hatch - mark a
                                                specific session dead on the
                                                spot (e.g. to unblock
                                                git_safe.py's push guard
                                                without waiting out
                                                STALE_MINUTES or reaching for
                                                --override-session-guard).
                                                Silent on the hook path; never
                                                blocks.
  log --session ID --summary S --next N [--questions Q]
                                                Insert a handover row. The
                                                only way a handover ever gets
                                                written - normally invoked
                                                via the /handover skill.
  db-snapshot                                  Backup command, not a hook -
                                                copies state.db into
                                                db-history/ (a separate
                                                local-only git repo, no
                                                remote - see
                                                lib/db_history.py) and
                                                commits it there. Run
                                                manually whenever you want a
                                                restore point.
  prune [--days N] [--vacuum]                  Delete context_watch rows and
                                                dead sessions rows older than
                                                N days (default 30).
                                                handovers are never deleted.
                                                Manual only - not wired to a
                                                hook.
  task-add --title T --category infra|app       Insert a task, status='open'.
    --priority 1-4 [--details D]                Priority is required, same
                                                 as category. --title must be
                                                 <= TASK_TITLE_MAX_LEN chars
                                                 (rejected with an error
                                                 otherwise - move the extra
                                                 text into --details instead).
                                                 Prints its id (+ a bloat
                                                 reminder once open+discussing
                                                 reaches TASK_BLOAT_THRESHOLD).
  task-status --id N --status S [--note N]      Update a task's status
    [--priority 1-4] [--category infra|app]     ('open'|'discussing'|
                                                 'rejected'|'closed'). --note
                                                 is appended (timestamped) to
                                                 task_details, not a
                                                 replacement. Omit --priority/
                                                 --category to leave unchanged.
  task-retitle --id N --title T                 Replace a task's title in
                                                 place (same TASK_TITLE_MAX_LEN
                                                 check as task-add). The only
                                                 way to fix a bad title after
                                                 creation - task-status never
                                                 touches task_title.
  task-list [--status S1,S2,...]               Print tasks as JSON, sorted
    [--category infra|app]                     priority-first. Default
                                                status filter: open,discussing.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone

from cli.db_history import cmd_db_snapshot
from cli.handovers import cmd_log
from cli.tasks import cmd_task_add, cmd_task_list, cmd_task_retitle, cmd_task_status
from hooks.context import cmd_context_check
from hooks.sessions import cmd_session_end, cmd_session_start, cmd_stop_check
from hooks.task_remind import cmd_task_remind
from lib.schema import get_conn
from lib.sessions import session_activity_note


def cmd_init(_args):
    get_conn().close()


def cmd_prune(args):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    conn = get_conn()
    deleted = {}
    cur = conn.execute("DELETE FROM context_watch WHERE ts < ?", (cutoff,))
    deleted["context_watch"] = cur.rowcount
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

    sub.add_parser("task-remind").set_defaults(func=cmd_task_remind)
    sub.add_parser("db-snapshot").set_defaults(func=cmd_db_snapshot)

    session_end_p = sub.add_parser("session-end")
    session_end_p.add_argument(
        "--session", default=None,
        help="mark this session_id dead directly - manual escape hatch for "
             "when SessionEnd didn't fire and you know for a fact the "
             "session is closed. Omit to use the hook's stdin JSON instead.",
    )
    session_end_p.set_defaults(func=cmd_session_end)

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

    task_add_p = sub.add_parser("task-add")
    task_add_p.add_argument("--title", required=True)
    task_add_p.add_argument("--details", default="")
    task_add_p.add_argument("--category", required=True, choices=["infra", "app"])
    task_add_p.add_argument("--priority", type=int, required=True, choices=[1, 2, 3, 4])
    task_add_p.set_defaults(func=cmd_task_add)

    task_status_p = sub.add_parser("task-status")
    task_status_p.add_argument("--id", type=int, required=True)
    task_status_p.add_argument(
        "--status", required=True, choices=["open", "discussing", "rejected", "closed"]
    )
    task_status_p.add_argument("--note", default="")
    task_status_p.add_argument(
        "--priority", type=int, choices=[1, 2, 3, 4], default=None,
        help="omit to leave unchanged",
    )
    task_status_p.add_argument(
        "--category", choices=["infra", "app"], default=None, help="omit to leave unchanged"
    )
    task_status_p.set_defaults(func=cmd_task_status)

    task_retitle_p = sub.add_parser("task-retitle")
    task_retitle_p.add_argument("--id", type=int, required=True)
    task_retitle_p.add_argument("--title", required=True)
    task_retitle_p.set_defaults(func=cmd_task_retitle)

    task_list_p = sub.add_parser("task-list")
    task_list_p.add_argument("--status", default="")
    task_list_p.add_argument("--category", choices=["infra", "app"], default=None)
    task_list_p.set_defaults(func=cmd_task_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
