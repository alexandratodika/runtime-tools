import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import runtime_state as state


def test_temp_directory(test):
    base = Path(os.environ.get("STATE_TEST_TMP", tempfile.gettempdir())).resolve()
    base.mkdir(parents=True, exist_ok=True)
    temp = tempfile.TemporaryDirectory(dir=base)
    def cleanup():
        target = Path(temp.name).resolve()
        if target.parent != base:
            raise ValueError("Unexpected test cleanup path")
        temp.cleanup()
    test.addCleanup(cleanup)
    return temp


def command(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(f"Command failed: {args[0]} {args[1:3]}\n{result.stderr}")
    return result.stdout.strip()


def database(path, value=0):
    with contextlib.closing(sqlite3.connect(path)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS totals (a INTEGER, b INTEGER)")
            db.execute("DELETE FROM totals")
            db.execute("INSERT INTO totals VALUES (?, ?)", (value, value))


def read_value(path):
    with contextlib.closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT a, b FROM totals").fetchone()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_temp_directory(self)
        self.root = Path(self.temp.name)

    def test_backup_includes_wal_without_touching_live_database(self):
        src, dst = self.root / "live.db", self.root / "saved.db"
        database(src)
        db = sqlite3.connect(src)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("UPDATE totals SET a=42, b=42")
        db.commit()
        before = src.read_bytes()
        self.assertTrue(Path(str(src) + "-wal").exists())
        state.snapshot_file(src, dst)
        self.assertEqual(read_value(dst), (42, 42))
        self.assertEqual(src.read_bytes(), before)
        self.assertTrue(Path(str(src) + "-wal").exists())

    def test_snapshot_is_consistent_while_transactions_continue(self):
        src, dst = self.root / "live.db", self.root / "saved.db"
        database(src)
        with contextlib.closing(sqlite3.connect(src)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        stop, ready = threading.Event(), threading.Event()
        errors = []

        def writer():
            db = sqlite3.connect(src, timeout=3)
            try:
                for n in range(1, 10000):
                    db.execute("UPDATE totals SET a=?", (n,))
                    time.sleep(0.0001)
                    db.execute("UPDATE totals SET b=?", (n,))
                    db.commit()
                    ready.set()
                    if stop.is_set():
                        break
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            state.snapshot_file(src, dst)
        finally:
            stop.set()
            thread.join(5)
        a, b = read_value(dst)
        self.assertEqual(a, b)
        self.assertGreater(a, 0)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())

    def test_corrupt_database_never_replaces_good_snapshot(self):
        src, dst = self.root / "live.db", self.root / "saved.db"
        src.write_bytes(b"broken sqlite file")
        database(dst, 7)
        with self.assertRaises(sqlite3.DatabaseError):
            state.snapshot_file(src, dst)
        self.assertEqual(read_value(dst), (7, 7))
        self.assertEqual(list(self.root.glob(".state-*")), [])

    def test_partial_json_never_replaces_good_snapshot(self):
        src, dst = self.root / "cache.json", self.root / "saved.json"
        src.write_text('{"products":', encoding="utf-8")
        dst.write_text('{"products": []}', encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            state.snapshot_file(src, dst)
        self.assertEqual(json.loads(dst.read_text()), {"products": []})

    def test_sessions_are_sqlite_snapshots(self):
        src, dst = self.root / "live.session", self.root / "saved.session"
        database(src, 5)
        state.snapshot_file(src, dst)
        self.assertEqual(read_value(dst), (5, 5))

    def test_backup_has_a_deadline_and_preserves_last_snapshot(self):
        src, dst = self.root / "live.db", self.root / "saved.db"
        database(src, 9)
        database(dst, 1)
        with patch.object(state.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaises(TimeoutError):
                state.snapshot_file(src, dst, timeout=1)
        self.assertEqual(read_value(dst), (1, 1))


class WorkflowTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Workflow shell syntax is checked on Linux CI")
    def test_workflow_shell_blocks_have_valid_bash_syntax(self):
        root = Path(__file__).resolve().parent.parent
        paths = list((root / ".github/workflows").glob("run-*.yml"))
        self.assertTrue(paths)
        for path in paths:
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if line == "        run: |":
                    block = []
                    for following in lines[index + 1:]:
                        if following and not following.startswith("          "):
                            break
                        block.append(following[10:])
                    result = subprocess.run(["bash", "-n"], input="\n".join(block) + "\n", text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, f"{path.name}: {result.stderr}")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_temp_directory(self)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.live = self.root / "live"
        self.sync = self.root / "sync"
        self.editor = self.root / "editor"
        command("git", "init", "--bare", "--initial-branch=main", str(self.remote))
        command("git", "clone", str(self.remote), str(self.live))
        self.configure(self.live)
        (self.live / "bot.py").write_text("version = 1\n", encoding="utf-8")
        (self.live / "cache.json").write_text('{"products": []}', encoding="utf-8")
        database(self.live / "shop_data.db")
        command("git", "add", ".", cwd=self.live)
        command("git", "commit", "-m", "Initial code and state", cwd=self.live)
        command("git", "push", "origin", "main", cwd=self.live)
        command("git", "clone", str(self.remote), str(self.sync))
        command("git", "clone", str(self.remote), str(self.editor))
        self.configure(self.sync)
        self.configure(self.editor)

    def configure(self, repo):
        command("git", "config", "user.name", "State Test", cwd=repo)
        command("git", "config", "user.email", "state-test@example.invalid", cwd=repo)
        command("git", "config", "core.autocrlf", "false", cwd=repo)

    def edit_code(self, version):
        (self.editor / "bot.py").write_text(f"version = {version}\n", encoding="utf-8")
        command("git", "add", "bot.py", cwd=self.editor)
        command("git", "commit", "-m", "Concurrent code change", cwd=self.editor)
        command("git", "push", "origin", "main", cwd=self.editor)

    def save(self, *patterns):
        with contextlib.redirect_stdout(io.StringIO()):
            state.save_state(self.live, self.sync, patterns)

    def pull_editor(self):
        command("git", "pull", "--ff-only", cwd=self.editor)

    def test_save_preserves_remote_code_and_does_not_change_runtime(self):
        database(self.live / "shop_data.db", 11)
        live_head = command("git", "rev-parse", "HEAD", cwd=self.live)
        self.edit_code(2)
        self.save("shop_data.db")
        self.pull_editor()
        self.assertEqual(read_value(self.editor / "shop_data.db"), (11, 11))
        self.assertEqual((self.editor / "bot.py").read_text(), "version = 2\n")
        self.assertEqual((self.live / "bot.py").read_text(), "version = 1\n")
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.live), live_head)
        self.assertEqual(command("git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", cwd=self.editor), "shop_data.db")

    def test_push_race_retries_without_losing_code_or_snapshot(self):
        database(self.live / "shop_data.db", 13)
        original_git = state.git
        pushes = []

        def race(repo, *args):
            if args[0] == "push":
                if not pushes:
                    self.edit_code(3)
                result = original_git(repo, *args)
                pushes.append(result.returncode)
                return result
            return original_git(repo, *args)

        with patch.object(state, "git", side_effect=race):
            self.save("shop_data.db")
        self.assertEqual(len(pushes), 2)
        self.assertNotEqual(pushes[0], 0)
        self.assertEqual(pushes[1], 0)
        self.pull_editor()
        self.assertEqual((self.editor / "bot.py").read_text(), "version = 3\n")
        self.assertEqual(read_value(self.editor / "shop_data.db"), (13, 13))

    def test_failed_snapshot_cannot_replace_newer_remote_database(self):
        database(self.editor / "shop_data.db", 99)
        command("git", "add", "shop_data.db", cwd=self.editor)
        command("git", "commit", "-m", "Newer remote state", cwd=self.editor)
        command("git", "push", "origin", "main", cwd=self.editor)
        (self.live / "shop_data.db").write_bytes(b"corrupt")
        (self.live / "cache.json").write_text('{"products": [1]}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.save("shop_data.db", "cache.json")
        self.pull_editor()
        self.assertEqual(read_value(self.editor / "shop_data.db"), (99, 99))
        self.assertEqual(json.loads((self.editor / "cache.json").read_text()), {"products": [1]})

    def test_unchanged_state_does_not_create_commits(self):
        self.save("shop_data.db", "cache.json")
        before = command("git", "rev-parse", "main", cwd=self.remote)
        self.save("shop_data.db", "cache.json")
        self.assertEqual(command("git", "rev-parse", "main", cwd=self.remote), before)

    def test_overlapping_final_save_waits_for_process_lock(self):
        script = "import sys; sys.path.insert(0, sys.argv[2]); from runtime_state import state_lock;\nwith state_lock(sys.argv[1], timeout=0.2): pass"
        def attempt():
            return subprocess.run([sys.executable, "-c", script, str(self.sync), str(Path(state.__file__).parent)], capture_output=True, timeout=10)
        with state.state_lock(self.sync):
            self.assertNotEqual(attempt().returncode, 0)
        self.assertEqual(attempt().returncode, 0)

    def test_rejects_live_checkout_as_backup_destination(self):
        with self.assertRaises(ValueError):
            state.save_state(self.live, self.live, ["shop_data.db"])

    def test_missing_files_are_not_created_or_deleted(self):
        head = command("git", "rev-parse", "HEAD", cwd=self.sync)
        self.save("not-present.session")
        self.assertFalse((self.live / "not-present.session").exists())
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.sync), head)


if __name__ == "__main__":
    unittest.main()
