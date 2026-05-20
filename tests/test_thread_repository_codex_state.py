from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "codex-cli-startup.py"
SPEC = importlib.util.spec_from_file_location("codex_cli_startup", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module: {MODULE_PATH}")
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


class ThreadRepositoryCodexStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.codex_home = Path(self._temp_dir.name)
        self.state_db = self.codex_home / "state_5.sqlite"
        self._create_state_db(self.state_db)
        self.repository = APP.ThreadRepository(self.state_db, self.codex_home)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _create_state_db(self, path: Path) -> None:
        connection = _connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER,
                    source TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sandbox_policy TEXT NOT NULL DEFAULT '',
                    approval_mode TEXT NOT NULL DEFAULT '',
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    archived_at INTEGER,
                    git_sha TEXT,
                    git_branch TEXT,
                    git_origin_url TEXT,
                    model TEXT,
                    reasoning_effort TEXT,
                    first_user_message TEXT,
                    cli_version TEXT
                );
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    child_thread_id TEXT NOT NULL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _create_logs_db(self, with_process_column: bool = True) -> None:
        connection = _connect(self.codex_home / APP.CODEX_LOGS_DB_FILENAME)
        try:
            if with_process_column:
                connection.executescript(
                    """
                    CREATE TABLE logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL,
                        ts_nanos INTEGER NOT NULL,
                        level TEXT NOT NULL,
                        target TEXT NOT NULL,
                        message TEXT,
                        thread_id TEXT,
                        process_uuid TEXT
                    );
                    """
                )
            else:
                connection.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, thread_id TEXT)")
            connection.commit()
        finally:
            connection.close()

    def _rollout_path(self, thread_id: str, archived: bool = False, day: str = "02", suffix: str = "") -> Path:
        name = f"rollout-2026-01-{day}T03-04-05{suffix}-{thread_id}.jsonl"
        if archived:
            return self.codex_home / "archived_sessions" / name
        return self.codex_home / "sessions" / "2026" / "01" / day / name

    def _write_rollout(self, thread_id: str, path: Path, meta_thread_id: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        session_meta = {
            "timestamp": "2026-01-02T03:04:05Z",
            "type": "session_meta",
            "payload": {
                "id": meta_thread_id or thread_id,
                "timestamp": "2026-01-02T03:04:05Z",
                "cwd": "G:/Project/example",
                "originator": "codex-cli",
                "cli_version": "test",
                "source": "cli",
                "model_provider": "openai",
            },
        }
        event = {
            "timestamp": "2026-01-02T03:04:06Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello", "kind": "plain"},
        }
        path.write_text(json.dumps(session_meta) + "\n" + json.dumps(event) + "\n", encoding="utf-8")

    def _make_old(self, path: Path) -> None:
        old_timestamp = time.time() - APP.ACTIVE_ROLLOUT_MTIME_GRACE_SECONDS - 10
        os.utime(path, (old_timestamp, old_timestamp))

    def _insert_thread(self, thread_id: str, rollout_path: Path, archived: bool, cwd: str = "G:/Project/example") -> None:
        connection = _connect(self.state_db)
        try:
            timestamp = int(rollout_path.stat().st_mtime)
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, created_at_ms, updated_at_ms,
                    source, model_provider, cwd, title, archived, archived_at,
                    model, reasoning_effort, first_user_message, cli_version
                )
                VALUES (?, ?, ?, ?, ?, ?, 'cli', 'openai', ?, 'Test thread', ?, ?, 'gpt-test', 'medium', 'hello', 'test')
                """,
                (
                    thread_id,
                    str(rollout_path),
                    timestamp,
                    timestamp,
                    timestamp * 1000,
                    timestamp * 1000,
                    cwd,
                    1 if archived else 0,
                    timestamp if archived else None,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _thread_row(self, thread_id: str) -> sqlite3.Row | None:
        connection = _connect(self.state_db)
        try:
            return connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        finally:
            connection.close()

    def test_archive_and_unarchive_keep_rollout_and_sqlite_in_sync(self) -> None:
        thread_id = "11111111-1111-1111-1111-111111111111"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._make_old(active_path)
        self._insert_thread(thread_id, active_path, archived=False)

        archived_path = self.repository.archive_thread(thread_id)
        archived_row = self._thread_row(thread_id)

        self.assertFalse(active_path.exists())
        self.assertTrue(archived_path.exists())
        self.assertEqual(archived_path.parent, self.codex_home / "archived_sessions")
        self.assertEqual(archived_row["archived"], 1)
        self.assertEqual(archived_row["rollout_path"], str(archived_path))
        self.assertIsNotNone(archived_row["updated_at_ms"])

        restored_path = self.repository.unarchive_thread(thread_id)
        restored_row = self._thread_row(thread_id)

        self.assertFalse(archived_path.exists())
        self.assertTrue(restored_path.exists())
        self.assertEqual(restored_path.parent, self.codex_home / "sessions" / "2026" / "01" / "02")
        self.assertEqual(restored_row["archived"], 0)
        self.assertIsNone(restored_row["archived_at"])
        self.assertEqual(restored_row["rollout_path"], str(restored_path))

    def test_delete_archived_thread_removes_row_edges_and_rollout(self) -> None:
        thread_id = "22222222-2222-2222-2222-222222222222"
        archived_path = self._rollout_path(thread_id, archived=True)
        self._write_rollout(thread_id, archived_path)
        self._insert_thread(thread_id, archived_path, archived=True)
        connection = _connect(self.state_db)
        try:
            connection.execute(
                "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id) VALUES (?, ?)",
                (thread_id, "child"),
            )
            connection.commit()
        finally:
            connection.close()

        self.repository.delete_archived_thread(thread_id)

        self.assertIsNone(self._thread_row(thread_id))
        self.assertFalse(archived_path.exists())
        connection = _connect(self.state_db)
        try:
            edge_count = connection.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(edge_count, 0)

    def test_delete_archived_threads_respects_workspace_scope_then_all_scope(self) -> None:
        first_id = "33333333-3333-3333-3333-333333333333"
        second_id = "44444444-4444-4444-4444-444444444444"
        first_path = self._rollout_path(first_id, archived=True)
        second_path = self._rollout_path(second_id, archived=True, day="03")
        self._write_rollout(first_id, first_path)
        self._write_rollout(second_id, second_path)
        self._insert_thread(first_id, first_path, archived=True, cwd="G:/Project/one")
        self._insert_thread(second_id, second_path, archived=True, cwd="G:/Project/two")

        deleted = self.repository.delete_archived_threads(
            "G:/Project/one",
            APP.THREAD_SCOPE_WORKSPACE,
            APP.THREAD_VIEW_CHATS,
            ["G:/Project/one", "G:/Project/two"],
        )
        self.assertEqual(deleted, 1)
        self.assertIsNone(self._thread_row(first_id))
        self.assertIsNotNone(self._thread_row(second_id))

        deleted = self.repository.delete_archived_threads(
            "G:/Project/one",
            APP.THREAD_SCOPE_ALL_WORKSPACES,
            APP.THREAD_VIEW_CHATS,
            ["G:/Project/one", "G:/Project/two"],
        )
        self.assertEqual(deleted, 1)
        self.assertIsNone(self._thread_row(second_id))

    def test_archive_guard_blocks_live_log_process(self) -> None:
        thread_id = "55555555-5555-5555-5555-555555555555"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._make_old(active_path)
        self._insert_thread(thread_id, active_path, archived=False)
        self._create_logs_db()
        connection = _connect(self.codex_home / APP.CODEX_LOGS_DB_FILENAME)
        try:
            connection.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id, process_uuid) VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), 0, "INFO", "test", thread_id, "pid:12345:test"),
            )
            connection.commit()
        finally:
            connection.close()
        self.repository._is_pid_running = lambda _pid: True

        with self.assertRaisesRegex(RuntimeError, "appears active"):
            self.repository.archive_thread(thread_id)
        self.assertTrue(active_path.exists())
        self.assertEqual(self._thread_row(thread_id)["archived"], 0)

    def test_archive_guard_blocks_recent_rollout_mtime(self) -> None:
        thread_id = "66666666-6666-6666-6666-666666666666"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._insert_thread(thread_id, active_path, archived=False)

        with self.assertRaisesRegex(RuntimeError, "appears active"):
            self.repository.archive_thread(thread_id)
        self.assertTrue(active_path.exists())

    def test_archive_guard_fails_closed_when_logs_schema_is_unknown(self) -> None:
        thread_id = "77777777-7777-7777-7777-777777777777"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._make_old(active_path)
        self._insert_thread(thread_id, active_path, archived=False)
        self._create_logs_db(with_process_column=False)

        with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
            self.repository.archive_thread(thread_id)
        self.assertTrue(active_path.exists())

    def test_thread_has_live_process_does_not_use_rollout_mtime(self) -> None:
        thread_id = "88888888-8888-8888-8888-888888888888"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._insert_thread(thread_id, active_path, archived=False)

        self.assertFalse(self.repository.thread_has_live_process(thread_id))

    def test_rollout_resolution_rejects_mismatched_outside_nested_and_ambiguous_paths(self) -> None:
        thread_id = "99999999-9999-9999-9999-999999999999"
        mismatched_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        mismatched_path = self._rollout_path(mismatched_id)
        self._write_rollout(thread_id, mismatched_path)
        with self.assertRaises(FileNotFoundError):
            self.repository._resolve_active_rollout_path(str(mismatched_path), thread_id)

        outside_path = Path(self._temp_dir.name).parent / f"rollout-2026-01-02T03-04-05-{thread_id}.jsonl"
        self._write_rollout(thread_id, outside_path)
        with self.assertRaises(FileNotFoundError):
            self.repository._resolve_active_rollout_path(str(outside_path), thread_id)
        outside_path.unlink(missing_ok=True)

        wrong_active_path = self.codex_home / "sessions" / "nested" / f"rollout-2026-01-02T03-04-05-{thread_id}.jsonl"
        self._write_rollout(thread_id, wrong_active_path)
        with self.assertRaises(FileNotFoundError):
            self.repository._resolve_active_rollout_path(str(wrong_active_path), thread_id)

        nested_archived_path = self.codex_home / "archived_sessions" / "nested" / f"rollout-2026-01-02T03-04-05-{thread_id}.jsonl"
        self._write_rollout(thread_id, nested_archived_path)
        with self.assertRaises(FileNotFoundError):
            self.repository._resolve_archived_rollout_path(str(nested_archived_path), thread_id)

        first_path = self._rollout_path(thread_id, day="04")
        second_path = self._rollout_path(thread_id, day="05", suffix="-alt")
        self._write_rollout(thread_id, first_path)
        self._write_rollout(thread_id, second_path)
        with self.assertRaisesRegex(RuntimeError, "Multiple rollout files"):
            self.repository._resolve_active_rollout_path("", thread_id)

    def test_schema_failure_happens_before_file_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            codex_home = Path(temp_text)
            state_db = codex_home / "state_5.sqlite"
            connection = _connect(state_db)
            try:
                connection.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        rollout_path TEXT NOT NULL,
                        updated_at INTEGER NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()
            repository = APP.ThreadRepository(state_db, codex_home)
            thread_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            active_path = codex_home / "sessions" / "2026" / "01" / "02" / f"rollout-2026-01-02T03-04-05-{thread_id}.jsonl"
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": thread_id}}) + "\n",
                encoding="utf-8",
            )
            old_timestamp = time.time() - APP.ACTIVE_ROLLOUT_MTIME_GRACE_SECONDS - 10
            os.utime(active_path, (old_timestamp, old_timestamp))
            connection = _connect(state_db)
            try:
                connection.execute(
                    "INSERT INTO threads (id, rollout_path, updated_at, archived) VALUES (?, ?, ?, 0)",
                    (thread_id, str(active_path), int(old_timestamp)),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(RuntimeError, "missing required columns"):
                repository.archive_thread(thread_id)
            self.assertTrue(active_path.exists())
            self.assertFalse((codex_home / "archived_sessions" / active_path.name).exists())


if __name__ == "__main__":
    unittest.main()
