---
description: Wrap up this session - save uncommitted work, log a handover to state.db, confirm it's safe to close.
allowed-tools: Bash(python .claude/scripts/db.py log *), Bash(python .claude/scripts/git_safe.py *), Bash(git status)
---

Wrap up this session so the next session (or the next agent picking this repo back up) can pick up cleanly. Do these in order:

1. **Save uncommitted work.** Run `git status`. If there are changes worth keeping and it's safe to do so (not a mid-experiment broken state), commit and push them via `git_safe.py commit` and `git_safe.py push --yes --session <this session's id>` per the established auto-push policy. If it's unclear whether the state is safe to commit, say so and ask rather than guessing.

2. **Write the handover.** Based on your own read of this conversation, determine:
   - **Summary** - what actually happened/changed this session (code written, decisions made, commits/pushes done).
   - **Next steps** - concrete next actions for whoever picks this up next.
   - **Open questions** - anything unresolved that needs the user's input.

   Don't ask the user to restate anything you can already tell from the conversation. If nothing of substance happened this session, say that plainly instead of padding out a summary.

3. **Log it:**

   ```
   python .claude/scripts/db.py log --session <this session's id> --summary "..." --next "..." --questions "..."
   ```

4. **Confirm to the user**, in 1-2 sentences, that the handover is logged and it's safe to close this session now. Remind them that closing the session itself is still their action - you can't do it for them.
