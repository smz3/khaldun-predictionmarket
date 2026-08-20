"""
Safe git commit/push helper. Stdlib only (subprocess, fnmatch), no deps.
Never runs with shell=True and never exposes a --force flag anywhere.

Subcommands:
  commit --message M     Stage changed files (skipping filenames that look
                          like secrets - .env, *.pem, *.key, credentials*,
                          etc), print the staged diffstat, then commit.
                          Refuses if a merge/rebase is in progress or if
                          unresolved conflict markers exist. No-op (no
                          commit made) if nothing safe remains to stage.

  push [--yes]            Push the current branch to its upstream (creating
       [--session ID]     one with -u origin <branch> if none exists).
                          Without --yes: dry run only, prints what would
                          happen and exits without pushing. Refuses to push
                          if a merge/rebase is in progress, if the local
                          branch is behind its upstream (pull/rebase first),
                          if any secret-looking filename (see SECRET_GLOBS)
                          is present in the commits about to be pushed - this
                          check is unconditional, --yes does not bypass it -
                          or - when --session is given - if pushing to
                          main/master while another session's heartbeat is
                          fresh in state.db (see db.py session_activity_note).
                          Without --session, that last check degrades to a
                          warning instead of a block, since self vs. other
                          can't be told apart.
"""
import argparse
import fnmatch
import os
import subprocess
import sys

SECRET_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
    "*credentials*.json", "*secret*", "*.p12", "*.pfx",
]


def run_git_full(args, timeout=30):
    try:
        out = subprocess.run(
            ["git"] + args, capture_output=True, text=True, timeout=timeout
        )
        return out.returncode, out.stdout, out.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def run_git(args, timeout=30):
    return run_git_full(args, timeout)[1]


def git_dir():
    return run_git(["rev-parse", "--git-dir"]).strip()


def is_merge_or_rebase_in_progress():
    gd = git_dir()
    if not gd:
        return False
    return (
        os.path.exists(os.path.join(gd, "MERGE_HEAD"))
        or os.path.exists(os.path.join(gd, "rebase-merge"))
        or os.path.exists(os.path.join(gd, "rebase-apply"))
    )


def has_conflicts():
    return bool(run_git(["diff", "--name-only", "--diff-filter=U"]).strip())


def get_changed_files():
    out = run_git(["status", "--porcelain=v1"])
    paths = []
    for line in out.splitlines():
        if not line:
            continue
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip('"'))
    return paths


def is_secret_path(path):
    base = os.path.basename(path).lower()
    return any(fnmatch.fnmatch(base, pat) for pat in SECRET_GLOBS)


def find_secret_files(paths):
    return [p for p in paths if p and is_secret_path(p)]


def cmd_commit(args):
    if is_merge_or_rebase_in_progress():
        print("Refusing to commit: merge or rebase in progress. Resolve it first.")
        sys.exit(1)

    if has_conflicts():
        print("Refusing to commit: unresolved merge conflicts present.")
        sys.exit(1)

    changed = get_changed_files()
    if not changed:
        print("Nothing to commit.")
        return

    staged, skipped = [], []
    for path in changed:
        (skipped if is_secret_path(path) else staged).append(path)

    if skipped:
        print("Skipped (looks like a secret - not staged):")
        for p in skipped:
            print(f"  {p}")

    if not staged:
        print("Nothing safe to stage. Commit aborted.")
        return

    for p in staged:
        rc, _, err = run_git_full(["add", "--", p])
        if rc != 0:
            print(f"Failed to stage {p}: {err}")
            sys.exit(rc)

    diffstat = run_git(["diff", "--cached", "--stat"])
    if not diffstat.strip():
        print("Nothing staged after filtering. Commit aborted.")
        return
    print("Staging:")
    print(diffstat)

    rc, out, err = run_git_full(["commit", "-m", args.message])
    print(out or err)
    if rc != 0:
        print("Commit failed.")
        sys.exit(rc)
    print("Committed.")


def cmd_push(args):
    if is_merge_or_rebase_in_progress():
        print("Refusing to push: merge or rebase in progress.")
        sys.exit(1)

    branch = run_git(["branch", "--show-current"]).strip()
    if not branch:
        print("Refusing to push: detached HEAD.")
        sys.exit(1)

    up_rc, upstream, _ = run_git_full(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    has_upstream = up_rc == 0 and upstream.strip()

    behind = ahead = 0
    if has_upstream:
        counts = run_git(["rev-list", "--left-right", "--count", "@{u}...HEAD"]).strip()
        parts = counts.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
        if behind > 0:
            print(
                f"Refusing to push: local branch is {behind} commit(s) behind "
                f"{upstream.strip()}. Pull/rebase first."
            )
            sys.exit(1)

    if has_upstream:
        changed_paths = run_git(["diff", "--name-only", "@{u}..HEAD"]).splitlines()
    else:
        changed_paths = run_git(["ls-tree", "-r", "--name-only", "HEAD"]).splitlines()
    secret_hits = find_secret_files(changed_paths)
    if secret_hits:
        print("Refusing to push: secret-looking file(s) present in commits about to be pushed:")
        for p in secret_hits:
            print(f"  {p}")
        print(
            "Remove/untrack these (git rm --cached <path>, commit, and add them to "
            ".gitignore) before pushing. This check cannot be bypassed with --yes."
        )
        sys.exit(1)

    warnings = []
    if branch in ("main", "master"):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import db as state_db

        conn = state_db.get_conn()
        note = state_db.session_activity_note(conn, args.session)
        conn.close()
        if args.session:
            if not note.startswith("No other sessions"):
                print(f"Refusing to push to {branch}: {note}")
                sys.exit(1)
        else:
            warnings.append(
                f"[warn] pushing to {branch} without --session - cannot tell "
                f"self apart from others. {note}"
            )

    print(f"Branch: {branch}")
    print(f"Upstream: {upstream.strip() if has_upstream else '(none - will set with -u origin ' + branch + ')'}")
    if has_upstream:
        print(f"Ahead: {ahead}, Behind: {behind}")
    for w in warnings:
        print(w)

    if not args.yes:
        print("Dry run - nothing pushed. Re-run with --yes to actually push.")
        return

    push_args = ["push"] if has_upstream else ["push", "-u", "origin", branch]
    rc, out, err = run_git_full(push_args)
    print(out or err)
    if rc != 0:
        print("Push failed.")
        sys.exit(rc)
    print("Pushed.")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    commit_p = sub.add_parser("commit")
    commit_p.add_argument("--message", required=True)
    commit_p.set_defaults(func=cmd_commit)

    push_p = sub.add_parser("push")
    push_p.add_argument("--yes", action="store_true")
    push_p.add_argument("--session", default="")
    push_p.set_defaults(func=cmd_push)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
