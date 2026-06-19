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
from unittest import mock

try:
    import zstandard as zstd
except ImportError:
    zstd = None


MODULE_PATH = Path(__file__).resolve().parents[1] / "codex-cli-startup.py"
SPEC = importlib.util.spec_from_file_location("codex_cli_startup", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module: {MODULE_PATH}")
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)
REAL_APP_SERVER_CLIENT = APP.AppServerThreadClient


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


class _FailingAppServerClient:
    def __init__(self, _codex_home_path: Path) -> None:
        pass

    def list_threads(self, _archived_only: bool) -> list[dict[str, object]]:
        raise RuntimeError("app-server unavailable")

    def archive_thread(self, _thread_id: str) -> None:
        raise RuntimeError("app-server unavailable")

    def unarchive_thread(self, _thread_id: str) -> dict[str, object]:
        raise RuntimeError("app-server unavailable")

    def delete_thread(self, _thread_id: str) -> None:
        raise RuntimeError("app-server unavailable")


class _StateBackedAppServerClient:
    def __init__(self, codex_home_path: Path) -> None:
        self._codex_home_path = Path(codex_home_path)
        self._state_db_path = self._codex_home_path / "state_5.sqlite"

    def list_threads(self, archived_only: bool) -> list[dict[str, object]]:
        connection = _connect(self._state_db_path)
        try:
            parent_by_child = {
                str(row["child_thread_id"]): str(row["parent_thread_id"])
                for row in connection.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges")
                if row["child_thread_id"]
            }
            rows = connection.execute(
                """
                SELECT id, rollout_path, created_at_ms, updated_at_ms, cwd, source, thread_source,
                       first_user_message, model_provider, cli_version, git_branch, git_sha, title
                FROM threads
                WHERE archived = ?
                ORDER BY updated_at_ms DESC, id DESC
                """,
                (1 if archived_only else 0,),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "id": str(row["id"]),
                "parentThreadId": parent_by_child.get(str(row["id"])),
                "preview": str(row["first_user_message"] or ""),
                "modelProvider": str(row["model_provider"] or ""),
                "createdAt": row["created_at_ms"],
                "updatedAt": row["updated_at_ms"],
                "path": str(row["rollout_path"] or ""),
                "cwd": str(row["cwd"] or ""),
                "cliVersion": str(row["cli_version"] or ""),
                "source": str(row["source"] or ""),
                "threadSource": str(row["thread_source"] or ""),
                "gitInfo": {
                    "branch": str(row["git_branch"] or ""),
                    "sha": str(row["git_sha"] or ""),
                    "originUrl": None,
                },
                "name": str(row["title"] or ""),
            }
            for row in rows
        ]

    def archive_thread(self, thread_id: str) -> None:
        repository = APP.ThreadRepository(self._state_db_path, self._codex_home_path)
        connection = _connect(self._state_db_path)
        moved_paths: list[tuple[Path, Path]] = []
        try:
            columns = repository._require_thread_write_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            thread_ids = repository._thread_ids_with_spawn_descendants(connection, [thread_id])
            rows_by_id = repository._thread_rows_by_id(connection, thread_ids)
            root_row = rows_by_id.get(thread_id)
            if root_row is None:
                raise RuntimeError(f"Thread was not found: {thread_id}")
            if bool(int(root_row["archived"] or 0)):
                raise RuntimeError("The selected thread is already archived.")
            archived_at = int(time.time())
            for current_id in thread_ids:
                row = rows_by_id.get(current_id)
                if row is None or bool(int(row["archived"] or 0)):
                    continue
                active_path = repository._resolve_active_rollout_path(str(row["rollout_path"] or ""), current_id)
                archived_path = repository._archive_active_rollout(active_path)
                moved_paths.append((archived_path, active_path))
                repository._update_thread_archive_state(connection, columns, current_id, archived_path, True, archived_at)
            connection.commit()
        except Exception:
            connection.rollback()
            for archived_path, active_path in reversed(moved_paths):
                repository._move_rollout_back(archived_path, active_path)
            raise
        finally:
            connection.close()

    def unarchive_thread(self, thread_id: str) -> dict[str, object]:
        repository = APP.ThreadRepository(self._state_db_path, self._codex_home_path)
        connection = _connect(self._state_db_path)
        restored_path: Path | None = None
        archived_path: Path | None = None
        try:
            columns = repository._require_thread_write_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, rollout_path, archived FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Thread was not found: {thread_id}")
            if not bool(int(row["archived"] or 0)):
                raise RuntimeError("The selected thread is not archived.")
            archived_path = repository._resolve_archived_rollout_path(str(row["rollout_path"] or ""), thread_id)
            restored_path = repository._restore_archived_rollout(archived_path)
            repository._update_thread_archive_state(connection, columns, thread_id, restored_path, False, None)
            connection.commit()
            return {"id": thread_id, "path": str(restored_path)}
        except Exception:
            connection.rollback()
            if restored_path is not None and archived_path is not None:
                repository._move_rollout_back(restored_path, archived_path)
            raise
        finally:
            connection.close()

    def delete_thread(self, thread_id: str) -> None:
        repository = APP.ThreadRepository(self._state_db_path, self._codex_home_path)
        repository._delete_archived_thread_ids([thread_id])


