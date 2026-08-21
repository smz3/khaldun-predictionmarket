"""
Local-only backup history for state.db.

.claude/db-history/ is its own git repo (own .git, no remote) that
snapshots state.db over time. This is deliberately separate from this
project's repo, which never sees state.db at all (it's gitignored there -
see schema.py's DB_PATH docstring for why: binary blob, multi-session
writers, no meaningful diff). A dedicated repo with no remote gives real
point-in-time restore (not just "last backup overwrites the previous
one") while making it structurally impossible for a snapshot to leave
this machine - there's no origin configured, so even `git push` here
would be a no-op.

snapshot() uses sqlite3's own backup() API rather than a plain file copy,
so a concurrent writer (another live session) can't produce a torn/
corrupt snapshot.
"""
import subprocess
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "state.db"
HISTORY_DIR = Path(__file__).resolve().parents[2] / "db-history"
SNAPSHOT_PATH = HISTORY_DIR / "state.db"


def _git(*args, check=True):
    return subprocess.run(
        ["git", *args], cwd=HISTORY_DIR, check=check,
        capture_output=True, text=True,
    )


def _ensure_repo():
    HISTORY_DIR.mkdir(exist_ok=True)
    if not (HISTORY_DIR / ".git").exists():
        _git("init")
        _git("config", "user.email", "state-db-backup@localhost")
        _git("config", "user.name", "state-db-backup")


def snapshot(message="snapshot"):
    """Copy state.db into db-history/ and commit it there. Returns True if
    a new commit was made, False if there was nothing to snapshot (no
    state.db yet) or nothing changed since the last snapshot."""
    if not DB_PATH.exists():
        return False
    _ensure_repo()

    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(SNAPSHOT_PATH))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    _git("add", "state.db")
    result = _git("commit", "-m", message, check=False)
    return result.returncode == 0
