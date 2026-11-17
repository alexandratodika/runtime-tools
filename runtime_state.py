"""Snapshot runtime state without changing the checkout used by running bots.

Only the explicitly selected state files are committed, on top of the latest
remote branch. Failed snapshots retain the last good remote version.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time


@contextmanager
def state_lock(state_dir, timeout=180):
    """Serialize periodic/final saves, including overlapping child processes."""
    path = Path(state_dir) / ".git" / "runtime-state.lock"
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.write(b"0")
            lock.flush()
            def acquire():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(lock, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another state save is still running")
                time.sleep(0.1)
        try:
            yield
        finally:
            release()


def snapshot_file(source, destination, timeout=30):
    """Atomically replace a state file only after obtaining a valid snapshot."""
    source, destination = Path(source), Path(destination)
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=destination.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if source.suffix in (".db", ".session"):
            deadline = time.monotonic() + timeout

            def progress(status, remaining, total):
                if time.monotonic() >= deadline:
                    raise TimeoutError("SQLite backup deadline exceeded")

            # Read-only access includes committed WAL pages; never checkpoint or
            # copy the live database file as a fallback.
            src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst, pages=250, progress=progress, sleep=0.05)
                    if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise ValueError("Invalid SQLite snapshot")
                finally:
                    dst.close()
            finally:
                src.close()
        else:
            shutil.copyfile(source, tmp)
            if source.suffix == ".json":
                with tmp.open(encoding="utf-8") as stream:
                    json.load(stream)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def git(repo, *args):
    """Keep authenticated remote URLs and Git output out of Actions logs."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, timeout=60,
    )
    return result


def publish_state(repo, filenames, branch="main", attempts=3):
    """Rebuild the state-only commit on a fresh remote head if a push races."""
    for _ in range(attempts):
        if git(repo, "fetch", "origin", branch).returncode:
            raise RuntimeError("State fetch failed")
        # This is a separate checkout. Mixed reset updates the parent/index but
        # preserves the validated snapshots; no live bot files are touched.
        if git(repo, "reset", "--mixed", f"origin/{branch}").returncode:
            raise RuntimeError("State checkout update failed")
        if git(repo, "add", "-f", "--", *filenames).returncode:
            raise RuntimeError("State staging failed")
        diff = git(repo, "diff", "--cached", "--quiet")
        if diff.returncode == 0:
            return False
        if diff.returncode != 1:
            raise RuntimeError("State diff failed")
        if git(repo, "commit", "-m", "chore: save validated runtime state [skip ci]").returncode:
            raise RuntimeError("State commit failed")
        if git(repo, "push", "origin", f"HEAD:{branch}").returncode == 0:
            return True
    raise RuntimeError("State push failed after retries")


def save_state(source_dir, state_dir, patterns, branch="main"):
    source_dir, state_dir = Path(source_dir).resolve(), Path(state_dir).resolve()
    if source_dir == state_dir or source_dir in state_dir.parents or state_dir in source_dir.parents:
        raise ValueError("Runtime and state checkouts must be separate")
    if not (source_dir / ".git").is_dir() or not (state_dir / ".git").is_dir():
        raise ValueError("Both checkouts must already exist")
    failures = []
    with state_lock(state_dir):
        snapshots = []
        candidates = set()
        for pattern in patterns:
            if Path(pattern).name != pattern or pattern in (".", ".."):
                raise ValueError("Only top-level state file patterns are allowed")
            candidates.update(source_dir.glob(pattern))
        for path in sorted(candidates):
            if not path.is_file():
                continue
            if path.is_symlink():
                raise ValueError("State files must not be symlinks")
            try:
                snapshot_file(path, state_dir / path.name)
                snapshots.append(path.name)
            except Exception as exc:
                failures.append(path.name)
                # Report filenames and exception types, never DB contents or URLs.
                print(f"::warning::State snapshot failed: {path.name} ({type(exc).__name__})", flush=True)
        if snapshots:
            changed = publish_state(state_dir, snapshots, branch)
            print(f"State backup: {len(snapshots)} validated files; pushed={changed}", flush=True)
        if failures:
            raise RuntimeError(f"{len(failures)} state snapshots failed; last good remote files retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir")
    parser.add_argument("state_dir")
    parser.add_argument("patterns", nargs="+")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    try:
        save_state(args.source_dir, args.state_dir, args.patterns, args.branch)
    except Exception as exc:
        print(f"::error::State backup failed ({type(exc).__name__}); retry required", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
