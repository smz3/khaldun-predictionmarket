"""
Manual CLI command for lib/db_history.py's snapshot mechanism.
"""
from lib.db_history import snapshot


def cmd_db_snapshot(_args):
    if snapshot():
        print("snapshot committed")
    else:
        print("nothing to snapshot (no state.db, or unchanged since last snapshot)")
