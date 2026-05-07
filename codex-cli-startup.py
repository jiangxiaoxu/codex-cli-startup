from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QCloseEvent, QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def resolve_app_dir() -> Path:
    """Resolve the directory used for app-adjacent config files.

    @param None.
    @returns The executable directory when frozen, otherwise the source directory.
    """

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_resource_path(relative_path: str) -> Path:
    """Resolve a source or PyInstaller-bundled resource path.

    @param relative_path: Project-relative resource path.
    @returns: Absolute resource path.
    """

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


SCRIPT_DIR = resolve_app_dir()
APP_ICON_PATH = resolve_resource_path("assets/codex-cli-startup.ico")
APP_USER_MODEL_ID = "codex-cli-startup.app"
CONFIG_FILENAME = "codex-cli-startup_config.json"
CONFIG_PATH = SCRIPT_DIR / CONFIG_FILENAME
DEFAULT_WINDOW_SIZE = (1280, 780)
DEFAULT_SPLITTER_SIZES = (320, 960)
DEFAULT_COLUMN_WIDTHS = (520, 165, 100)
ROLLOUT_FILE_PATTERN = re.compile(r"^rollout-(\d{4})-(\d{2})-(\d{2})T.+-[0-9a-fA-F-]+\.jsonl$")
PROCESS_UUID_PATTERN = re.compile(r"^pid:(\d+):.+$")
CODEX_LOGS_DB_FILENAME = "logs_2.sqlite"
ACTIVE_ROLLOUT_MTIME_GRACE_SECONDS = 90
ACTIVE_THREAD_ARCHIVE_MESSAGE = "Thread appears active. Exit the Codex session before archiving."
THREAD_SCOPE_WORKSPACE = "workspace"
THREAD_SCOPE_ALL_WORKSPACES = "all_workspaces"
THREAD_VIEW_CHATS = "chats"
ALL_WORKSPACES_SELECTION = "__all_workspaces__"
INTERACTIVE_CHAT_SOURCES = {"cli", "vscode", "codex", "atlas", "chatgpt"}


def set_windows_app_user_model_id(app_id: str) -> None:
    """Set the Windows taskbar identity for the current process.

    @param app_id: Stable Windows AppUserModelID value.
    @returns: None.
    """

    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        return


@dataclass(slots=True)
class WorkspaceEntry:
    name: str
    path: str


@dataclass(slots=True)
class UiState:
    selected_workspace: str = ""
    show_archived: bool = False
    show_subagents: bool = False
    thread_scope: str = THREAD_SCOPE_WORKSPACE
    thread_view: str = THREAD_VIEW_CHATS
    window_size: tuple[int, int] = DEFAULT_WINDOW_SIZE
    splitter_sizes: tuple[int, int] = DEFAULT_SPLITTER_SIZES
    column_widths: tuple[int, ...] = DEFAULT_COLUMN_WIDTHS


@dataclass(slots=True)
class AppConfig:
    workspaces: list[WorkspaceEntry] = field(default_factory=list)
    terminal: str = "wt"
    ui_state: UiState = field(default_factory=UiState)


@dataclass(slots=True)
class ThreadRecord:
    thread_id: str
    title: str
    updated_at_text: str
    source: str
    cwd: str
    model: str
    rollout_path: str
    first_user_message: str
    summary: str
    archived: bool
    sort_timestamp: int


def normalize_workspace_path(path_text: str) -> str:
    """Return a normalized workspace path for comparisons.

    @param path_text The path string to normalize.
    @returns A normalized absolute path string.
    """

    normalized_input = path_text.strip()
    if not normalized_input:
        return ""
    if normalized_input.startswith("\\\\?\\UNC\\"):
        normalized_input = "\\" + normalized_input[7:]
    elif normalized_input.startswith("\\\\?\\"):
        normalized_input = normalized_input[4:]

    absolute_path = os.path.abspath(normalized_input)
    return os.path.normcase(os.path.normpath(absolute_path))


def strip_windows_verbatim_prefix(path_text: str) -> str:
    """Return a Windows path without the verbatim prefix.

    @param path_text The path string to normalize for display or shell use.
    @returns The path without a leading Windows verbatim prefix.
    """

    normalized_input = path_text.strip()
    if normalized_input.startswith("\\\\?\\UNC\\"):
        return "\\\\" + normalized_input[8:]
    if normalized_input.startswith("\\\\?\\"):
        return normalized_input[4:]
    return normalized_input


def resolve_codex_state_db_path() -> Path:
    """Resolve the Codex state database path from environment-aware locations.

    @param None.
    @returns The resolved state database path.
    """

    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "state_5.sqlite"

    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        return Path(user_profile).expanduser() / ".codex" / "state_5.sqlite"

    return Path.home() / ".codex" / "state_5.sqlite"


def resolve_codex_home_path() -> Path:
    """Resolve the Codex home path from environment-aware locations.

    @param None.
    @returns The resolved Codex home directory path.
    """

    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser()

    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        return Path(user_profile).expanduser() / ".codex"

    return Path.home() / ".codex"


def resolve_codex_logs_db_path(codex_home_path: Path) -> Path:
    """Resolve the Codex logs database path from a Codex home directory.

    @param codex_home_path The resolved Codex home directory path.
    @returns The resolved logs database path.
    """

    return codex_home_path / CODEX_LOGS_DB_FILENAME


def truncate_text(text: str, limit: int) -> str:
    """Collapse whitespace and truncate long text.

    @param text The source text to shorten.
    @param limit The maximum output length.
    @returns A single-line truncated string.
    """

    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: max(0, limit - 3)]}..."


