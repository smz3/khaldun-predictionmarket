"""PostToolUse hook, matcher "TodoWrite" (see settings.json - "TodoWrite" is
Claude Code's own built-in tool name, not ours to rename). Fires every time
the built-in TodoWrite tool is used and injects a static reminder to mirror
any durable, cross-session items into the tasks table via task-add -
TodoWrite itself is per-session/ephemeral and cannot supply the required
priority/category, so this cannot be fully automatic; it's a nudge, not a
sync. Always fires, never dedups - cheap and the repetition on multi-call
turns is an accepted tradeoff over building dedup logic for it.
"""
import json


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