class _FakeStdin:
    def __init__(self) -> None:
        self.input_text = ""
        self.closed = False

    def write(self, text: str) -> int:
        self.input_text += text
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)
        self._index = 0

    def readline(self) -> str:
        if self._index >= len(self._lines):
            return ""
        line = self._lines[self._index]
        self._index += 1
        return line


class _FakeAppServerProcess:
    def __init__(self, stdout_text: str, returncode: int = 0, stderr_text: str = "") -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(stdout_text)
        self.stderr = _FakeStdout(stderr_text)
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.killed = False
        self.terminated = False

    @property
    def input_text(self) -> str:
        return self.stdin.input_text

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: int | None = None) -> int:
        self.returncode = self._wait_returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class ThreadRepositoryCodexStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.codex_home = Path(self._temp_dir.name)
        self.state_db = self.codex_home / "state_5.sqlite"
        self._create_state_db(self.state_db)
        self._app_server_patch = mock.patch.object(APP, "AppServerThreadClient", _StateBackedAppServerClient)
        self._app_server_patch.start()
        self.repository = APP.ThreadRepository(self.state_db, self.codex_home)

    def tearDown(self) -> None:
        self._app_server_patch.stop()
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
                    cli_version TEXT,
                    thread_source TEXT
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

    def _rollout_path(
        self,
        thread_id: str,
        archived: bool = False,
        day: str = "02",
        suffix: str = "",
        compressed: bool = False,
    ) -> Path:
        extension = ".jsonl.zst" if compressed else ".jsonl"
        name = f"rollout-2026-01-{day}T03-04-05{suffix}-{thread_id}{extension}"
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
        content = json.dumps(session_meta) + "\n" + json.dumps(event) + "\n"
        if path.name.lower().endswith(".jsonl.zst"):
            if zstd is None:
                self.skipTest("zstandard is not installed")
            path.write_bytes(zstd.ZstdCompressor().compress(content.encode("utf-8")))
            return
        path.write_text(content, encoding="utf-8")

    def _make_old(self, path: Path) -> None:
        old_timestamp = time.time() - APP.ACTIVE_ROLLOUT_MTIME_GRACE_SECONDS - 10
        os.utime(path, (old_timestamp, old_timestamp))

    def _insert_thread(
        self,
        thread_id: str,
        rollout_path: Path,
        archived: bool,
        cwd: str = "G:/Project/example",
        source: str = "cli",
        thread_source: str | None = None,
    ) -> None:
        connection = _connect(self.state_db)
        try:
            timestamp = int(rollout_path.stat().st_mtime)
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, created_at_ms, updated_at_ms,
                    source, model_provider, cwd, title, archived, archived_at,
                    model, reasoning_effort, first_user_message, cli_version, thread_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'openai', ?, 'Test thread', ?, ?, 'gpt-test', 'medium', 'hello', 'test', ?)
                """,
                (
                    thread_id,
                    str(rollout_path),
                    timestamp,
                    timestamp,
                    timestamp * 1000,
                    timestamp * 1000,
                    source,
                    cwd,
                    1 if archived else 0,
                    timestamp if archived else None,
                    thread_source,
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

    def _insert_spawn_edge(self, parent_thread_id: str, child_thread_id: str) -> None:
        connection = _connect(self.state_db)
        try:
            connection.execute(
                "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id) VALUES (?, ?)",
                (parent_thread_id, child_thread_id),
            )
            connection.commit()
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

    def test_archive_thread_cascades_to_spawned_descendants(self) -> None:
        parent_id = "12121212-1212-1212-1212-121212121212"
        child_id = "13131313-1313-1313-1313-131313131313"
        parent_path = self._rollout_path(parent_id)
        child_path = self._rollout_path(child_id, day="03")
        self._write_rollout(parent_id, parent_path)
        self._write_rollout(child_id, child_path)
        self._make_old(parent_path)
        self._make_old(child_path)
        self._insert_thread(parent_id, parent_path, archived=False)
        self._insert_thread(child_id, child_path, archived=False, source="subagent_thread_spawn")
        self._insert_spawn_edge(parent_id, child_id)

        archived_parent_path = self.repository.archive_thread(parent_id)
        archived_child_path = self.codex_home / "archived_sessions" / child_path.name

        self.assertEqual(archived_parent_path, self.codex_home / "archived_sessions" / parent_path.name)
        self.assertFalse(parent_path.exists())
        self.assertFalse(child_path.exists())
        self.assertTrue(archived_parent_path.exists())
        self.assertTrue(archived_child_path.exists())
        self.assertEqual(self._thread_row(parent_id)["archived"], 1)
        self.assertEqual(self._thread_row(child_id)["archived"], 1)

    def test_archive_thread_prefers_app_server_before_local_active_guard(self) -> None:
        thread_id = "14141414-1414-1414-1414-141414141414"
        active_path = self._rollout_path(thread_id)
        archived_path = self.codex_home / "archived_sessions" / active_path.name
        self._write_rollout(thread_id, active_path)
        self._insert_thread(thread_id, active_path, archived=False)
        calls: list[str] = []
        state_db = self.state_db

        class ArchivingAppServerClient:
            def __init__(self, _codex_home_path: Path) -> None:
                pass

            def archive_thread(self, current_thread_id: str) -> None:
                calls.append(current_thread_id)
                archived_path.parent.mkdir(parents=True, exist_ok=True)
                APP.shutil.move(str(active_path), str(archived_path))
                connection = _connect(state_db)
                try:
                    connection.execute(
                        "UPDATE threads SET archived = 1, archived_at = ?, rollout_path = ? WHERE id = ?",
                        (int(time.time()), str(archived_path), current_thread_id),
                    )
                    connection.commit()
                finally:
                    connection.close()

        with mock.patch.object(APP, "AppServerThreadClient", ArchivingAppServerClient):
            returned_path = self.repository.archive_thread(thread_id)

        self.assertEqual(calls, [thread_id])
        self.assertEqual(returned_path, archived_path)
        self.assertFalse(active_path.exists())
        self.assertTrue(archived_path.exists())
        self.assertEqual(self._thread_row(thread_id)["archived"], 1)

    def test_unarchive_thread_prefers_app_server(self) -> None:
        thread_id = "15151515-1515-1515-1515-151515151515"
        archived_path = self._rollout_path(thread_id, archived=True)
        restored_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, archived_path)
        self._insert_thread(thread_id, archived_path, archived=True)
        calls: list[str] = []
        state_db = self.state_db

        class UnarchivingAppServerClient:
            def __init__(self, _codex_home_path: Path) -> None:
                pass

            def unarchive_thread(self, current_thread_id: str) -> dict[str, object]:
                calls.append(current_thread_id)
                restored_path.parent.mkdir(parents=True, exist_ok=True)
                APP.shutil.move(str(archived_path), str(restored_path))
                connection = _connect(state_db)
                try:
                    connection.execute(
                        "UPDATE threads SET archived = 0, archived_at = NULL, rollout_path = ? WHERE id = ?",
                        (str(restored_path), current_thread_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
                return {"id": current_thread_id, "path": str(restored_path)}

        with mock.patch.object(APP, "AppServerThreadClient", UnarchivingAppServerClient):
            returned_path = self.repository.unarchive_thread(thread_id)

        self.assertEqual(calls, [thread_id])
        self.assertEqual(returned_path, restored_path)
        self.assertFalse(archived_path.exists())
        self.assertTrue(restored_path.exists())
        self.assertEqual(self._thread_row(thread_id)["archived"], 0)

    def test_archive_thread_surfaces_unsupported_without_local_mutation(self) -> None:
        thread_id = "16161616-1616-1616-1616-161616161616"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._make_old(active_path)
        self._insert_thread(thread_id, active_path, archived=False)
        process = _FakeAppServerProcess(
            "\n".join(
                [
                    json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}),
                    json.dumps({"id": 2, "error": {"code": -32601, "message": "Method not found"}}),
                ]
            )
            + "\n"
        )

        with mock.patch.object(APP, "AppServerThreadClient", REAL_APP_SERVER_CLIENT):
            with mock.patch.object(APP.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "does not support"):
                    self.repository.archive_thread(thread_id)

        self.assertTrue(active_path.exists())
        self.assertEqual(self._thread_row(thread_id)["archived"], 0)

    def test_archive_thread_surfaces_app_server_invalid_request(self) -> None:
        thread_id = "17171717-1717-1717-1717-171717171717"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._make_old(active_path)
        self._insert_thread(thread_id, active_path, archived=False)
        process = _FakeAppServerProcess(
            "\n".join(
                [
                    json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}),
                    json.dumps({"id": 2, "error": {"code": -32602, "message": "invalid params"}}),
                ]
            )
            + "\n"
        )

        with mock.patch.object(APP, "AppServerThreadClient", REAL_APP_SERVER_CLIENT):
            with mock.patch.object(APP.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "invalid params"):
                    self.repository.archive_thread(thread_id)

        self.assertTrue(active_path.exists())
        self.assertEqual(self._thread_row(thread_id)["archived"], 0)

    def test_delete_archived_thread_removes_row_edges_and_rollout(self) -> None:
        thread_id = "22222222-2222-2222-2222-222222222222"
        child_id = "23232323-2323-2323-2323-232323232323"
        archived_path = self._rollout_path(thread_id, archived=True)
        child_archived_path = self._rollout_path(child_id, archived=True, day="03")
        self._write_rollout(thread_id, archived_path)
        self._write_rollout(child_id, child_archived_path)
        self._insert_thread(thread_id, archived_path, archived=True)
        self._insert_thread(child_id, child_archived_path, archived=True, source="subagent_thread_spawn")
        self._insert_spawn_edge(thread_id, child_id)

        self.repository.delete_archived_thread(thread_id)

        self.assertIsNone(self._thread_row(thread_id))
        self.assertIsNone(self._thread_row(child_id))
        self.assertFalse(archived_path.exists())
        self.assertFalse(child_archived_path.exists())
        connection = _connect(self.state_db)
        try:
            edge_count = connection.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(edge_count, 0)

    def test_delete_archived_thread_surfaces_app_server_invalid_request(self) -> None:
        thread_id = "24242424-2424-2424-2424-242424242424"
        archived_path = self._rollout_path(thread_id, archived=True)
        self._write_rollout(thread_id, archived_path)
        self._insert_thread(thread_id, archived_path, archived=True)
        process = _FakeAppServerProcess(
            "\n".join(
                [
                    json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}),
                    json.dumps({"id": 2, "error": {"code": -32602, "message": "invalid params"}}),
                ]
            )
            + "\n"
        )

        with mock.patch.object(APP, "AppServerThreadClient", REAL_APP_SERVER_CLIENT):
            with mock.patch.object(APP.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "invalid params"):
                    self.repository.delete_archived_thread(thread_id)

        self.assertIsNotNone(self._thread_row(thread_id))
        self.assertTrue(archived_path.exists())

    def test_delete_archived_threads_uses_app_server_and_returns_root_count(self) -> None:
        parent_id = "25252525-2525-2525-2525-252525252525"
        child_id = "26262626-2626-2626-2626-262626262626"
        parent_path = self._rollout_path(parent_id, archived=True)
        child_path = self._rollout_path(child_id, archived=True, day="03")
        self._write_rollout(parent_id, parent_path)
        self._write_rollout(child_id, child_path)
        self._insert_thread(parent_id, parent_path, archived=True, cwd="G:/Project/one")
        self._insert_thread(child_id, child_path, archived=True, cwd="G:/Project/one", source="subagent_thread_spawn")
        self._insert_spawn_edge(parent_id, child_id)
        calls: list[str] = []
        state_db = self.state_db

        class DeletingAppServerClient:
            def __init__(self, _codex_home_path: Path) -> None:
                pass

            def list_threads(self, _archived_only: bool) -> list[dict[str, object]]:
                return [
                    {
                        "id": parent_id,
                        "parentThreadId": None,
                        "preview": "hello",
                        "modelProvider": "openai",
                        "createdAt": 1760000000,
                        "updatedAt": 1760000200,
                        "path": str(parent_path),
                        "cwd": "G:/Project/one",
                        "cliVersion": "test",
                        "source": "cli",
                        "threadSource": "user",
                        "gitInfo": None,
                        "name": "Parent",
                    },
                    {
                        "id": child_id,
                        "parentThreadId": parent_id,
                        "preview": "child",
                        "modelProvider": "openai",
                        "createdAt": 1760000000,
                        "updatedAt": 1760000100,
                        "path": str(child_path),
                        "cwd": "G:/Project/one",
                        "cliVersion": "test",
                        "source": "subagent_thread_spawn",
                        "threadSource": "thread_spawn",
                        "gitInfo": None,
                        "name": "Child",
                    },
                ]

            def delete_thread(self, current_thread_id: str) -> None:
                calls.append(current_thread_id)
                connection = _connect(state_db)
                try:
                    rows = connection.execute(
                        "SELECT id, rollout_path FROM threads WHERE id IN (?, ?)",
                        (current_thread_id, child_id),
                    ).fetchall()
                    connection.execute(
                        "DELETE FROM thread_spawn_edges WHERE parent_thread_id = ? OR child_thread_id = ?",
                        (current_thread_id, child_id),
                    )
                    connection.execute("DELETE FROM threads WHERE id IN (?, ?)", (current_thread_id, child_id))
                    connection.commit()
                finally:
                    connection.close()
                for row in rows:
                    Path(str(row["rollout_path"])).unlink(missing_ok=True)

        with mock.patch.object(APP, "AppServerThreadClient", DeletingAppServerClient):
            deleted = self.repository.delete_archived_threads(
                "G:/Project/one",
                APP.THREAD_SCOPE_WORKSPACE,
                APP.THREAD_VIEW_CHATS,
                ["G:/Project/one"],
            )

        self.assertEqual(calls, [parent_id])
        self.assertEqual(deleted, 1)
        self.assertIsNone(self._thread_row(parent_id))
        self.assertIsNone(self._thread_row(child_id))
        self.assertFalse(parent_path.exists())
        self.assertFalse(child_path.exists())

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

    def test_bulk_delete_surfaces_app_server_error_after_prior_root_delete(self) -> None:
        parent_id = "34343434-3434-3434-3434-343434343434"
        child_id = "35353535-3535-3535-3535-353535353535"
        sibling_id = "36363636-3636-3636-3636-363636363636"
        parent_path = self._rollout_path(parent_id, archived=True)
        child_path = self._rollout_path(child_id, day="03")
        sibling_path = self._rollout_path(sibling_id, archived=True, day="04")
        self._write_rollout(parent_id, parent_path)
        self._write_rollout(child_id, child_path)
        self._write_rollout(sibling_id, sibling_path)
        self._insert_thread(parent_id, parent_path, archived=True, cwd="G:/Project/one")
        self._insert_thread(child_id, child_path, archived=False, cwd="G:/Project/one", source="subagent_thread_spawn")
        self._insert_thread(sibling_id, sibling_path, archived=True, cwd="G:/Project/one")
        self._insert_spawn_edge(parent_id, child_id)

        with self.assertRaisesRegex(RuntimeError, "Only archived threads can be deleted"):
            self.repository.delete_archived_threads(
                "G:/Project/one",
                APP.THREAD_SCOPE_WORKSPACE,
                APP.THREAD_VIEW_CHATS,
                ["G:/Project/one"],
            )

        self.assertIsNotNone(self._thread_row(parent_id))
        self.assertIsNotNone(self._thread_row(child_id))
        self.assertIsNone(self._thread_row(sibling_id))
        self.assertTrue(parent_path.exists())
        self.assertTrue(child_path.exists())
        self.assertFalse(sibling_path.exists())

    def test_orphan_subagents_are_counted_but_not_bulk_deleted(self) -> None:
        visible_id = "38383838-3838-3838-3838-383838383838"
        orphan_id = "39393939-3939-3939-3939-393939393939"
        child_id = "3a3a3a3a-3a3a-3a3a-3a3a-3a3a3a3a3a3a"
        visible_path = self._rollout_path(visible_id, archived=True)
        orphan_path = self._rollout_path(orphan_id, archived=True, day="03")
        child_path = self._rollout_path(child_id, archived=True, day="04")
        self._write_rollout(visible_id, visible_path)
        self._write_rollout(orphan_id, orphan_path)
        self._write_rollout(child_id, child_path)
        self._insert_thread(visible_id, visible_path, archived=True, cwd="G:/Project/one")
        self._insert_thread(orphan_id, orphan_path, archived=True, cwd="G:/Project/one", source="subagent_thread_spawn")
        self._insert_thread(child_id, child_path, archived=True, cwd="G:/Project/one", source="subagent_thread_spawn")
        self._insert_spawn_edge(visible_id, child_id)

        orphan_count = self.repository.count_orphan_subagent_threads(
            "G:/Project/one",
            True,
            APP.THREAD_SCOPE_WORKSPACE,
            ["G:/Project/one"],
        )
        deleted = self.repository.delete_archived_threads(
            "G:/Project/one",
            APP.THREAD_SCOPE_WORKSPACE,
            APP.THREAD_VIEW_CHATS,
            ["G:/Project/one"],
        )

        self.assertEqual(orphan_count, 1)
        self.assertEqual(deleted, 1)
        self.assertIsNone(self._thread_row(visible_id))
        self.assertIsNone(self._thread_row(child_id))
        self.assertIsNotNone(self._thread_row(orphan_id))
        self.assertFalse(visible_path.exists())
        self.assertFalse(child_path.exists())
        self.assertTrue(orphan_path.exists())

    def test_load_threads_filters_to_codex_interactive_source_kinds(self) -> None:
        source_cases = [
            ("a0000000-0000-0000-0000-000000000001", "cli", True),
            ("a0000000-0000-0000-0000-000000000002", "vscode", True),
            ("a0000000-0000-0000-0000-000000000003", "atlas", False),
            ("a0000000-0000-0000-0000-000000000004", "chatgpt", False),
            ("a0000000-0000-0000-0000-000000000005", "{\"custom\":\"atlas\"}", False),
            ("a0000000-0000-0000-0000-000000000006", "exec", False),
            ("a0000000-0000-0000-0000-000000000007", "mcp", False),
            ("a0000000-0000-0000-0000-000000000008", "codex", False),
            ("a0000000-0000-0000-0000-000000000009", "{\"subagent\":\"review\"}", False),
            ("a0000000-0000-0000-0000-00000000000a", "{\"internal\":\"memory_consolidation\"}", False),
            ("a0000000-0000-0000-0000-00000000000b", "unknown", False),
            ("a0000000-0000-0000-0000-00000000000c", "future_variant", False),
        ]
        for index, (thread_id, source, _expected) in enumerate(source_cases, start=1):
            path = self._rollout_path(thread_id, day=f"{index:02d}")
            self._write_rollout(thread_id, path)
            self._insert_thread(thread_id, path, archived=False, source=source)

        records = self.repository.load_threads(
            "G:/Project/example",
            False,
            APP.THREAD_SCOPE_WORKSPACE,
            APP.THREAD_VIEW_CHATS,
        )

        expected_ids = {thread_id for thread_id, _source, expected in source_cases if expected}
        self.assertEqual({record.thread_id for record in records}, expected_ids)
        self.assertEqual({record.source for record in records}, {"CLI", "VSCode"})
        self.assertEqual(APP._display_source("exec"), "Exec")
        self.assertEqual(APP._display_source("mcp"), "AppServer")
        self.assertEqual(APP._display_source("subagent_thread_spawn"), "SubAgentThreadSpawn")
        self.assertEqual(APP._display_source("{\"subagent\":\"review\"}"), "SubAgentReview")
        self.assertEqual(APP._display_source("cli", "user"), "CLI")
        self.assertEqual(APP._display_source("cli", "thread_spawn"), "SubAgentThreadSpawn")
        self.assertEqual(APP._display_source("future_variant"), "Future_variant")

    def test_app_server_client_uses_newline_json_rpc_handshake_and_thread_list(self) -> None:
        thread_id = "c0000000-0000-0000-0000-000000000001"
        process = _FakeAppServerProcess(
            "\n".join(
                [
                    json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}),
                    json.dumps({"method": "warning", "params": {"message": "ignored"}}),
                    json.dumps({"id": 2, "result": {"data": [{"id": thread_id, "preview": "hello"}]}}),
                ]
            )
            + "\n"
        )

        with mock.patch.object(APP.subprocess, "Popen", return_value=process) as popen_mock:
            threads = REAL_APP_SERVER_CLIENT(self.codex_home).list_threads(False)

        self.assertEqual(threads, [{"id": thread_id, "preview": "hello"}])
        popen_mock.assert_called_once()
        self.assertEqual(popen_mock.call_args.args[0][1:], ["app-server", "--stdio"])
        self.assertEqual(popen_mock.call_args.kwargs["env"]["CODEX_HOME"], str(self.codex_home))
        request_lines = [json.loads(line) for line in process.input_text.splitlines()]
        self.assertEqual([line.get("method") for line in request_lines], ["initialize", "initialized", "thread/list"])
        self.assertEqual(request_lines[0]["params"]["clientInfo"]["name"], "codex-cli-startup")
        self.assertIsNone(request_lines[0]["params"]["capabilities"])
        self.assertEqual(request_lines[2]["params"]["archived"], False)
        self.assertEqual(request_lines[2]["params"]["sourceKinds"], ["cli", "vscode"])
        self.assertTrue(request_lines[2]["params"]["useStateDbOnly"])

    def test_app_server_client_sends_thread_lifecycle_requests(self) -> None:
        thread_id = "c0000000-0000-0000-0000-000000000002"
        cases = [
            ("archive_thread", "thread/archive", {}),
            ("unarchive_thread", "thread/unarchive", {"thread": {"id": thread_id, "path": "restored.jsonl"}}),
            ("delete_thread", "thread/delete", {}),
        ]
        for method_name, app_server_method, result in cases:
            with self.subTest(method_name=method_name):
                process = _FakeAppServerProcess(
                    "\n".join(
                        [
                            json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}),
                            json.dumps({"id": 2, "result": result}),
                        ]
                    )
                    + "\n"
                )

                with mock.patch.object(APP.subprocess, "Popen", return_value=process):
                    returned = getattr(REAL_APP_SERVER_CLIENT(self.codex_home), method_name)(thread_id)

                request_lines = [json.loads(line) for line in process.input_text.splitlines()]
                self.assertEqual([line.get("method") for line in request_lines], ["initialize", "initialized", app_server_method])
                self.assertEqual(request_lines[2]["params"], {"threadId": thread_id})
                if method_name == "unarchive_thread":
                    self.assertEqual(returned, result["thread"])

    def test_app_server_client_resolves_codex_command_for_subprocess(self) -> None:
        with mock.patch.object(APP.shutil, "which", side_effect=[None, "C:/Users/me/AppData/Roaming/npm/codex.cmd"]):
            resolved = REAL_APP_SERVER_CLIENT(self.codex_home)._resolve_codex_executable()

        self.assertEqual(resolved, "C:/Users/me/AppData/Roaming/npm/codex.cmd")

    def test_load_threads_maps_app_server_threads_and_filters_locally(self) -> None:
        visible_id = "c1000000-0000-0000-0000-000000000001"
        child_id = "c1000000-0000-0000-0000-000000000002"
        other_workspace_id = "c1000000-0000-0000-0000-000000000003"
        rollout_path = self._rollout_path(visible_id)
        self._write_rollout(visible_id, rollout_path)
        app_server_threads = [
            {
                "id": visible_id,
                "parentThreadId": None,
                "preview": "first user message",
                "modelProvider": "openai",
                "createdAt": 1760000000,
                "updatedAt": 1760000100,
                "path": str(rollout_path),
                "cwd": "G:/Project/example",
                "cliVersion": "codex-test",
                "source": "cli",
                "threadSource": "user",
                "gitInfo": {"branch": "main", "sha": "abc123", "originUrl": None},
                "name": "App server title",
            },
            {
                "id": child_id,
                "parentThreadId": visible_id,
                "preview": "hidden child",
                "modelProvider": "openai",
                "createdAt": 1760000000,
                "updatedAt": 1760000200,
                "path": None,
                "cwd": "G:/Project/example",
                "cliVersion": "codex-test",
                "source": "cli",
                "threadSource": "thread_spawn",
                "gitInfo": None,
                "name": "Hidden child",
            },
            {
                "id": other_workspace_id,
                "parentThreadId": None,
                "preview": "other workspace",
                "modelProvider": "openai",
                "createdAt": 1760000000,
                "updatedAt": 1760000300,
                "path": None,
                "cwd": "G:/Project/other",
                "cliVersion": "codex-test",
                "source": "cli",
                "threadSource": "user",
                "gitInfo": None,
                "name": "Other",
            },
        ]

        class StaticAppServerClient:
            def __init__(self, _codex_home_path: Path) -> None:
                pass

            def list_threads(self, _archived_only: bool) -> list[dict[str, object]]:
                return app_server_threads

        with mock.patch.object(APP, "AppServerThreadClient", StaticAppServerClient):
            records = self.repository.load_threads(
                "G:/Project/example",
                False,
                APP.THREAD_SCOPE_WORKSPACE,
                APP.THREAD_VIEW_CHATS,
            )

        self.assertEqual([record.thread_id for record in records], [visible_id])
        self.assertEqual(records[0].title, "App server title")
        self.assertEqual(records[0].summary, "first user message")
        self.assertEqual(records[0].source, "CLI")
        self.assertEqual(records[0].model_provider, "openai")
        self.assertEqual(records[0].cli_version, "codex-test")
        self.assertEqual(records[0].git_branch, "main")
        self.assertEqual(records[0].git_sha, "abc123")
        self.assertFalse(records[0].rollout_missing)

    def test_load_threads_surfaces_app_server_decode_failure(self) -> None:
        thread_id = "c2000000-0000-0000-0000-000000000001"
        path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, path)
        self._insert_thread(thread_id, path, archived=False)
        process = _FakeAppServerProcess("not-json\n")

        with mock.patch.object(APP, "AppServerThreadClient", REAL_APP_SERVER_CLIENT):
            with mock.patch.object(APP.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                    self.repository.load_threads(
                        "G:/Project/example",
                        False,
                        APP.THREAD_SCOPE_WORKSPACE,
                        APP.THREAD_VIEW_CHATS,
                    )

        self.assertIsNotNone(self._thread_row(thread_id))
        self.assertTrue(path.exists())

    def test_thread_source_column_hides_subagents_even_when_session_source_is_cli(self) -> None:
        user_id = "b0000000-0000-0000-0000-000000000001"
        subagent_id = "b0000000-0000-0000-0000-000000000002"
        user_path = self._rollout_path(user_id, day="01")
        subagent_path = self._rollout_path(subagent_id, day="02")
        self._write_rollout(user_id, user_path)
        self._write_rollout(subagent_id, subagent_path)
        self._insert_thread(user_id, user_path, archived=False, source="cli", thread_source="user")
        self._insert_thread(subagent_id, subagent_path, archived=False, source="cli", thread_source="thread_spawn")

        records = self.repository.load_threads(
            "G:/Project/example",
            False,
            APP.THREAD_SCOPE_WORKSPACE,
            APP.THREAD_VIEW_CHATS,
        )

        self.assertEqual([record.thread_id for record in records], [user_id])
        self.assertEqual(records[0].source, "CLI")

    def test_load_threads_uses_rollout_index_for_jsonl_zst_stale_paths(self) -> None:
        thread_id = "37373737-3737-3737-3737-373737373737"
        compressed_path = self._rollout_path(thread_id, compressed=True)
        self._write_rollout(thread_id, compressed_path)
        self._insert_thread(thread_id, compressed_path, archived=False)
        stale_path = self.codex_home / "sessions" / "2026" / "01" / "02" / f"missing-{thread_id}.jsonl"
        connection = _connect(self.state_db)
        try:
            connection.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (str(stale_path), thread_id))
            connection.commit()
        finally:
            connection.close()

        records = self.repository.load_threads(
            "G:/Project/example",
            False,
            APP.THREAD_SCOPE_WORKSPACE,
            APP.THREAD_VIEW_CHATS,
        )

        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].rollout_missing)

    def test_jsonl_zst_rollout_requires_matching_session_meta(self) -> None:
        thread_id = "3b3b3b3b-3b3b-3b3b-3b3b-3b3b3b3b3b3b"
        mismatched_id = "3c3c3c3c-3c3c-3c3c-3c3c-3c3c3c3c3c3c"
        compressed_path = self._rollout_path(thread_id, compressed=True)
        self._write_rollout(thread_id, compressed_path, meta_thread_id=mismatched_id)
        self._insert_thread(thread_id, compressed_path, archived=False)

        records = self.repository.load_threads(
            "G:/Project/example",
            False,
            APP.THREAD_SCOPE_WORKSPACE,
            APP.THREAD_VIEW_CHATS,
        )

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].rollout_missing)

    def test_archive_thread_surfaces_app_server_error_without_live_process_guard(self) -> None:
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

        with mock.patch.object(APP, "AppServerThreadClient", _FailingAppServerClient):
            with self.assertRaisesRegex(RuntimeError, "app-server unavailable"):
                self.repository.archive_thread(thread_id)
        self.assertTrue(active_path.exists())
        self.assertEqual(self._thread_row(thread_id)["archived"], 0)

    def test_archive_thread_surfaces_app_server_error_without_recent_rollout_guard(self) -> None:
        thread_id = "66666666-6666-6666-6666-666666666666"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._insert_thread(thread_id, active_path, archived=False)

        with mock.patch.object(APP, "AppServerThreadClient", _FailingAppServerClient):
            with self.assertRaisesRegex(RuntimeError, "app-server unavailable"):
                self.repository.archive_thread(thread_id)
        self.assertTrue(active_path.exists())

    def test_archive_thread_surfaces_app_server_error_without_logs_schema_guard(self) -> None:
        thread_id = "77777777-7777-7777-7777-777777777777"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._make_old(active_path)
        self._insert_thread(thread_id, active_path, archived=False)
        self._create_logs_db(with_process_column=False)

        with mock.patch.object(APP, "AppServerThreadClient", _FailingAppServerClient):
            with self.assertRaisesRegex(RuntimeError, "app-server unavailable"):
                self.repository.archive_thread(thread_id)
        self.assertTrue(active_path.exists())

    def test_thread_has_live_process_does_not_use_rollout_mtime(self) -> None:
        thread_id = "88888888-8888-8888-8888-888888888888"
        active_path = self._rollout_path(thread_id)
        self._write_rollout(thread_id, active_path)
        self._insert_thread(thread_id, active_path, archived=False)

        self.assertFalse(self.repository.thread_has_live_process(thread_id))

    def test_find_command_source_uses_launch_shell_without_direct_path_shortcut(self) -> None:
        completed = APP.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="C:/Users/test/AppData/Roaming/npm/codex.ps1\n",
            stderr="",
        )
        with mock.patch.object(APP.shutil, "which", return_value="C:/outer/codex.exe") as which_mock:
            with mock.patch.object(APP.subprocess, "run", return_value=completed) as run_mock:
                source = APP.MainWindow._find_command_source(object(), "codex", "C:/Program Files/PowerShell/7/pwsh.exe")

        self.assertEqual(source, "C:/Users/test/AppData/Roaming/npm/codex.ps1")
        which_mock.assert_not_called()
        self.assertEqual(run_mock.call_args.args[0][0], "C:/Program Files/PowerShell/7/pwsh.exe")

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

            with mock.patch.object(APP, "AppServerThreadClient", _FailingAppServerClient):
                with self.assertRaisesRegex(RuntimeError, "app-server unavailable"):
                    repository.archive_thread(thread_id)
            self.assertTrue(active_path.exists())
            self.assertFalse((codex_home / "archived_sessions" / active_path.name).exists())


if __name__ == "__main__":
    unittest.main()