def format_timestamp(timestamp_value: object) -> tuple[int, str]:
    """Format a thread timestamp defensively.

    @param timestamp_value The raw timestamp from SQLite.
    @returns A tuple of sortable timestamp and display text.
    """

    if timestamp_value is None:
        return 0, ""

    try:
        raw_timestamp = int(timestamp_value)
    except (TypeError, ValueError):
        return 0, str(timestamp_value)

    if raw_timestamp > 10_000_000_000:
        raw_timestamp = raw_timestamp // 1000

    try:
        display_text = datetime.fromtimestamp(raw_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        display_text = str(raw_timestamp)
    return raw_timestamp, display_text


def powershell_quote(value: str) -> str:
    """Quote a string for PowerShell single-quoted literals.

    @param value The string value to quote.
    @returns A PowerShell-safe single-quoted literal.
    """

    escaped = value.replace("'", "''")
    return "'" + escaped + "'"


def _coerce_int_sequence(value: object, fallback: Sequence[int]) -> tuple[int, ...]:
    if isinstance(value, list) and all(isinstance(item, int) for item in value):
        return tuple(value)
    return tuple(fallback)


def _is_subagent_source(raw_source: str) -> bool:
    """Return whether a serialized thread source represents a subagent.

    @param raw_source The serialized source value from SQLite.
    @returns True when the source is a subagent variant.
    """

    source_text = raw_source.strip()
    if not source_text:
        return False
    source_key = source_text.lower()
    if source_key.startswith("subagent"):
        return True
    if source_key in {"cli", "vscode", "exec", "mcp", "unknown"}:
        return False

    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and "subagent" in parsed


def _source_key(raw_source: str) -> str:
    """Return the normalized source key stored by Codex.

    @param raw_source The serialized source value from SQLite.
    @returns A lowercase source key suitable for filtering and display.
    """

    source_text = raw_source.strip()
    if not source_text:
        return ""
    if not source_text.startswith("{"):
        source_key = source_text.lower()
        return "subagent" if source_key.startswith("subagent") else source_key

    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return source_text.lower()
    if not isinstance(parsed, dict):
        return source_text.lower()
    if "subagent" in parsed:
        return "subagent"
    if "custom" in parsed:
        return str(parsed["custom"]).strip().lower()
    if "internal" in parsed:
        return "internal"
    if len(parsed) == 1:
        return next(iter(parsed)).lower()
    return source_text.lower()


def _is_interactive_chat_source(raw_source: str) -> bool:
    """Return whether the source belongs to Codex interactive chat sessions.

    @param raw_source The serialized source value from SQLite.
    @returns True for sources shown by the Chats view.
    """

    return _source_key(raw_source) in INTERACTIVE_CHAT_SOURCES


def _display_source(raw_source: str) -> str:
    """Return a short user-facing source label.

    @param raw_source The serialized source value from SQLite.
    @returns A compact source label for the table.
    """

    return "CLI" if _source_key(raw_source) == "cli" else "Non-CLI"


class ThreadTableDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Paint thread rows with a clear selection bar and no focus rectangle.

        @param painter The painter used by Qt for the cell.
        @param option The style option describing the cell state.
        @param index The model index being painted.
        @returns None.
        """

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if not selected:
            option_copy = QStyleOptionViewItem(option)
            option_copy.state &= ~QStyle.StateFlag.State_HasFocus
            super().paint(painter, option_copy, index)
            return

        painter.save()
        rect = option.rect
        painter.fillRect(rect, QColor("#2f4058"))
        if index.column() == 0:
            painter.fillRect(QRect(rect.left(), rect.top(), 4, rect.height()), QColor("#8ec5ff"))
            text_rect = rect.adjusted(14, 0, -8, 0)
        else:
            text_rect = rect.adjusted(8, 0, -8, 0)

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        elided_text = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.setPen(QColor("#eef5ff"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_text)
        painter.restore()


def load_app_config(config_path: Path) -> AppConfig:
    """Load the launcher config and create it when missing.

    @param config_path The JSON config file path.
    @returns The parsed application config.
    """

    if not config_path.exists():
        config = AppConfig()
        save_app_config(config_path, config)
        return config

    try:
        raw_data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        config = AppConfig()
        save_app_config(config_path, config)
        return config
    raw_workspaces = raw_data.get("workspaces", [])
    workspaces: list[WorkspaceEntry] = []
    for item in raw_workspaces:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        workspaces.append(WorkspaceEntry(name=name or Path(path).name or path, path=path))

    raw_ui_state = raw_data.get("ui_state", {})
    if not isinstance(raw_ui_state, dict):
        raw_ui_state = {}

    ui_state = UiState(
        selected_workspace=str(raw_ui_state.get("selected_workspace", "")),
        show_archived=bool(raw_ui_state.get("show_archived", False)),
        show_subagents=bool(raw_ui_state.get("show_subagents", False)),
        thread_scope=str(raw_ui_state.get("thread_scope", THREAD_SCOPE_WORKSPACE)),
        thread_view=str(raw_ui_state.get("thread_view", THREAD_VIEW_CHATS)),
        window_size=_coerce_int_sequence(raw_ui_state.get("window_size"), DEFAULT_WINDOW_SIZE)[:2],
        splitter_sizes=_coerce_int_sequence(raw_ui_state.get("splitter_sizes"), DEFAULT_SPLITTER_SIZES)[:2],
        column_widths=_coerce_int_sequence(raw_ui_state.get("column_widths"), DEFAULT_COLUMN_WIDTHS),
    )
    if ui_state.thread_scope not in {THREAD_SCOPE_WORKSPACE, THREAD_SCOPE_ALL_WORKSPACES}:
        ui_state.thread_scope = THREAD_SCOPE_WORKSPACE
    if ui_state.thread_view != THREAD_VIEW_CHATS:
        ui_state.thread_view = THREAD_VIEW_CHATS
    if len(ui_state.window_size) != 2:
        ui_state.window_size = DEFAULT_WINDOW_SIZE
    if len(ui_state.splitter_sizes) != 2:
        ui_state.splitter_sizes = DEFAULT_SPLITTER_SIZES

    terminal = str(raw_data.get("terminal", "wt")).strip() or "wt"
    return AppConfig(workspaces=workspaces, terminal=terminal, ui_state=ui_state)


def save_app_config(config_path: Path, config: AppConfig) -> None:
    """Persist the launcher config to disk.

    @param config_path The JSON config file path.
    @param config The configuration to persist.
    @returns None.
    """

    payload = {
        "workspaces": [{"name": item.name, "path": item.path} for item in config.workspaces],
        "terminal": "wt",
        "ui_state": {
            "selected_workspace": config.ui_state.selected_workspace,
            "show_archived": config.ui_state.show_archived,
            "show_subagents": config.ui_state.show_subagents,
            "thread_scope": config.ui_state.thread_scope,
            "thread_view": config.ui_state.thread_view,
            "window_size": list(config.ui_state.window_size),
            "splitter_sizes": list(config.ui_state.splitter_sizes),
            "column_widths": list(config.ui_state.column_widths),
        },
    }
    config_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class ThreadRepository:
    def __init__(self, database_path: Path, codex_home_path: Path) -> None:
        """Initialize the thread repository wrapper.

        @param database_path The Codex SQLite database path.
        @param codex_home_path The Codex home directory path.
        @returns None.
        """

        self._database_path = database_path
        self._codex_home_path = codex_home_path

    @property
    def database_path(self) -> Path:
        """Expose the resolved database path.

        @param None.
        @returns The resolved database file path.
        """

        return self._database_path

    @property
    def codex_home_path(self) -> Path:
        """Expose the resolved Codex home path.

        @param None.
        @returns The resolved Codex home directory path.
        """

        return self._codex_home_path

    def load_threads(
        self,
        workspace_path: str,
        archived_only: bool,
        thread_scope: str,
        thread_view: str,
    ) -> list[ThreadRecord]:
        """Load threads from the SQLite index.

        @param workspace_path The selected workspace path.
        @param archived_only Whether only archived threads should be shown.
        @param thread_scope The workspace scope to apply.
        @param thread_view The thread view filter to apply.
        @returns A list of threads sorted by most recent update.
        """

        if not self._database_path.exists():
            raise FileNotFoundError(f"Codex state database was not found: {self._database_path}")

        if thread_scope not in {THREAD_SCOPE_WORKSPACE, THREAD_SCOPE_ALL_WORKSPACES}:
            thread_scope = THREAD_SCOPE_WORKSPACE
        if thread_view != THREAD_VIEW_CHATS:
            thread_view = THREAD_VIEW_CHATS
        normalized_workspace = normalize_workspace_path(workspace_path)
        database_uri = f"file:{self._database_path}?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row

        try:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(threads)").fetchall()
                if "name" in row.keys()
            }
            if not columns:
                raise RuntimeError("The threads table is missing or could not be introspected.")

            updated_column = "updated_at" if "updated_at" in columns else "created_at"
            edge_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(thread_spawn_edges)").fetchall()
                if "name" in row.keys()
            }
            child_thread_ids: set[str] = set()
            if "child_thread_id" in edge_columns:
                child_thread_ids = {
                    str(row["child_thread_id"])
                    for row in connection.execute("SELECT child_thread_id FROM thread_spawn_edges").fetchall()
                    if row["child_thread_id"]
                }
            select_parts = [
                '"id" AS "thread_id"' if "id" in columns else "'' AS thread_id",
                '"rollout_path" AS "rollout_path"' if "rollout_path" in columns else "'' AS rollout_path",
                '"title" AS "title"' if "title" in columns else "'' AS title",
                f'"{updated_column}" AS "updated_at"' if updated_column in columns else "0 AS updated_at",
                '"cwd" AS "cwd"' if "cwd" in columns else "'' AS cwd",
                '"model" AS "model"' if "model" in columns else "'' AS model",
                '"source" AS "source"' if "source" in columns else "'' AS source",
                '"first_user_message" AS "first_user_message"'
                if "first_user_message" in columns
                else "'' AS first_user_message",
                '"archived" AS "archived"' if "archived" in columns else "0 AS archived",
            ]

            rows = connection.execute(
                f"""
                SELECT {", ".join(select_parts)}
                FROM threads
                ORDER BY {updated_column} DESC, thread_id DESC
                """
            ).fetchall()
        finally:
            connection.close()

        records: list[ThreadRecord] = []
        for row in rows:
            thread_id = str(row["thread_id"] or "")
            cwd = str(row["cwd"] or "")
            if thread_scope == THREAD_SCOPE_WORKSPACE and normalize_workspace_path(cwd) != normalized_workspace:
                continue

            archived_flag = bool(int(row["archived"] or 0))
            if archived_flag != archived_only:
                continue
            source = str(row["source"] or "")
            is_subagent = _is_subagent_source(source) or thread_id in child_thread_ids
            if is_subagent:
                continue
            if thread_view == THREAD_VIEW_CHATS and not _is_interactive_chat_source(source):
                continue

            sort_timestamp, updated_text = format_timestamp(row["updated_at"])
            title = str(row["title"] or "").strip()
            first_user_message = str(row["first_user_message"] or "").strip()
            summary = truncate_text(first_user_message, 160)
            if thread_view == THREAD_VIEW_CHATS and not title and not summary:
                continue
            display_title = title or summary or thread_id

            records.append(
                ThreadRecord(
                    thread_id=thread_id,
                    title=display_title,
                    updated_at_text=updated_text,
                    source=_display_source(source),
                    cwd=cwd,
                    model=str(row["model"] or ""),
                    rollout_path=str(row["rollout_path"] or ""),
                    first_user_message=first_user_message,
                    summary=summary,
                    archived=archived_flag,
                    sort_timestamp=sort_timestamp,
                )
            )

        records.sort(key=lambda item: (item.sort_timestamp, item.thread_id), reverse=True)
        return records

    def unarchive_thread(self, thread_id: str) -> Path:
        """Move an archived thread rollout back to sessions and update SQLite.

        @param thread_id The thread id to unarchive.
        @returns The restored rollout path.
        """

        if not self._database_path.exists():
            raise FileNotFoundError(f"Codex state database was not found: {self._database_path}")

        database_uri = f"file:{self._database_path}"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT id, rollout_path, archived FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Thread was not found: {thread_id}")
            if not bool(int(row["archived"] or 0)):
                raise RuntimeError("The selected thread is not archived.")

            archived_path = self._resolve_archived_rollout_path(str(row["rollout_path"] or ""), thread_id)
            restored_path = self._restore_archived_rollout(archived_path)
            restored_path_text = str(restored_path)
            connection.execute(
                """
                UPDATE threads
                SET archived = 0,
                    archived_at = NULL,
                    rollout_path = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (restored_path_text, int(restored_path.stat().st_mtime), thread_id),
            )
            connection.commit()
            return restored_path
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def archive_thread(self, thread_id: str) -> Path:
        """Move an active thread rollout to archived_sessions and update SQLite.

        @param thread_id The thread id to archive.
        @returns The archived rollout path.
        """

        if not self._database_path.exists():
            raise FileNotFoundError(f"Codex state database was not found: {self._database_path}")

        database_uri = f"file:{self._database_path}"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT id, rollout_path, archived FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Thread was not found: {thread_id}")
            if bool(int(row["archived"] or 0)):
                raise RuntimeError("The selected thread is already archived.")

            active_path = self._resolve_active_rollout_path(str(row["rollout_path"] or ""), thread_id)
            if self._thread_may_be_active_for_archive(thread_id, active_path):
                raise RuntimeError(ACTIVE_THREAD_ARCHIVE_MESSAGE)
            archived_path = self._archive_active_rollout(active_path)
            archived_at = int(datetime.now().timestamp())
            connection.execute(
                """
                UPDATE threads
                SET archived = 1,
                    archived_at = ?,
                    rollout_path = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (archived_at, str(archived_path), int(archived_path.stat().st_mtime), thread_id),
            )
            connection.commit()
            return archived_path
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete_archived_thread(self, thread_id: str) -> None:
        """Delete one archived thread rollout and SQLite index row.

        @param thread_id The archived thread id to delete.
        @returns None.
        """

        if not self._database_path.exists():
            raise FileNotFoundError(f"Codex state database was not found: {self._database_path}")

        database_uri = f"file:{self._database_path}"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT id, rollout_path, archived FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Thread was not found: {thread_id}")
            if not bool(int(row["archived"] or 0)):
                raise RuntimeError("Only archived threads can be deleted.")

            try:
                archived_path = self._resolve_archived_rollout_path(str(row["rollout_path"] or ""), thread_id)
            except FileNotFoundError:
                archived_path = None
            if archived_path is not None:
                archived_path.unlink()
            connection.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            if self._table_exists(connection, "thread_spawn_edges"):
                connection.execute(
                    """
                    DELETE FROM thread_spawn_edges
                    WHERE parent_thread_id = ? OR child_thread_id = ?
                    """,
                    (thread_id, thread_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete_archived_threads(self, workspace_path: str, thread_scope: str, thread_view: str) -> int:
        """Delete archived threads matching the current workspace and view filters.

        @param workspace_path The selected workspace path.
        @param thread_scope The workspace scope to apply.
        @param thread_view The thread view filter to apply.
        @returns The number of deleted archived threads.
        """

        records = self.load_threads(workspace_path, True, thread_scope, thread_view)
        deleted_count = 0
        for record in records:
            self.delete_archived_thread(record.thread_id)
            deleted_count += 1
        return deleted_count

    def _resolve_active_rollout_path(self, rollout_path_text: str, thread_id: str) -> Path:
        """Resolve the active rollout path for a thread.

        @param rollout_path_text The rollout path stored in SQLite.
        @param thread_id The thread id to locate.
        @returns The active rollout path.
        """

        sessions_root = self._codex_home_path / "sessions"
        candidates: list[Path] = []
        if rollout_path_text:
            stored_path = Path(rollout_path_text)
            candidates.append(stored_path if stored_path.is_absolute() else self._codex_home_path / stored_path)
        if sessions_root.exists():
            candidates.extend(sessions_root.rglob(f"*{thread_id}*.jsonl"))

        for candidate in candidates:
            if candidate.exists() and candidate.is_file() and self._is_path_under(candidate, sessions_root):
                return candidate
        raise FileNotFoundError(f"Active rollout was not found for thread: {thread_id}")

    def thread_has_live_process(self, thread_id: str) -> bool:
        """Return whether Codex logs point to a still-running process for a thread.

        @param thread_id The thread id to inspect in Codex logs.
        @returns True when a logged process_uuid pid is still running.
        """

        return self._has_live_log_process(thread_id)

    def _thread_may_be_active_for_archive(self, thread_id: str, rollout_path: Path) -> bool:
        """Return whether a thread should be protected from archive.

        @param thread_id The thread id to test.
        @param rollout_path The active rollout path for the thread.
        @returns True when the thread should be treated as active or unstable.
        """

        if self.thread_has_live_process(thread_id):
            return True
        return self._rollout_recently_modified(rollout_path)

    def _has_live_log_process(self, thread_id: str) -> bool:
        """Return whether recent logs point to a still-running Codex process.

        @param thread_id The thread id to inspect in Codex logs.
        @returns True when a recent process_uuid pid is still running.
        """

        logs_db_path = resolve_codex_logs_db_path(self._codex_home_path)
        if not logs_db_path.exists():
            return False

        database_uri = f"file:{logs_db_path}?mode=ro"
        try:
            connection = sqlite3.connect(database_uri, uri=True)
            try:
                rows = connection.execute(
                    """
                    SELECT process_uuid
                    FROM logs
                    WHERE thread_id = ?
                      AND process_uuid IS NOT NULL
                    ORDER BY ts DESC, ts_nanos DESC, id DESC
                    LIMIT 20
                    """,
                    (thread_id,),
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return False

        for row in rows:
            process_uuid = str(row[0] or "")
            match = PROCESS_UUID_PATTERN.match(process_uuid)
            if match and self._is_pid_running(int(match.group(1))):
                return True
        return False

    def _is_pid_running(self, pid: int) -> bool:
        """Return whether a Windows process id is still running.

        @param pid The process id to test.
        @returns True when the process exists and has not exited.
        """

        if pid <= 0 or sys.platform != "win32":
            return False

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    def _rollout_recently_modified(self, rollout_path: Path) -> bool:
        """Return whether a rollout was recently modified.

        @param rollout_path The rollout path to inspect.
        @returns True when the rollout mtime is within the active grace period.
        """

        try:
            modified_at = rollout_path.stat().st_mtime
        except OSError:
            return False
        return time.time() - modified_at < ACTIVE_ROLLOUT_MTIME_GRACE_SECONDS

    def _archive_active_rollout(self, active_path: Path) -> Path:
        """Move an active rollout into the archived_sessions directory.

        @param active_path The active rollout file path.
        @returns The archived rollout file path.
        """

        sessions_root = self._codex_home_path / "sessions"
        if not self._is_path_under(active_path, sessions_root):
            raise RuntimeError(f"Active rollout is outside the sessions directory: {active_path}")

        archived_root = self._codex_home_path / "archived_sessions"
        archived_root.mkdir(parents=True, exist_ok=True)
        archived_path = archived_root / active_path.name
        if archived_path.exists():
            raise FileExistsError(f"Archived rollout already exists: {archived_path}")
        shutil.move(str(active_path), str(archived_path))
        os.utime(archived_path, None)
        return archived_path

    def _resolve_archived_rollout_path(self, rollout_path_text: str, thread_id: str) -> Path:
        """Resolve the archived rollout path for a thread.

        @param rollout_path_text The rollout path stored in SQLite.
        @param thread_id The thread id to locate.
        @returns The archived rollout path.
        """

        archived_root = self._codex_home_path / "archived_sessions"
        candidates: list[Path] = []
        if rollout_path_text:
            stored_path = Path(rollout_path_text)
            candidates.append(stored_path if stored_path.is_absolute() else self._codex_home_path / stored_path)
        if archived_root.exists():
            candidates.extend(archived_root.rglob(f"*{thread_id}*.jsonl"))

        for candidate in candidates:
            if candidate.exists() and candidate.is_file() and self._is_path_under(candidate, archived_root):
                return candidate
        raise FileNotFoundError(f"Archived rollout was not found for thread: {thread_id}")

    def _restore_archived_rollout(self, archived_path: Path) -> Path:
        """Move an archived rollout into the active sessions tree.

        @param archived_path The archived rollout file path.
        @returns The restored rollout file path.
        """

        file_name = archived_path.name
        match = ROLLOUT_FILE_PATTERN.match(file_name)
        if not match:
            raise RuntimeError(f"Archived rollout filename does not contain a session date: {file_name}")

        year, month, day = match.groups()
        dest_dir = self._codex_home_path / "sessions" / year / month / day
        dest_dir.mkdir(parents=True, exist_ok=True)
        restored_path = dest_dir / file_name
        if restored_path.exists():
            raise FileExistsError(f"Active rollout already exists: {restored_path}")
        shutil.move(str(archived_path), str(restored_path))
        os.utime(restored_path, None)
        return restored_path

    def _is_path_under(self, path: Path, parent: Path) -> bool:
        """Return whether a path is under a parent directory.

        @param path The path to test.
        @param parent The expected parent directory.
        @returns True when path resolves under parent.
        """

        try:
            path.resolve().relative_to(parent.resolve())
        except ValueError:
            return False
        return True

    def _table_exists(self, connection: sqlite3.Connection, table_name: str) -> bool:
        """Return whether a SQLite table exists.

        @param connection The SQLite connection to inspect.
        @param table_name The table name to check.
        @returns True when the table exists.
        """

        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None


class WorkspaceDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, workspace: WorkspaceEntry | None = None) -> None:
        """Build the add/edit workspace dialog.

        @param parent The parent widget.
        @param workspace The optional workspace to edit.
        @returns None.
        """

        super().__init__(parent)
        self.setWindowTitle("Workspace")
        self._path = workspace.path if workspace else ""

        self._name_edit = QLineEdit(workspace.name if workspace else "")
        self._path_edit = QLineEdit(self._path)
        browse_button = QPushButton("Browse...")
        save_button = QPushButton("Save")
        cancel_button = QPushButton("Cancel")

        browse_button.clicked.connect(self._browse_directory)
        save_button.clicked.connect(self._validate_and_accept)
        cancel_button.clicked.connect(self.reject)

        layout = QGridLayout(self)
        layout.addWidget(QLabel("Name"), 0, 0)
        layout.addWidget(self._name_edit, 0, 1, 1, 2)
        layout.addWidget(QLabel("Path"), 1, 0)
        layout.addWidget(self._path_edit, 1, 1)
        layout.addWidget(browse_button, 1, 2)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row, 2, 0, 1, 3)

        self.resize(560, 120)

    def workspace_entry(self) -> WorkspaceEntry:
        """Return the validated workspace payload.

        @param None.
        @returns The workspace entry from the dialog fields.
        """

        path_text = self._path_edit.text().strip()
        name_text = self._name_edit.text().strip() or Path(path_text).name or path_text
        return WorkspaceEntry(name=name_text, path=path_text)

    def _browse_directory(self) -> None:
        selected_path = QFileDialog.getExistingDirectory(self, "Select Workspace", self._path_edit.text().strip())
        if selected_path:
            self._path_edit.setText(selected_path)
            if not self._name_edit.text().strip():
                self._name_edit.setText(Path(selected_path).name)

    def _validate_and_accept(self) -> None:
        candidate = Path(self._path_edit.text().strip())
        if not candidate.exists() or not candidate.is_dir():
            QMessageBox.warning(self, "Invalid Path", "Please choose an existing directory.")
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path, state_db_path: Path, codex_home_path: Path) -> None:
        """Create the main launcher window.

        @param config_path The launcher config path.
        @param state_db_path The Codex SQLite database path.
        @param codex_home_path The Codex home directory path.
        @returns None.
        """

        super().__init__()
        self._config_path = config_path
        self._config = load_app_config(config_path)
        self._repository = ThreadRepository(state_db_path, codex_home_path)
        self._threads: list[ThreadRecord] = []

        self.setWindowTitle("codex-cli-startup")
        self.resize(*self._config.ui_state.window_size)

        self._workspace_list = QListWidget()
        self._workspace_list.currentRowChanged.connect(self._handle_workspace_changed)

        add_button = QPushButton("Add")
        edit_button = QPushButton("Edit")
        delete_button = QPushButton("Delete")
        add_button.clicked.connect(self._add_workspace)
        edit_button.clicked.connect(self._edit_workspace)
        delete_button.clicked.connect(self._delete_workspace)

        left_buttons = QHBoxLayout()
        left_buttons.addWidget(add_button)
        left_buttons.addWidget(edit_button)
        left_buttons.addWidget(delete_button)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("Workspaces"))
        left_layout.addWidget(self._workspace_list, 1)
        left_layout.addLayout(left_buttons)

        self._archived_toggle = QPushButton("Archived only")
        self._archived_toggle.setCheckable(True)
        self._archived_toggle.setChecked(self._config.ui_state.show_archived)
        self._archived_toggle.toggled.connect(self._handle_archived_toggled)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._refresh_threads)

        self._thread_status_label = QLabel("Select a workspace to load threads.")
        self._thread_table = QTableWidget(0, 3)
        self._thread_table.setHorizontalHeaderLabels(["Title", "Updated", "Source"])
        self._thread_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._thread_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._thread_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._thread_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._thread_table.setAlternatingRowColors(True)
        self._thread_table.setWordWrap(False)
        self._thread_table.setShowGrid(False)
        self._thread_table.setItemDelegate(ThreadTableDelegate(self._thread_table))
        self._thread_table.setStyleSheet(
            """
            QTableWidget {
                gridline-color: transparent;
                outline: 0;
            }
            QTableWidget::item {
                padding: 4px 8px;
                border: 0;
            }
            QTableWidget::item:focus {
                outline: 0;
                border: 0;
            }
            """
        )
        self._thread_table.verticalHeader().setVisible(False)
        self._thread_table.itemSelectionChanged.connect(self._handle_thread_selection_changed)
        self._thread_table.itemDoubleClicked.connect(lambda _item: self._resume_selected_thread())

        for index, width in enumerate(self._config.ui_state.column_widths):
            if index < self._thread_table.columnCount():
                self._thread_table.setColumnWidth(index, width)
        for index in range(len(self._config.ui_state.column_widths), self._thread_table.columnCount()):
            if index < len(DEFAULT_COLUMN_WIDTHS):
                self._thread_table.setColumnWidth(index, DEFAULT_COLUMN_WIDTHS[index])

        self._new_thread_button = QPushButton("New Thread")
        self._resume_button = QPushButton("Resume Selected")
        self._fork_button = QPushButton("Fork Selected")
        self._archive_button = QPushButton("Archive Selected")
        self._unarchive_button = QPushButton("Unarchive Selected")
        self._delete_thread_button = QPushButton("Delete Selected")
        self._delete_archived_button = QPushButton("Delete All Archived")
        self._new_thread_button.clicked.connect(self._start_new_thread)
        self._resume_button.clicked.connect(self._resume_selected_thread)
        self._fork_button.clicked.connect(self._fork_selected_thread)
        self._archive_button.clicked.connect(self._archive_selected_thread)
        self._unarchive_button.clicked.connect(self._unarchive_selected_thread)
        self._delete_thread_button.clicked.connect(self._delete_selected_thread)
        self._delete_archived_button.clicked.connect(self._delete_all_archived_threads)

        self._detail_panel = QWidget()
        detail_layout = QGridLayout(self._detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(QLabel("Details"), 0, 0, 1, 2)
        self._detail_cwd = QLineEdit()
        self._detail_model = QLineEdit()
        self._detail_thread_id = QLineEdit()
        for line_edit in (self._detail_cwd, self._detail_model, self._detail_thread_id):
            line_edit.setReadOnly(True)
        self._detail_first_message = QPlainTextEdit()
        self._detail_first_message.setReadOnly(True)
        self._detail_first_message.setMaximumHeight(90)
        detail_layout.addWidget(QLabel("Cwd"), 1, 0)
        detail_layout.addWidget(self._detail_cwd, 1, 1)
        detail_layout.addWidget(QLabel("Model"), 2, 0)
        detail_layout.addWidget(self._detail_model, 2, 1)
        detail_layout.addWidget(QLabel("Thread ID"), 3, 0)
        detail_layout.addWidget(self._detail_thread_id, 3, 1)
        detail_layout.addWidget(QLabel("First User Message"), 4, 0)
        detail_layout.addWidget(self._detail_first_message, 4, 1)
        self._detail_panel.setVisible(False)

        right_top = QHBoxLayout()
        right_top.addWidget(self._archived_toggle)
        right_top.addStretch(1)
        right_top.addWidget(self._new_thread_button)
        right_top.addWidget(refresh_button)

        right_buttons = QHBoxLayout()
        right_buttons.addStretch(1)
        right_buttons.addWidget(self._archive_button)
        right_buttons.addWidget(self._unarchive_button)
        right_buttons.addWidget(self._delete_thread_button)
        right_buttons.addWidget(self._delete_archived_button)
        right_buttons.addWidget(self._fork_button)
        right_buttons.addWidget(self._resume_button)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addLayout(right_top)
        right_layout.addWidget(self._thread_status_label)
        right_layout.addWidget(self._thread_table, 1)
        right_layout.addWidget(self._detail_panel)
        right_layout.addLayout(right_buttons)

        self._splitter = QSplitter()
        self._splitter.addWidget(left_panel)
        self._splitter.addWidget(right_panel)
        self._splitter.setCollapsible(0, False)
        self._splitter.setCollapsible(1, False)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addWidget(self._splitter)
        self.setCentralWidget(root)

        self._populate_workspace_list()
        self._splitter.setSizes(list(self._config.ui_state.splitter_sizes))
        self._update_action_state()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Persist UI state before the window closes.

        @param event The close event instance.
        @returns None.
        """

        self._persist_ui_state()
        super().closeEvent(event)

    def _populate_workspace_list(self) -> None:
        self._workspace_list.clear()
        selected_normalized = normalize_workspace_path(self._config.ui_state.selected_workspace)
        selected_row = 0

        all_item = QListWidgetItem("All")
        all_item.setToolTip("Show chats from all workspaces")
        all_item.setData(Qt.ItemDataRole.UserRole, ALL_WORKSPACES_SELECTION)
        self._workspace_list.addItem(all_item)
        if (
            self._config.ui_state.selected_workspace == ALL_WORKSPACES_SELECTION
            or self._config.ui_state.thread_scope == THREAD_SCOPE_ALL_WORKSPACES
        ):
            selected_row = 0

        for index, workspace in enumerate(self._config.workspaces):
            item = QListWidgetItem(workspace.name)
            item.setToolTip(workspace.path)
            item.setData(Qt.ItemDataRole.UserRole, workspace.path)
            self._workspace_list.addItem(item)
            if (
                self._config.ui_state.thread_scope != THREAD_SCOPE_ALL_WORKSPACES
                and normalize_workspace_path(workspace.path) == selected_normalized
            ):
                selected_row = index + 1

        self._workspace_list.setCurrentRow(selected_row)

    def _selected_workspace(self) -> WorkspaceEntry | None:
        current_row = self._workspace_list.currentRow()
        if current_row <= 0:
            return None
        workspace_index = current_row - 1
        if workspace_index >= len(self._config.workspaces):
            return None
        return self._config.workspaces[workspace_index]

    def _is_all_workspaces_selected(self) -> bool:
        current_item = self._workspace_list.currentItem()
        return current_item is not None and current_item.data(Qt.ItemDataRole.UserRole) == ALL_WORKSPACES_SELECTION

    def _add_workspace(self) -> None:
        dialog = WorkspaceDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        workspace = dialog.workspace_entry()
        self._upsert_workspace(workspace, None)

    def _edit_workspace(self) -> None:
        current_row = self._workspace_list.currentRow()
        if current_row <= 0:
            QMessageBox.information(self, "Edit Workspace", "Please select a workspace first.")
            return

        editing_index = current_row - 1
        dialog = WorkspaceDialog(self, self._config.workspaces[editing_index])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        workspace = dialog.workspace_entry()
        self._upsert_workspace(workspace, editing_index)

    def _delete_workspace(self) -> None:
        current_row = self._workspace_list.currentRow()
        workspace = self._selected_workspace()
        if current_row <= 0 or workspace is None:
            QMessageBox.information(self, "Delete Workspace", "Please select a workspace first.")
            return

        confirmation = QMessageBox.question(
            self,
            "Delete Workspace",
            f"Remove '{workspace.name}' from the launcher list?",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        del self._config.workspaces[current_row - 1]
        if not self._config.workspaces:
            self._config.ui_state.selected_workspace = ALL_WORKSPACES_SELECTION
            self._config.ui_state.thread_scope = THREAD_SCOPE_ALL_WORKSPACES
        self._persist_ui_state()
        self._populate_workspace_list()

    def _upsert_workspace(self, workspace: WorkspaceEntry, editing_index: int | None) -> None:
        normalized_candidate = normalize_workspace_path(workspace.path)
        for index, existing in enumerate(self._config.workspaces):
            if index == editing_index:
                continue
            if normalize_workspace_path(existing.path) == normalized_candidate:
                QMessageBox.warning(self, "Duplicate Workspace", "This workspace path already exists.")
                return

        if editing_index is None:
            self._config.workspaces.append(workspace)
            target_index = len(self._config.workspaces)
        else:
            self._config.workspaces[editing_index] = workspace
            target_index = editing_index + 1

        self._config.ui_state.selected_workspace = workspace.path
        self._config.ui_state.thread_scope = THREAD_SCOPE_WORKSPACE
        self._persist_ui_state()
        self._populate_workspace_list()
        self._workspace_list.setCurrentRow(target_index)

    def _handle_workspace_changed(self, _row: int) -> None:
        workspace = self._selected_workspace()
        if self._is_all_workspaces_selected():
            self._config.ui_state.selected_workspace = ALL_WORKSPACES_SELECTION
            self._config.ui_state.thread_scope = THREAD_SCOPE_ALL_WORKSPACES
        else:
            self._config.ui_state.selected_workspace = workspace.path if workspace else ""
            self._config.ui_state.thread_scope = THREAD_SCOPE_WORKSPACE
        self._persist_ui_state()
        self._refresh_threads()

    def _handle_archived_toggled(self, checked: bool) -> None:
        self._config.ui_state.show_archived = checked
        self._persist_ui_state()
        self._refresh_threads()

    def _current_thread_scope(self) -> str:
        return THREAD_SCOPE_ALL_WORKSPACES if self._is_all_workspaces_selected() else THREAD_SCOPE_WORKSPACE

    def _current_thread_view(self) -> str:
        return THREAD_VIEW_CHATS

    def _refresh_threads(self) -> None:
        workspace = self._selected_workspace()
        thread_scope = self._current_thread_scope()
        thread_view = self._current_thread_view()
        if workspace is None and thread_scope == THREAD_SCOPE_WORKSPACE:
            self._threads = []
            self._thread_table.setRowCount(0)
            self._thread_status_label.setText("No workspace configured.")
            self._update_action_state()
            return

        try:
            self._threads = self._repository.load_threads(
                workspace.path if workspace else "",
                self._archived_toggle.isChecked(),
                thread_scope,
                thread_view,
            )
        except Exception as error:  # noqa: BLE001
            self._threads = []
            self._thread_table.setRowCount(0)
            self._thread_status_label.setText("Failed to load threads.")
            self._update_action_state()
            QMessageBox.critical(self, "Thread Load Failed", str(error))
            return

        self._thread_table.setRowCount(len(self._threads))
        for row_index, thread in enumerate(self._threads):
            values = [
                thread.title,
                thread.updated_at_text,
                thread.source,
            ]
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, thread.thread_id)
                self._thread_table.setItem(row_index, column_index, item)

        scope_text = "All Workspaces" if thread_scope == THREAD_SCOPE_ALL_WORKSPACES else "Workspace"
        workspace_text = "All" if thread_scope == THREAD_SCOPE_ALL_WORKSPACES else workspace.path if workspace else "(none)"
        archived_text = "only" if self._archived_toggle.isChecked() else "hidden"
        self._thread_status_label.setText(
            f"Threads: {len(self._threads)} | Scope: {scope_text} | "
            f"Archived: {archived_text} | Workspace: {workspace_text} | DB: {self._repository.database_path}"
        )
        self._thread_table.resizeRowsToContents()
        self._update_action_state()

    def _selected_thread(self) -> ThreadRecord | None:
        current_row = self._thread_table.currentRow()
        if current_row < 0 or current_row >= len(self._threads):
            return None
        return self._threads[current_row]

    def _handle_thread_selection_changed(self) -> None:
        self._update_action_state()

    def _update_action_state(self) -> None:
        workspace_selected = self._selected_workspace() is not None
        thread = self._selected_thread()
        thread_selected = thread is not None
        archived_selected = thread is not None and thread.archived
        archived_view_active = self._archived_toggle.isChecked()
        active_thread_selected = thread_selected and not archived_selected and not archived_view_active

        self._new_thread_button.setVisible(workspace_selected)
        self._new_thread_button.setEnabled(workspace_selected)

        self._resume_button.setVisible(active_thread_selected)
        self._resume_button.setEnabled(active_thread_selected)
        self._fork_button.setVisible(active_thread_selected)
        self._fork_button.setEnabled(active_thread_selected)
        self._archive_button.setVisible(active_thread_selected)
        self._archive_button.setEnabled(active_thread_selected)

        self._unarchive_button.setVisible(archived_selected)
        self._unarchive_button.setEnabled(archived_selected)
        self._delete_thread_button.setVisible(archived_selected)
        self._delete_thread_button.setEnabled(archived_selected)

        delete_all_visible = archived_view_active and bool(self._threads)
        self._delete_archived_button.setVisible(delete_all_visible)
        self._delete_archived_button.setEnabled(delete_all_visible)
        self._update_detail_panel()

    def _update_detail_panel(self) -> None:
        thread = self._selected_thread()
        if thread is None:
            self._detail_panel.setVisible(False)
            self._detail_cwd.clear()
            self._detail_model.clear()
            self._detail_thread_id.clear()
            self._detail_first_message.clear()
            return

        self._detail_cwd.setText(thread.cwd)
        self._detail_model.setText(thread.model)
        self._detail_thread_id.setText(thread.thread_id)
        self._detail_first_message.setPlainText(thread.first_user_message)
        self._detail_panel.setVisible(True)

    def _resume_selected_thread(self) -> None:
        thread = self._validated_selected_thread("Resume Thread")
        if thread is None:
            return
        self._launch_codex_resume(thread)

    def _fork_selected_thread(self) -> None:
        thread = self._validated_selected_thread("Fork Thread")
        if thread is None:
            return
        if self._repository.thread_has_live_process(thread.thread_id):
            confirmation = QMessageBox.question(
                self,
                "Fork Active Thread",
                "This thread appears to be running.\n\n"
                "Forking may capture only the currently persisted state. Continue?",
            )
            if confirmation != QMessageBox.StandardButton.Yes:
                return
        self._launch_codex_fork(thread)

    def _archive_selected_thread(self) -> None:
        thread = self._validated_selected_thread("Archive Thread")
        if thread is None:
            return

        confirmation = QMessageBox.question(
            self,
            "Archive Thread",
            f"Archive this thread?\n\n{thread.title}\n{thread.thread_id}",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        try:
            self._repository.archive_thread(thread.thread_id)
        except Exception as error:  # noqa: BLE001
            QMessageBox.critical(self, "Archive Failed", str(error))
            return

        self._refresh_threads()

    def _unarchive_selected_thread(self) -> None:
        thread = self._validated_selected_thread("Unarchive Thread", allow_archived=True)
        if thread is None:
            return
        if not thread.archived:
            QMessageBox.information(self, "Unarchive Thread", "The selected thread is not archived.")
            return

        try:
            restored_path = self._repository.unarchive_thread(thread.thread_id)
        except Exception as error:  # noqa: BLE001
            QMessageBox.critical(self, "Unarchive Failed", str(error))
            return

        QMessageBox.information(
            self,
            "Unarchive Thread",
            f"Thread was unarchived.\n\nRestored rollout:\n{restored_path}",
        )
        self._refresh_threads()

    def _delete_selected_thread(self) -> None:
        thread = self._validated_selected_thread("Delete Thread", allow_archived=True)
        if thread is None:
            return
        if not thread.archived:
            QMessageBox.information(self, "Delete Thread", "Only archived threads can be deleted.")
            return

        confirmation = QMessageBox.question(
            self,
            "Delete Thread",
            f"Delete this archived thread permanently?\n\n{thread.title}\n{thread.thread_id}",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        try:
            self._repository.delete_archived_thread(thread.thread_id)
        except Exception as error:  # noqa: BLE001
            QMessageBox.critical(self, "Delete Failed", str(error))
            return

        QMessageBox.information(self, "Delete Thread", "Archived thread was deleted.")
        self._refresh_threads()

    def _delete_all_archived_threads(self) -> None:
        if not self._archived_toggle.isChecked():
            QMessageBox.information(
                self,
                "Delete All Archived",
                "Turn on Archived only before deleting archived threads.",
            )
            return
        if not self._threads:
            QMessageBox.information(self, "Delete All Archived", "There are no archived threads to delete.")
            return

        workspace = self._selected_workspace()
        thread_scope = self._current_thread_scope()
        scope_text = "All" if thread_scope == THREAD_SCOPE_ALL_WORKSPACES else workspace.name if workspace else "Workspace"
        confirmation = QMessageBox.question(
            self,
            "Delete All Archived",
            f"Delete all {len(self._threads)} archived threads in {scope_text} permanently?",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        try:
            deleted_count = self._repository.delete_archived_threads(
                workspace.path if workspace else "",
                thread_scope,
                self._current_thread_view(),
            )
        except Exception as error:  # noqa: BLE001
            QMessageBox.critical(self, "Delete All Archived Failed", str(error))
            return

        QMessageBox.information(
            self,
            "Delete All Archived",
            f"Deleted {deleted_count} archived threads.",
        )
        self._refresh_threads()

    def _validated_selected_thread(self, dialog_title: str, allow_archived: bool = False) -> ThreadRecord | None:
        thread = self._selected_thread()
        if thread is None:
            QMessageBox.information(self, dialog_title, "Please select a thread first.")
            return None
        if not thread.thread_id:
            QMessageBox.warning(self, dialog_title, "The selected thread does not have a valid thread id.")
            return None
        if thread.archived and not allow_archived:
            QMessageBox.warning(self, dialog_title, "Archived threads must be unarchived before this action.")
            return None
        return thread

    def _start_new_thread(self) -> None:
        self._launch_codex()

    def _launch_codex_resume(self, thread: ThreadRecord) -> None:
        self._launch_codex("resume", thread.thread_id, workspace_path=thread.cwd)

    def _launch_codex_fork(self, thread: ThreadRecord) -> None:
        self._launch_codex("fork", thread.thread_id, workspace_path=thread.cwd)

    def _launch_codex(self, *codex_args: str, workspace_path: str | None = None) -> None:
        workspace = self._selected_workspace()
        launch_path = strip_windows_verbatim_prefix(workspace_path or (workspace.path if workspace else ""))
        if not launch_path:
            QMessageBox.information(self, "Launch Codex", "Please select a workspace first.")
            return
        if not Path(launch_path).exists():
            QMessageBox.critical(self, "Workspace Not Found", f"The workspace path no longer exists:\n\n{launch_path}")
            return

        terminal_path = shutil.which(self._config.terminal)
        if not terminal_path:
            QMessageBox.critical(self, "Windows Terminal Not Found", "`wt.exe` was not found in PATH.")
            return

        shell_path = shutil.which("pwsh")
        if not shell_path:
            QMessageBox.critical(self, "pwsh Not Found", "PowerShell 7 (`pwsh`) was not found in PATH.")
            return

        if not self._find_command_source("codex", shell_path):
            QMessageBox.critical(self, "codex Not Found", "`codex` was not found in PATH for PowerShell 7.")
            return

        child_env = os.environ.copy()
        child_env.pop("TERM", None)
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        shell_command = "codex -C " + powershell_quote(launch_path)
        if codex_args:
            shell_command += " " + " ".join(powershell_quote(argument) for argument in codex_args)
        terminal_title = (
            workspace.name
            if workspace and normalize_workspace_path(workspace.path) == normalize_workspace_path(launch_path)
            else ""
        )
        terminal_title = terminal_title or Path(launch_path).name or "Codex"

        try:
            subprocess.Popen(  # noqa: S603
                [
                    terminal_path,
                    "-w",
                    "-1",
                    "nt",
                    "-d",
                    launch_path,
                    "--title",
                    terminal_title,
                    "--suppressApplicationTitle",
                    shell_path,
                    "-NoExit",
                    "-Command",
                    shell_command,
                ],
                creationflags=creation_flags,
                env=child_env,
            )
        except OSError as error:
            QMessageBox.critical(self, "Launch Failed", f"Failed to open a new PowerShell window.\n\n{error}")

    def _find_command_source(self, command_name: str, terminal_path: str) -> str | None:
        direct_path = shutil.which(command_name)
        if direct_path:
            return direct_path

        try:
            completed = subprocess.run(  # noqa: S603
                [
                    terminal_path,
                    "-NoLogo",
                    "-NoProfile",
                    "-Command",
                    (
                        f"$command = Get-Command {command_name} -ErrorAction SilentlyContinue; "
                        "if ($command) { $command.Source }"
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None

        source = completed.stdout.strip()
        return source or None

    def _persist_ui_state(self) -> None:
        self._config.terminal = "wt"
        self._config.ui_state.window_size = (self.width(), self.height())
        self._config.ui_state.show_archived = self._archived_toggle.isChecked()
        self._config.ui_state.show_subagents = False
        self._config.ui_state.thread_scope = self._current_thread_scope()
        self._config.ui_state.thread_view = self._current_thread_view()
        self._config.ui_state.splitter_sizes = tuple(self._splitter.sizes()[:2])
        self._config.ui_state.column_widths = tuple(
            self._thread_table.columnWidth(index) for index in range(self._thread_table.columnCount())
        )
        save_app_config(self._config_path, self._config)


def main() -> int:
    """Launch the GUI application.

    @param None.
    @returns The process exit code.
    """

    set_windows_app_user_model_id(APP_USER_MODEL_ID)
    app = QApplication(sys.argv)
    app.setApplicationName("codex-cli-startup")
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window = MainWindow(CONFIG_PATH, resolve_codex_state_db_path(), resolve_codex_home_path())
    if APP_ICON_PATH.exists():
        window.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
