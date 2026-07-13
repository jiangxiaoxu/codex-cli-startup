from __future__ import annotations

import ctypes
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

from config_paths import (
    ConfigPathError,
    atomic_write_text,
    configuration_lock,
    resolve_config_path,
)

from PySide6.QtCore import QModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QDragMoveEvent, QDropEvent, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
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


def resolve_resource_path(relative_path: str) -> Path:
    """Resolve a source or PyInstaller-bundled resource path.

    @param relative_path: Project-relative resource path.
    @returns: Absolute resource path.
    """

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


APP_ICON_PATH = resolve_resource_path("assets/codex-cli-startup.ico")
APP_USER_MODEL_ID = "codex-cli-startup.app"
DEFAULT_WINDOW_SIZE = (1280, 780)
DEFAULT_SPLITTER_SIZES = (320, 960)
DEFAULT_DETAIL_SPLITTER_SIZES = (560, 170)
DEFAULT_COLUMN_WIDTHS = (420, 260, 165, 100)
ROLLOUT_FILE_PATTERN = re.compile(
    r"^rollout-(\d{4})-(\d{2})-(\d{2})T.+-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"\.jsonl(?:\.zst)?$"
)
PROCESS_UUID_PATTERN = re.compile(r"^pid:(\d+):.+$")
CODEX_LOGS_DB_FILENAME = "logs_2.sqlite"
APP_SERVER_THREAD_LIST_TIMEOUT_SECONDS = 8
APP_SERVER_UNSUPPORTED_ERROR_CODES = {-32601}
ACTIVE_ROLLOUT_MTIME_GRACE_SECONDS = 90
ACTIVE_THREAD_ARCHIVE_MESSAGE = "Thread appears active. Exit the Codex session before archiving."
ACTIVE_THREAD_UNKNOWN_MESSAGE = "Thread activity could not be confirmed. Refresh Codex logs before archiving."
LIVE_PROCESS_STATE_LIVE = "live"
LIVE_PROCESS_STATE_NOT_LIVE = "not_live"
LIVE_PROCESS_STATE_UNKNOWN = "unknown"
THREAD_WRITE_REQUIRED_COLUMNS = {"id", "rollout_path", "archived", "archived_at", "updated_at"}
THREAD_SCOPE_WORKSPACE = "workspace"
THREAD_SCOPE_ALL_WORKSPACES = "all_workspaces"
THREAD_SCOPE_OTHER_WORKSPACES = "other_workspaces"
THREAD_VIEW_CHATS = "chats"
ALL_WORKSPACES_SELECTION = "__all_workspaces__"
OTHER_WORKSPACES_SELECTION = "__other_workspaces__"
FIXED_WORKSPACE_ROW_COUNT = 2
TITLE_DISPLAY_LIMIT = 120
ROLLOUT_GLOB_PATTERNS = ("*.jsonl", "*.jsonl.zst")
INTERACTIVE_CHAT_SOURCES = {"cli", "vscode"}
SOURCE_LABELS = {
    "cli": "CLI",
    "vscode": "VSCode",
    "exec": "Exec",
    "mcp": "AppServer",
    "appserver": "AppServer",
    "subagent": "SubAgent",
    "subagentreview": "SubAgentReview",
    "subagentcompact": "SubAgentCompact",
    "subagentthreadspawn": "SubAgentThreadSpawn",
    "subagentother": "SubAgentOther",
    "internal": "Internal",
    "unknown": "Unknown",
}
SUBAGENT_SOURCE_KEYS = {
    "subagent": "subagent",
    "sub_agent": "subagent",
    "sub-agent": "subagent",
    "subagentreview": "subagentreview",
    "subagent_review": "subagentreview",
    "sub-agent-review": "subagentreview",
    "subagentcompact": "subagentcompact",
    "subagent_compact": "subagentcompact",
    "sub-agent-compact": "subagentcompact",
    "subagentthreadspawn": "subagentthreadspawn",
    "subagent_thread_spawn": "subagentthreadspawn",
    "sub-agent-thread-spawn": "subagentthreadspawn",
    "subagentother": "subagentother",
    "subagent_other": "subagentother",
    "sub-agent-other": "subagentother",
}
THREAD_SOURCE_KEYS = {
    "subagent": "subagent",
    "review": "subagentreview",
    "compact": "subagentcompact",
    "thread_spawn": "subagentthreadspawn",
    "threadspawn": "subagentthreadspawn",
    "other": "subagentother",
    "memory_consolidation": "internal",
}


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
    detail_splitter_sizes: tuple[int, int] = DEFAULT_DETAIL_SPLITTER_SIZES
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
    created_at_text: str
    updated_at_text: str
    source: str
    cwd: str
    model: str
    model_provider: str
    reasoning_effort: str
    rollout_path: str
    first_user_message: str
    summary: str
    archived: bool
    rollout_missing: bool
    sort_timestamp: int
    tokens_used: int
    cli_version: str
    git_branch: str
    git_sha: str


@dataclass(slots=True)
class ThreadUsageStats:
    total_tokens: int | None = None
    last_total_tokens: int | None = None
    last_input_tokens: int | None = None
    last_cached_input_tokens: int | None = None
    model_context_window: int | None = None


@dataclass(slots=True)
class ThreadDeletePlan:
    thread_ids: list[str]
    archived_paths: dict[str, Path]


class AppServerThreadRequestRejected(RuntimeError):
    def __init__(self, request_id: int, code: int | None, message: str) -> None:
        """Initialize a non-fallback app-server request error.

        @param request_id The request id that received the error.
        @param code The app-server error code, or None when absent.
        @param message The app-server error message.
        @returns None.
        """

        self.request_id = request_id
        self.code = code
        self.message = message
        super().__init__(f"app-server returned an error for response id {request_id}: {message}")


class AppServerThreadRequestUnsupported(RuntimeError):
    pass


class AppServerThreadClient:
    def __init__(self, codex_home_path: Path, timeout_seconds: int = APP_SERVER_THREAD_LIST_TIMEOUT_SECONDS) -> None:
        """Initialize a one-shot Codex app-server thread list client.

        @param codex_home_path The Codex home directory app-server should read.
        @param timeout_seconds The maximum time to wait for the stdio request.
        @returns None.
        """

        self._codex_home_path = codex_home_path
        self._timeout_seconds = timeout_seconds

    def list_threads(self, archived_only: bool) -> list[dict[str, object]]:
        """Return app-server thread/list data.

        @param archived_only Whether archived threads should be requested.
        @returns The raw Thread objects returned by app-server.
        """

        result = self._request(
            "thread/list",
            {
                "archived": archived_only,
                "sourceKinds": ["cli", "vscode"],
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "useStateDbOnly": True,
            },
        )
        data = result.get("data")
        if not isinstance(data, list):
            raise RuntimeError("app-server thread/list response did not include a data array.")
        threads: list[dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                raise RuntimeError("app-server thread/list response included a non-object thread.")
            threads.append(item)
        return threads

    def archive_thread(self, thread_id: str) -> None:
        """Archive a thread through app-server.

        @param thread_id The thread id to archive.
        @returns None.
        """

        self._thread_lifecycle_request("thread/archive", thread_id)

    def unarchive_thread(self, thread_id: str) -> dict[str, object]:
        """Unarchive a thread through app-server.

        @param thread_id The thread id to unarchive.
        @returns The raw app-server Thread object when returned.
        """

        result = self._thread_lifecycle_request("thread/unarchive", thread_id)
        thread = result.get("thread")
        return thread if isinstance(thread, dict) else {}

    def delete_thread(self, thread_id: str) -> None:
        """Delete an archived thread through app-server.

        @param thread_id The archived thread id to delete.
        @returns None.
        """

        self._thread_lifecycle_request("thread/delete", thread_id)

    def _thread_lifecycle_request(self, method: str, thread_id: str) -> dict[str, object]:
        return self._request(method, {"threadId": thread_id})

    def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        messages = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-cli-startup",
                        "title": "Codex CLI Startup",
                        "version": "0",
                    },
                    "capabilities": None,
                },
            },
            {"method": "initialized"},
            {"id": 2, "method": method, "params": params},
        ]
        responses = self._run_messages(messages)
        self._response_result(responses, 1)
        return self._response_result(responses, 2)

    def _resolve_codex_executable(self) -> str:
        """Resolve the Codex CLI executable for subprocess execution.

        @param None.
        @returns The best executable path or command name for Codex CLI.
        """

        candidates = ("codex.exe", "codex.cmd", "codex.bat", "codex") if os.name == "nt" else ("codex",)
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return "codex"

    def _run_messages(self, messages: Sequence[dict[str, object]]) -> dict[int, dict[str, object]]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self._codex_home_path)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [self._resolve_codex_executable(), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
        )
        stdout_queue: queue.Queue[str] = queue.Queue()
        stderr_lines: list[str] = []
        stdout_thread = threading.Thread(
            target=self._read_pipe_lines,
            args=(process.stdout, stdout_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._collect_pipe_lines,
            args=(process.stderr, stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        responses: dict[int, dict[str, object]] = {}
        try:
            self._write_message(process, messages[0])
            responses[1] = self._read_response(process, stdout_queue, 1, stderr_lines)
            for message in messages[1:]:
                self._write_message(process, message)
            responses[2] = self._read_response(process, stdout_queue, 2, stderr_lines)
        finally:
            self._close_process(process)
        return responses

    def _write_message(self, process: subprocess.Popen[str], message: dict[str, object]) -> None:
        if process.stdin is None:
            raise RuntimeError("app-server stdin is not available.")
        process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _read_response(
        self,
        process: subprocess.Popen[str],
        stdout_queue: queue.Queue[str],
        request_id: int,
        stderr_lines: list[str],
    ) -> dict[str, object]:
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise RuntimeError(f"app-server response id {request_id} timed out.")
            try:
                line = stdout_queue.get(timeout=min(remaining_seconds, 0.1))
            except queue.Empty:
                if process.poll() is not None:
                    error_text = "".join(stderr_lines).strip()
                    raise RuntimeError(f"app-server exited before response id {request_id}: {error_text}")
                continue
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError("app-server emitted invalid JSON.") from error
            if not isinstance(decoded, dict):
                continue
            response_id = _coerce_optional_int(decoded.get("id"))
            if response_id == request_id:
                return decoded

    def _close_process(self, process: subprocess.Popen[str]) -> None:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)

    def _read_pipe_lines(self, pipe: object, output_queue: queue.Queue[str]) -> None:
        if pipe is None:
            return
        for line in iter(pipe.readline, ""):
            output_queue.put(line)

    def _collect_pipe_lines(self, pipe: object, lines: list[str]) -> None:
        if pipe is None:
            return
        for line in iter(pipe.readline, ""):
            lines.append(line)

    def _response_result(self, responses: dict[int, dict[str, object]], request_id: int) -> dict[str, object]:
        response = responses.get(request_id)
        if response is None:
            raise RuntimeError(f"app-server did not return response id {request_id}.")
        error = response.get("error")
        if error is not None:
            code: int | None = None
            message = str(error)
            if isinstance(error, dict):
                code = _coerce_optional_int(error.get("code"))
                message = str(error.get("message") or message)
            if code in APP_SERVER_UNSUPPORTED_ERROR_CODES or "method not found" in message.lower():
                raise AppServerThreadRequestUnsupported(
                    f"app-server does not support response id {request_id}: {message}"
                )
            raise AppServerThreadRequestRejected(request_id, code, message)
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"app-server response id {request_id} did not include an object result.")
        return result


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


def truncate_first_line(text: str, limit: int) -> str:
    """Return the first non-empty line truncated for table display.

    @param text The source text to shorten.
    @param limit The maximum output length.
    @returns A single-line truncated string.
    """

    for line in text.splitlines():
        first_line = " ".join(line.split())
        if first_line:
            if len(first_line) <= limit:
                return first_line
            return f"{first_line[: max(0, limit - 3)]}..."
    return ""


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

    display_timestamp = raw_timestamp
    if display_timestamp > 10_000_000_000:
        display_timestamp = display_timestamp // 1000

    try:
        display_text = datetime.fromtimestamp(display_timestamp).strftime("%Y-%m-%d %H:%M:%S")
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


def _coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_compact_number(value: int | None) -> str:
    """Format a large integer for compact GUI display.

    @param value The integer value to format.
    @returns A compact decimal string, or N/A when missing.
    """

    if value is None:
        return "N/A"
    absolute_value = abs(value)
    if absolute_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if absolute_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _thread_source_key(raw_thread_source: str) -> str:
    """Return the source key implied by Codex thread_source.

    @param raw_thread_source The stored thread_source value from SQLite.
    @returns A normalized source key, or an empty string when it does not override source.
    """

    thread_source = raw_thread_source.strip().lower()
    if not thread_source or thread_source == "user":
        return ""
    return THREAD_SOURCE_KEYS.get(thread_source, "subagentother")


def _combined_source_key(raw_source: str, raw_thread_source: str = "") -> str:
    """Return the effective Codex source kind key.

    @param raw_source The serialized session source value from SQLite.
    @param raw_thread_source The optional thread_source value from SQLite.
    @returns A lowercase source key suitable for filtering and display.
    """

    thread_source_key = _thread_source_key(raw_thread_source)
    return thread_source_key or _source_key(raw_source)


def _is_subagent_source(raw_source: str, raw_thread_source: str = "") -> bool:
    """Return whether a serialized thread source represents a subagent.

    @param raw_source The serialized source value from SQLite.
    @param raw_thread_source The optional thread_source value from SQLite.
    @returns True when the source is a subagent variant.
    """

    thread_source_key = _thread_source_key(raw_thread_source)
    if thread_source_key.startswith("subagent"):
        return True
    source_text = raw_source.strip()
    if not source_text:
        return False
    source_key = source_text.lower()
    if source_key.startswith("subagent"):
        return True
    if source_key.startswith("internal_") or source_key in {"cli", "vscode", "exec", "mcp", "unknown"}:
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
        if source_key in SUBAGENT_SOURCE_KEYS:
            return SUBAGENT_SOURCE_KEYS[source_key]
        if source_key.startswith("subagent"):
            return "subagent"
        if source_key.startswith("internal_"):
            return "internal"
        if source_key in {"appserver", "app-server", "app_server"}:
            return "mcp"
        return source_key

    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return source_text.lower()
    if not isinstance(parsed, dict):
        return source_text.lower()
    if "subagent" in parsed:
        subagent_kind = str(parsed["subagent"]).strip().lower()
        return SUBAGENT_SOURCE_KEYS.get(f"subagent_{subagent_kind}", "subagent")
    if "custom" in parsed:
        return str(parsed["custom"]).strip().lower()
    if "internal" in parsed:
        return "internal"
    if len(parsed) == 1:
        return next(iter(parsed)).lower()
    return source_text.lower()


def _is_interactive_chat_source(raw_source: str, raw_thread_source: str = "") -> bool:
    """Return whether the source belongs to Codex interactive chat sessions.

    @param raw_source The serialized source value from SQLite.
    @param raw_thread_source The optional thread_source value from SQLite.
    @returns True for sources shown by the Chats view.
    """

    return _combined_source_key(raw_source, raw_thread_source) in INTERACTIVE_CHAT_SOURCES


def _display_source(raw_source: str, raw_thread_source: str = "") -> str:
    """Return a short user-facing source label.

    @param raw_source The serialized source value from SQLite.
    @param raw_thread_source The optional thread_source value from SQLite.
    @returns A compact source label for the table.
    """

    source_key = _combined_source_key(raw_source, raw_thread_source)
    if not source_key:
        return "Unknown"
    return SOURCE_LABELS.get(source_key, source_key[:1].upper() + source_key[1:])


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


class WorkspaceListWidget(QListWidget):
    workspaceOrderChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create a workspace list with constrained internal drag sorting.

        @param parent The optional parent widget.
        @returns None.
        """

        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event: QDropEvent) -> None:
        """Apply an internal drop only when fixed rows stay at the top.

        @param event The drop event to handle.
        @returns None.
        """

        source_rows = {index.row() for index in self.selectedIndexes()}
        if not source_rows or any(row < FIXED_WORKSPACE_ROW_COUNT for row in source_rows):
            event.ignore()
            return

        target_row = self._drop_target_row(event)
        if target_row is None or target_row < FIXED_WORKSPACE_ROW_COUNT:
            event.ignore()
            return

        previous_order = self._workspace_path_order()
        self.blockSignals(True)
        try:
            super().dropEvent(event)
        finally:
            self.blockSignals(False)
        current_order = self._workspace_path_order()
        if current_order != previous_order:
            self.workspaceOrderChanged.emit(current_order)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        """Accept drag moves only for valid insertion targets.

        @param event The drag move event to handle.
        @returns None.
        """

        super().dragMoveEvent(event)
        target_row = self._drop_target_row(event)
        if target_row is None or target_row < FIXED_WORKSPACE_ROW_COUNT:
            event.ignore()

    def _drop_target_row(self, event: QDragMoveEvent | QDropEvent) -> int | None:
        """Return the destination row implied by the current drop indicator.

        @param event The drop event to inspect.
        @returns The target insertion row, or None when the drop is invalid.
        """

        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return self.count()

        row = index.row()
        indicator_position = self.dropIndicatorPosition()
        if indicator_position == QAbstractItemView.DropIndicatorPosition.OnItem:
            return None
        if indicator_position == QAbstractItemView.DropIndicatorPosition.AboveItem:
            return row
        if indicator_position == QAbstractItemView.DropIndicatorPosition.BelowItem:
            return row + 1
        if indicator_position == QAbstractItemView.DropIndicatorPosition.OnViewport:
            return self.count()
        return row

    def _workspace_path_order(self) -> list[str]:
        """Return current custom workspace paths in visible list order.

        @param None.
        @returns The workspace paths below the fixed rows.
        """

        ordered_paths: list[str] = []
        for row in range(FIXED_WORKSPACE_ROW_COUNT, self.count()):
            item = self.item(row)
            path = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
            if isinstance(path, str) and path:
                ordered_paths.append(path)
        return ordered_paths


def load_app_config(config_path: Path, *, recover: bool = True) -> AppConfig:
    """Load the launcher config and create it when missing.

    @param config_path The JSON config file path.
    @param recover Whether to create a default file when the source is missing or invalid.
    @returns The parsed application config.
    """

    if not config_path.exists():
        config = AppConfig()
        if recover:
            save_app_config(config_path, config)
        return config

    try:
        raw_data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        config = AppConfig()
        if recover:
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
        detail_splitter_sizes=_coerce_int_sequence(
            raw_ui_state.get("detail_splitter_sizes"),
            DEFAULT_DETAIL_SPLITTER_SIZES,
        )[:2],
        column_widths=_coerce_int_sequence(raw_ui_state.get("column_widths"), DEFAULT_COLUMN_WIDTHS),
    )
    if ui_state.thread_scope not in {
        THREAD_SCOPE_WORKSPACE,
        THREAD_SCOPE_ALL_WORKSPACES,
        THREAD_SCOPE_OTHER_WORKSPACES,
    }:
        ui_state.thread_scope = THREAD_SCOPE_WORKSPACE
    if ui_state.thread_view != THREAD_VIEW_CHATS:
        ui_state.thread_view = THREAD_VIEW_CHATS
    if len(ui_state.window_size) != 2:
        ui_state.window_size = DEFAULT_WINDOW_SIZE
    if len(ui_state.splitter_sizes) != 2:
        ui_state.splitter_sizes = DEFAULT_SPLITTER_SIZES
    if len(ui_state.detail_splitter_sizes) != 2:
        ui_state.detail_splitter_sizes = DEFAULT_DETAIL_SPLITTER_SIZES

    terminal = str(raw_data.get("terminal", "wt")).strip() or "wt"
    return AppConfig(workspaces=workspaces, terminal=terminal, ui_state=ui_state)


def save_app_config(config_path: Path, config: AppConfig) -> None:
    """Persist the launcher config to disk.

    @param config_path The JSON config file path.
    @param config The configuration to persist.
    @returns None.
    """
    with configuration_lock(config_path):
        _save_app_config_locked(config_path, config)


def _save_app_config_locked(config_path: Path, config: AppConfig) -> None:
    """Persist app configuration while the caller holds the configuration lock.

    @param config_path The JSON config file path.
    @param config The configuration to persist.
    @returns None.
    """

    payload: dict[str, object] = {}
    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    else:
        if isinstance(raw_payload, dict):
            payload = raw_payload

    payload.update(
        {
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
                "detail_splitter_sizes": list(config.ui_state.detail_splitter_sizes),
                "column_widths": list(config.ui_state.column_widths),
            },
        }
    )
    atomic_write_text(
        config_path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


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

    def _connect_state_db(self, read_only: bool) -> sqlite3.Connection:
        """Open the Codex state database with consistent connection settings.

        @param read_only Whether to open the database in read-only mode.
        @returns A configured SQLite connection.
        """

        if not self._database_path.exists():
            raise FileNotFoundError(f"Codex state database was not found: {self._database_path}")

        database_uri = f"file:{self._database_path}?mode=ro" if read_only else f"file:{self._database_path}"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _table_columns(self, connection: sqlite3.Connection, table_name: str) -> set[str]:
        """Return the column names for a SQLite table.

        @param connection The SQLite connection to inspect.
        @param table_name The table whose columns should be returned.
        @returns A set of column names, or an empty set when the table is absent.
        """

        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            if "name" in row.keys()
        }

    def _require_thread_write_schema(self, connection: sqlite3.Connection) -> set[str]:
        """Validate that the threads table supports archive/delete writes.

        @param connection The SQLite connection to inspect.
        @returns The threads table columns.
        """

        columns = self._table_columns(connection, "threads")
        missing_columns = sorted(THREAD_WRITE_REQUIRED_COLUMNS - columns)
        if missing_columns:
            missing_text = ", ".join(missing_columns)
            raise RuntimeError(f"Codex threads table is missing required columns: {missing_text}")
        return columns

    def load_threads(
        self,
        workspace_path: str,
        archived_only: bool,
        thread_scope: str,
        thread_view: str,
        known_workspace_paths: Sequence[str] = (),
    ) -> list[ThreadRecord]:
        """Load threads from Codex app-server.

        @param workspace_path The selected workspace path.
        @param archived_only Whether only archived threads should be shown.
        @param thread_scope The workspace scope to apply.
        @param thread_view The thread view filter to apply.
        @param known_workspace_paths The configured workspace paths used by the Other scope.
        @returns A list of threads sorted by most recent update.
        """

        if thread_scope not in {
            THREAD_SCOPE_WORKSPACE,
            THREAD_SCOPE_ALL_WORKSPACES,
            THREAD_SCOPE_OTHER_WORKSPACES,
        }:
            thread_scope = THREAD_SCOPE_WORKSPACE
        if thread_view != THREAD_VIEW_CHATS:
            thread_view = THREAD_VIEW_CHATS
        normalized_workspace = normalize_workspace_path(workspace_path)
        normalized_known_workspaces = {
            normalize_workspace_path(path_text) for path_text in known_workspace_paths if path_text.strip()
        }
        return self._load_threads_from_app_server(
            archived_only,
            thread_scope,
            thread_view,
            normalized_workspace,
            normalized_known_workspaces,
        )

    def _load_threads_from_app_server(
        self,
        archived_only: bool,
        thread_scope: str,
        thread_view: str,
        normalized_workspace: str,
        normalized_known_workspaces: set[str],
    ) -> list[ThreadRecord]:
        """Load threads through `codex app-server --stdio`.

        @param archived_only Whether only archived threads should be shown.
        @param thread_scope The normalized workspace scope to apply.
        @param thread_view The normalized thread view to apply.
        @param normalized_workspace The selected workspace path normalized for comparison.
        @param normalized_known_workspaces The configured workspace paths normalized for Other scope.
        @returns A list of app-server-backed thread records.
        """

        app_server_threads = AppServerThreadClient(self._codex_home_path).list_threads(archived_only)
        rollout_index = self._build_rollout_index()
        records: list[ThreadRecord] = []
        for thread in app_server_threads:
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                raise RuntimeError("app-server returned a thread without an id.")
            cwd = str(thread.get("cwd") or "")
            if thread_scope == THREAD_SCOPE_WORKSPACE and normalize_workspace_path(cwd) != normalized_workspace:
                continue
            if thread_scope == THREAD_SCOPE_OTHER_WORKSPACES and normalize_workspace_path(cwd) in normalized_known_workspaces:
                continue

            source = (
                json.dumps(thread["source"], ensure_ascii=False)
                if isinstance(thread.get("source"), dict)
                else str(thread.get("source") or "")
            )
            thread_source = str(thread.get("threadSource") or "")
            if thread.get("parentThreadId") is not None or _is_subagent_source(source, thread_source):
                continue
            if thread_view == THREAD_VIEW_CHATS and not _is_interactive_chat_source(source, thread_source):
                continue

            sort_timestamp, updated_text = format_timestamp(thread.get("updatedAt"))
            _created_sort_timestamp, created_text = format_timestamp(thread.get("createdAt"))
            title = truncate_first_line(str(thread.get("name") or "").strip(), TITLE_DISPLAY_LIMIT)
            first_user_message = str(thread.get("preview") or "").strip()
            summary = truncate_text(first_user_message, 160)
            if thread_view == THREAD_VIEW_CHATS and not title and not summary:
                continue
            display_title = title or summary or thread_id
            git_info = thread.get("gitInfo")
            git_info_dict = git_info if isinstance(git_info, dict) else {}
            rollout_path = str(thread.get("path") or "")

            records.append(
                ThreadRecord(
                    thread_id=thread_id,
                    title=display_title,
                    created_at_text=created_text,
                    updated_at_text=updated_text,
                    source=_display_source(source, thread_source),
                    cwd=cwd,
                    model="",
                    model_provider=str(thread.get("modelProvider") or ""),
                    reasoning_effort="",
                    rollout_path=rollout_path,
                    first_user_message=first_user_message,
                    summary=summary,
                    archived=archived_only,
                    rollout_missing=self._rollout_missing(rollout_path, thread_id, archived_only, rollout_index),
                    sort_timestamp=sort_timestamp,
                    tokens_used=0,
                    cli_version=str(thread.get("cliVersion") or ""),
                    git_branch=str(git_info_dict.get("branch") or ""),
                    git_sha=str(git_info_dict.get("sha") or ""),
                )
            )

        records.sort(key=lambda item: (item.sort_timestamp, item.thread_id), reverse=True)
        return records

    def count_orphan_subagent_threads(
        self,
        workspace_path: str,
        archived_only: bool,
        thread_scope: str,
        known_workspace_paths: Sequence[str] = (),
    ) -> int:
        """Count hidden suspected subagent threads without a spawn parent edge.

        @param workspace_path The selected workspace path.
        @param archived_only Whether only archived threads are currently shown.
        @param thread_scope The workspace scope to apply.
        @param known_workspace_paths The configured workspace paths used by the Other scope.
        @returns The number of suspected orphan subagent rows hidden from the normal list.
        """

        if thread_scope not in {
            THREAD_SCOPE_WORKSPACE,
            THREAD_SCOPE_ALL_WORKSPACES,
            THREAD_SCOPE_OTHER_WORKSPACES,
        }:
            thread_scope = THREAD_SCOPE_WORKSPACE
        normalized_workspace = normalize_workspace_path(workspace_path)
        normalized_known_workspaces = {
            normalize_workspace_path(path_text) for path_text in known_workspace_paths if path_text.strip()
        }
        connection = self._connect_state_db(True)
        try:
            columns = self._table_columns(connection, "threads")
            if not columns:
                raise RuntimeError("The threads table is missing or could not be introspected.")
            edge_columns = self._table_columns(connection, "thread_spawn_edges")
            child_thread_ids: set[str] = set()
            if "child_thread_id" in edge_columns:
                child_thread_ids = {
                    str(row["child_thread_id"])
                    for row in connection.execute("SELECT child_thread_id FROM thread_spawn_edges").fetchall()
                    if row["child_thread_id"]
                }
            select_parts = [
                '"id" AS "thread_id"' if "id" in columns else "'' AS thread_id",
                '"cwd" AS "cwd"' if "cwd" in columns else "'' AS cwd",
                '"archived" AS "archived"' if "archived" in columns else "0 AS archived",
                '"source" AS "source"' if "source" in columns else "'' AS source",
                '"thread_source" AS "thread_source"' if "thread_source" in columns else "'' AS thread_source",
            ]
            rows = connection.execute(f"SELECT {', '.join(select_parts)} FROM threads").fetchall()
        finally:
            connection.close()

        orphan_count = 0
        for row in rows:
            thread_id = str(row["thread_id"] or "")
            cwd = str(row["cwd"] or "")
            if thread_scope == THREAD_SCOPE_WORKSPACE and normalize_workspace_path(cwd) != normalized_workspace:
                continue
            if thread_scope == THREAD_SCOPE_OTHER_WORKSPACES and normalize_workspace_path(cwd) in normalized_known_workspaces:
                continue
            archived_flag = bool(int(row["archived"] or 0))
            if archived_flag != archived_only:
                continue
            source = str(row["source"] or "")
            thread_source = str(row["thread_source"] or "")
            if _is_subagent_source(source, thread_source) and thread_id not in child_thread_ids:
                orphan_count += 1
        return orphan_count

    def load_thread_usage_stats(self, thread: ThreadRecord) -> ThreadUsageStats:
        """Load token and activity stats from a thread rollout file.

        @param thread The thread record whose rollout should be inspected.
        @returns Parsed usage stats with missing values left as None.
        """

        stats = ThreadUsageStats()
        if thread.rollout_missing:
            return stats

        try:
            rollout_path = (
                self._resolve_archived_rollout_path(thread.rollout_path, thread.thread_id)
                if thread.archived
                else self._resolve_active_rollout_path(thread.rollout_path, thread.thread_id)
            )
        except FileNotFoundError:
            return stats

        try:
            with rollout_path.open("r", encoding="utf-8", errors="replace") as rollout_file:
                for line in rollout_file:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue

                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    payload_type = str(payload.get("type") or "")

                    if event.get("type") == "event_msg" and payload_type == "token_count":
                        self._apply_token_count_payload(stats, payload)
        except OSError:
            return stats

        return stats

    def _apply_token_count_payload(self, stats: ThreadUsageStats, payload: dict[str, object]) -> None:
        info = payload.get("info")
        if not isinstance(info, dict):
            return

        total_token_usage = info.get("total_token_usage")
        if isinstance(total_token_usage, dict):
            total_tokens = _coerce_optional_int(total_token_usage.get("total_tokens"))
            if total_tokens is not None:
                stats.total_tokens = total_tokens

        last_token_usage = info.get("last_token_usage")
        if isinstance(last_token_usage, dict):
            stats.last_total_tokens = _coerce_optional_int(last_token_usage.get("total_tokens"))
            stats.last_input_tokens = _coerce_optional_int(last_token_usage.get("input_tokens"))
            stats.last_cached_input_tokens = _coerce_optional_int(last_token_usage.get("cached_input_tokens"))

        context_window = _coerce_optional_int(info.get("model_context_window"))
        if context_window is not None:
            stats.model_context_window = context_window

    def unarchive_thread(self, thread_id: str) -> Path:
        """Unarchive a thread through Codex app-server.

        @param thread_id The thread id to unarchive.
        @returns The restored rollout path.
        """

        app_server_thread = AppServerThreadClient(self._codex_home_path).unarchive_thread(thread_id)
        rollout_path_text = str(app_server_thread.get("path") or "")
        return self._resolve_active_rollout_path(rollout_path_text, thread_id)

    def _thread_ids_with_spawn_descendants(self, connection: sqlite3.Connection, thread_ids: Sequence[str]) -> list[str]:
        """Return thread ids plus transitive spawned descendants.

        @param connection The SQLite connection to inspect.
        @param thread_ids The root thread ids.
        @returns Ordered unique thread ids including descendants.
        """

        ordered_ids: list[str] = []
        seen_ids: set[str] = set()
        pending_ids = list(thread_ids)
        has_spawn_edges = self._table_exists(connection, "thread_spawn_edges")
        while pending_ids:
            current_id = pending_ids.pop(0)
            if current_id in seen_ids:
                continue
            seen_ids.add(current_id)
            ordered_ids.append(current_id)
            if not has_spawn_edges:
                continue
            child_rows = connection.execute(
                "SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id = ?",
                (current_id,),
            ).fetchall()
            pending_ids.extend(str(row["child_thread_id"]) for row in child_rows if row["child_thread_id"])
        return ordered_ids

    def _thread_rows_by_id(
        self,
        connection: sqlite3.Connection,
        thread_ids: Sequence[str],
    ) -> dict[str, sqlite3.Row]:
        """Load thread rows keyed by id.

        @param connection The SQLite connection to inspect.
        @param thread_ids The thread ids to load.
        @returns Existing thread rows keyed by id.
        """

        if not thread_ids:
            return {}
        placeholders = ", ".join("?" for _thread_id in thread_ids)
        rows = connection.execute(
            f"SELECT id, rollout_path, archived FROM threads WHERE id IN ({placeholders})",
            tuple(thread_ids),
        ).fetchall()
        return {str(row["id"]): row for row in rows}

    def archive_thread(self, thread_id: str) -> Path:
        """Archive a thread through Codex app-server.

        @param thread_id The thread id to archive.
        @returns The archived rollout path.
        """

        AppServerThreadClient(self._codex_home_path).archive_thread(thread_id)
        return self._resolve_archived_rollout_path("", thread_id)

    def delete_archived_thread(self, thread_id: str) -> None:
        """Delete one archived thread rollout and SQLite index row.

        @param thread_id The archived thread id to delete.
        @returns None.
        """

        self._delete_archived_thread_ids_app_server([thread_id])

    def _delete_archived_thread_ids_app_server(self, thread_ids: Sequence[str]) -> int:
        """Delete archived threads through app-server.

        @param thread_ids The selected archived root thread ids.
        @returns The best available count of deleted thread rows including descendants.
        """

        if not thread_ids:
            return 0

        completed_count = 0
        for thread_id in thread_ids:
            AppServerThreadClient(self._codex_home_path).delete_thread(thread_id)
            completed_count += 1
        return completed_count

    def _count_delete_plan_thread_ids(self, thread_ids: Sequence[str]) -> int:
        """Return the local delete target count without blocking app-server deletion.

        @param thread_ids The selected archived root thread ids.
        @returns The number of local rows that would be deleted, or root count when unavailable.
        """

        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect_state_db(True)
            plan = self._build_delete_plan(connection, thread_ids)
            return len(plan.thread_ids)
        except Exception:
            return len(thread_ids)
        finally:
            if connection is not None:
                connection.close()

    def _build_delete_plan(
        self,
        connection: sqlite3.Connection,
        root_thread_ids: Sequence[str],
    ) -> ThreadDeletePlan:
        """Precheck archived thread deletion including spawned descendants.

        @param connection The SQLite connection to inspect.
        @param root_thread_ids The selected root thread ids.
        @returns A delete plan with thread ids and existing rollout paths.
        """

        thread_ids = self._thread_ids_with_spawn_descendants(connection, root_thread_ids)
        rows_by_id = self._thread_rows_by_id(connection, thread_ids)
        for root_thread_id in root_thread_ids:
            if root_thread_id not in rows_by_id:
                raise RuntimeError(f"Thread was not found: {root_thread_id}")

        delete_ids: list[str] = []
        archived_paths: dict[str, Path] = {}
        for current_id in thread_ids:
            row = rows_by_id.get(current_id)
            if row is None:
                continue
            if not bool(int(row["archived"] or 0)):
                raise RuntimeError("Only archived threads can be deleted.")
            delete_ids.append(current_id)
            try:
                archived_paths[current_id] = self._resolve_archived_rollout_path(
                    str(row["rollout_path"] or ""),
                    current_id,
                )
            except FileNotFoundError:
                pass
        return ThreadDeletePlan(thread_ids=delete_ids, archived_paths=archived_paths)

    def _delete_archived_thread_ids(self, thread_ids: Sequence[str]) -> int:
        """Delete archived threads and spawned descendants after prechecks.

        @param thread_ids The selected archived root thread ids.
        @returns The number of SQLite thread rows deleted.
        """

        connection = self._connect_state_db(False)
        staged_paths: list[tuple[Path, Path]] = []
        committed = False
        try:
            self._require_thread_write_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            plan = self._build_delete_plan(connection, thread_ids)
            for archived_path in plan.archived_paths.values():
                staged_path = self._stage_rollout_for_delete(archived_path)
                staged_paths.append((staged_path, archived_path))

            if plan.thread_ids:
                placeholders = ", ".join("?" for _thread_id in plan.thread_ids)
                connection.execute(
                    f"DELETE FROM threads WHERE id IN ({placeholders})",
                    tuple(plan.thread_ids),
                )
            if self._table_exists(connection, "thread_spawn_edges"):
                placeholders = ", ".join("?" for _thread_id in plan.thread_ids)
                if placeholders:
                    connection.execute(
                        f"""
                        DELETE FROM thread_spawn_edges
                        WHERE parent_thread_id IN ({placeholders})
                           OR child_thread_id IN ({placeholders})
                        """,
                        tuple(plan.thread_ids) + tuple(plan.thread_ids),
                    )
            connection.commit()
            committed = True
            for staged_path, _archived_path in staged_paths:
                try:
                    staged_path.unlink()
                except OSError:
                    pass
            return len(plan.thread_ids)
        except Exception:
            if not committed:
                connection.rollback()
            for staged_path, archived_path in reversed(staged_paths):
                self._move_rollout_back(staged_path, archived_path)
            raise
        finally:
            connection.close()

    def _update_thread_archive_state(
        self,
        connection: sqlite3.Connection,
        columns: set[str],
        thread_id: str,
        rollout_path: Path,
        archived: bool,
        archived_at: int | None,
    ) -> None:
        """Update SQLite archive metadata after a rollout move.

        @param connection The SQLite connection to update.
        @param columns The introspected threads table columns.
        @param thread_id The thread id to update.
        @param rollout_path The new absolute rollout path.
        @param archived Whether the thread is archived.
        @param archived_at The archive timestamp, or None for active threads.
        @returns None.
        """

        modified_at = rollout_path.stat().st_mtime
        assignments = [
            "archived = ?",
            "archived_at = ?",
            "rollout_path = ?",
            "updated_at = ?",
        ]
        values: list[object] = [
            1 if archived else 0,
            archived_at,
            str(rollout_path),
            int(modified_at),
        ]
        if "updated_at_ms" in columns:
            assignments.append("updated_at_ms = ?")
            values.append(int(modified_at * 1000))
        values.append(thread_id)
        connection.execute(
            f"""
            UPDATE threads
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            values,
        )

    def _stage_rollout_for_delete(self, archived_path: Path) -> Path:
        """Move an archived rollout to a local trash staging path.

        @param archived_path The archived rollout path to stage for deletion.
        @returns The staged rollout path.
        """

        trash_root = self._codex_home_path / ".codex-cli-startup-trash"
        trash_root.mkdir(parents=True, exist_ok=True)
        staged_path = trash_root / f"{int(time.time() * 1000)}-{os.getpid()}-{archived_path.name}"
        shutil.move(str(archived_path), str(staged_path))
        return staged_path

    def _move_rollout_back(self, source_path: Path, destination_path: Path) -> None:
        """Best-effort rollback for a file move.

        @param source_path The current rollout path.
        @param destination_path The original rollout path.
        @returns None.
        """

        try:
            if source_path.exists() and not destination_path.exists():
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_path), str(destination_path))
        except OSError:
            pass

    def delete_archived_threads(
        self,
        workspace_path: str,
        thread_scope: str,
        thread_view: str,
        known_workspace_paths: Sequence[str] = (),
    ) -> int:
        """Delete archived threads matching the current workspace and view filters.

        @param workspace_path The selected workspace path.
        @param thread_scope The workspace scope to apply.
        @param thread_view The thread view filter to apply.
        @param known_workspace_paths The configured workspace paths used by the Other scope.
        @returns The number of deleted archived threads.
        """

        records = self.load_threads(workspace_path, True, thread_scope, thread_view, known_workspace_paths)
        return self._delete_archived_thread_ids_app_server([record.thread_id for record in records])

    def delete_missing_rollout_threads(
        self,
        workspace_path: str,
        archived_only: bool,
        thread_scope: str,
        thread_view: str,
        known_workspace_paths: Sequence[str] = (),
    ) -> int:
        """Delete stale SQLite rows whose rollout file no longer exists.

        @param workspace_path The selected workspace path.
        @param archived_only Whether only archived threads are currently shown.
        @param thread_scope The workspace scope to apply.
        @param thread_view The thread view filter to apply.
        @param known_workspace_paths The configured workspace paths used by the Other scope.
        @returns The number of deleted stale rows.
        """

        records = self.load_threads(workspace_path, archived_only, thread_scope, thread_view, known_workspace_paths)
        deleted_count = 0
        for record in records:
            if not record.rollout_missing:
                continue
            if not record.archived and self.thread_has_live_process(record.thread_id):
                continue
            self._delete_thread_index_row(record.thread_id)
            deleted_count += 1
        return deleted_count

    def _resolve_active_rollout_path(
        self,
        rollout_path_text: str,
        thread_id: str,
        rollout_index: dict[tuple[bool, str], list[Path]] | None = None,
    ) -> Path:
        """Resolve the active rollout path for a thread.

        @param rollout_path_text The rollout path stored in SQLite.
        @param thread_id The thread id to locate.
        @param rollout_index Optional per-refresh rollout filename index.
        @returns The active rollout path.
        """

        sessions_root = self._codex_home_path / "sessions"
        stored_candidate = self._stored_rollout_candidate(rollout_path_text, sessions_root, archived=False)
        if stored_candidate is not None and self._rollout_matches_thread(stored_candidate, thread_id, archived=False):
            return stored_candidate
        indexed_candidate = self._rollout_from_index(rollout_index, thread_id, archived=False)
        if indexed_candidate is not None:
            return indexed_candidate
        return self._find_rollout_by_thread_id(sessions_root, thread_id, archived=False)

    def _rollout_missing(
        self,
        rollout_path_text: str,
        thread_id: str,
        archived: bool,
        rollout_index: dict[tuple[bool, str], list[Path]] | None = None,
    ) -> bool:
        """Return whether a thread index row has no matching rollout file.

        @param rollout_path_text The rollout path stored in SQLite.
        @param thread_id The thread id to locate.
        @param archived Whether the thread is marked archived in SQLite.
        @param rollout_index Optional per-refresh rollout filename index.
        @returns True when no matching rollout exists in the expected tree.
        """

        try:
            if archived:
                self._resolve_archived_rollout_path(rollout_path_text, thread_id, rollout_index)
            else:
                self._resolve_active_rollout_path(rollout_path_text, thread_id, rollout_index)
        except FileNotFoundError:
            return True
        return False

    def _stored_rollout_candidate(self, rollout_path_text: str, root: Path, archived: bool) -> Path | None:
        """Resolve a stored SQLite rollout path within the expected root.

        @param rollout_path_text The rollout path stored in SQLite.
        @param root The expected rollout root directory.
        @param archived Whether the candidate should be an archived rollout.
        @returns A candidate path, or None when the stored path is unusable.
        """

        if not rollout_path_text:
            return None
        stored_path = Path(rollout_path_text)
        candidate = stored_path if stored_path.is_absolute() else self._codex_home_path / stored_path
        if not candidate.exists() or not candidate.is_file():
            return None
        if archived:
            if not self._is_direct_child(candidate, root):
                return None
        elif not self._is_active_rollout_location(candidate, root):
            return None
        return candidate

    def _build_rollout_index(self) -> dict[tuple[bool, str], list[Path]]:
        """Build one rollout filename index for the current refresh.

        @param None.
        @returns A mapping from archive state and thread id to rollout paths.
        """

        rollout_index: dict[tuple[bool, str], list[Path]] = {}
        for archived, root in (
            (False, self._codex_home_path / "sessions"),
            (True, self._codex_home_path / "archived_sessions"),
        ):
            if not root.exists():
                continue
            for candidate in self._iter_rollout_candidates(root, archived):
                thread_id = self._thread_id_from_rollout_filename(candidate.name)
                if thread_id:
                    rollout_index.setdefault((archived, thread_id), []).append(candidate)
        return rollout_index

    def _rollout_from_index(
        self,
        rollout_index: dict[tuple[bool, str], list[Path]] | None,
        thread_id: str,
        archived: bool,
    ) -> Path | None:
        """Resolve a rollout from a per-refresh filename index.

        @param rollout_index The index created for the current refresh.
        @param thread_id The thread id to locate.
        @param archived Whether the rollout should be archived.
        @returns The indexed rollout path, or None when absent.
        """

        if rollout_index is None:
            return None
        matches = [
            candidate
            for candidate in rollout_index.get((archived, thread_id.lower()), [])
            if self._rollout_matches_thread(candidate, thread_id, archived)
        ]
        if len(matches) > 1:
            match_text = "\n".join(str(path) for path in matches)
            raise RuntimeError(f"Multiple rollout files match thread {thread_id}:\n{match_text}")
        return matches[0] if matches else None

    def _iter_rollout_candidates(self, root: Path, archived: bool) -> list[Path]:
        """Return rollout candidate files below a rollout root.

        @param root The active or archived rollout root.
        @param archived Whether the root is the archived rollout root.
        @returns Rollout candidate paths for plain and compressed files.
        """

        candidates: list[Path] = []
        for pattern in ROLLOUT_GLOB_PATTERNS:
            iterator = root.glob(pattern) if archived else root.rglob(pattern)
            candidates.extend(iterator)
        return candidates

    def _thread_id_from_rollout_filename(self, file_name: str) -> str | None:
        """Return the thread id encoded in a rollout filename.

        @param file_name The rollout filename to inspect.
        @returns The lowercase thread id, or None when the filename does not match.
        """

        match = ROLLOUT_FILE_PATTERN.match(file_name)
        return match.group(4).lower() if match else None

    def _find_rollout_by_thread_id(self, root: Path, thread_id: str, archived: bool) -> Path:
        """Find a rollout by exact thread id and session metadata.

        @param root The active or archived rollout root.
        @param thread_id The thread id to locate.
        @param archived Whether the root is the archived rollout root.
        @returns The single matching rollout path.
        """

        if not root.exists():
            location = "archived" if archived else "active"
            raise FileNotFoundError(f"{location.title()} rollout was not found for thread: {thread_id}")

        matches = [
            candidate
            for candidate in self._iter_rollout_candidates(root, archived)
            if self._rollout_matches_thread(candidate, thread_id, archived)
        ]
        if len(matches) > 1:
            match_text = "\n".join(str(path) for path in matches)
            raise RuntimeError(f"Multiple rollout files match thread {thread_id}:\n{match_text}")
        if matches:
            return matches[0]

        location = "Archived" if archived else "Active"
        raise FileNotFoundError(f"{location} rollout was not found for thread: {thread_id}")

    def _rollout_matches_thread(self, candidate: Path, thread_id: str, archived: bool) -> bool:
        """Return whether a rollout file exactly belongs to a thread.

        @param candidate The rollout path to inspect.
        @param thread_id The expected thread id.
        @param archived Whether the rollout should be archived.
        @returns True when filename, location, and session metadata match.
        """

        root = self._codex_home_path / ("archived_sessions" if archived else "sessions")
        if not candidate.exists() or not candidate.is_file():
            return False
        if archived:
            if not self._is_direct_child(candidate, root):
                return False
        elif not self._is_active_rollout_location(candidate, root):
            return False
        if not self._rollout_filename_matches_thread(candidate.name, thread_id):
            return False
        return self._rollout_session_meta_matches_thread(candidate, thread_id)

    def _rollout_filename_matches_thread(self, file_name: str, thread_id: str) -> bool:
        """Return whether a rollout filename has the expected thread id suffix.

        @param file_name The rollout filename to inspect.
        @param thread_id The expected thread id.
        @returns True when the filename matches the Codex rollout pattern and id.
        """

        return self._thread_id_from_rollout_filename(file_name) == thread_id.lower()

    def _rollout_session_meta_matches_thread(self, rollout_path: Path, thread_id: str) -> bool:
        """Return whether the rollout head contains matching session metadata.

        @param rollout_path The rollout file to inspect.
        @param thread_id The expected thread id.
        @returns True when the first JSONL item is session metadata for the thread.
        """

        first_line = self._read_rollout_first_line(rollout_path)
        if not first_line:
            return False
        try:
            item = json.loads(first_line)
        except json.JSONDecodeError:
            return False
        if not isinstance(item, dict) or item.get("type") != "session_meta":
            return False
        payload = item.get("payload")
        return isinstance(payload, dict) and str(payload.get("id") or "").lower() == thread_id.lower()

    def _read_rollout_first_line(self, rollout_path: Path) -> str:
        """Read the first JSONL line from a plain or zstandard-compressed rollout.

        @param rollout_path The rollout file path to inspect.
        @returns The first decoded line, or an empty string when it cannot be read safely.
        """

        try:
            if rollout_path.name.lower().endswith(".jsonl.zst"):
                return self._read_zstandard_rollout_first_line(rollout_path)
            with rollout_path.open("r", encoding="utf-8", errors="replace") as rollout_file:
                return rollout_file.readline()
        except OSError:
            return ""

    def _read_zstandard_rollout_first_line(self, rollout_path: Path) -> str:
        """Read the first line from a zstandard-compressed rollout.

        @param rollout_path The compressed rollout file path.
        @returns The first decoded line, or an empty string when decompression fails.
        """

        try:
            import zstandard
        except ImportError:
            return ""

        try:
            with rollout_path.open("rb") as compressed_file:
                reader = zstandard.ZstdDecompressor().stream_reader(compressed_file)
                try:
                    text_reader = io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
                    try:
                        return text_reader.readline()
                    finally:
                        text_reader.close()
                finally:
                    reader.close()
        except (OSError, zstandard.ZstdError):
            return ""

    def thread_has_live_process(self, thread_id: str) -> bool:
        """Return whether Codex logs point to a still-running process for a thread.

        @param thread_id The thread id to inspect in Codex logs.
        @returns True when a logged process_uuid pid is still running.
        """

        return self._live_log_process_state(thread_id) == LIVE_PROCESS_STATE_LIVE

    def _delete_thread_index_row(self, thread_id: str) -> None:
        """Delete one SQLite thread row and its spawn edges.

        @param thread_id The thread id to delete from the SQLite index.
        @returns None.
        """

        connection = self._connect_state_db(False)
        try:
            connection.execute("BEGIN IMMEDIATE")
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

    def _thread_may_be_active_for_archive(self, thread_id: str, rollout_path: Path) -> bool:
        """Return whether a thread should be protected from archive.

        @param thread_id The thread id to test.
        @param rollout_path The active rollout path for the thread.
        @returns True when the thread should be treated as active or unstable.
        """

        if self._live_log_process_state(thread_id) != LIVE_PROCESS_STATE_NOT_LIVE:
            return True
        return self._rollout_recently_modified(rollout_path)

    def _assert_thread_can_archive(self, thread_id: str, rollout_path: Path) -> None:
        """Raise when a thread may still be active.

        @param thread_id The thread id to test.
        @param rollout_path The active rollout path for the thread.
        @returns None.
        """

        live_state = self._live_log_process_state(thread_id)
        if live_state == LIVE_PROCESS_STATE_LIVE:
            raise RuntimeError(ACTIVE_THREAD_ARCHIVE_MESSAGE)
        if live_state == LIVE_PROCESS_STATE_UNKNOWN:
            raise RuntimeError(ACTIVE_THREAD_UNKNOWN_MESSAGE)
        if self._rollout_recently_modified(rollout_path):
            raise RuntimeError(ACTIVE_THREAD_ARCHIVE_MESSAGE)

    def _live_log_process_state(self, thread_id: str) -> str:
        """Return whether recent logs point to a still-running Codex process.

        @param thread_id The thread id to inspect in Codex logs.
        @returns live, not_live, or unknown.
        """

        logs_db_path = resolve_codex_logs_db_path(self._codex_home_path)
        if not logs_db_path.exists():
            return LIVE_PROCESS_STATE_NOT_LIVE

        database_uri = f"file:{logs_db_path}?mode=ro"
        try:
            connection = sqlite3.connect(database_uri, uri=True)
            try:
                connection.execute("PRAGMA busy_timeout = 5000")
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
            return LIVE_PROCESS_STATE_UNKNOWN

        for row in rows:
            process_uuid = str(row[0] or "")
            match = PROCESS_UUID_PATTERN.match(process_uuid)
            if match and self._is_pid_running(int(match.group(1))):
                return LIVE_PROCESS_STATE_LIVE
        return LIVE_PROCESS_STATE_NOT_LIVE

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
        if not self._is_active_rollout_location(active_path, sessions_root):
            raise RuntimeError(f"Active rollout is outside the sessions date tree: {active_path}")

        archived_root = self._codex_home_path / "archived_sessions"
        archived_root.mkdir(parents=True, exist_ok=True)
        archived_path = archived_root / active_path.name
        if archived_path.exists():
            raise FileExistsError(f"Archived rollout already exists: {archived_path}")
        shutil.move(str(active_path), str(archived_path))
        os.utime(archived_path, None)
        return archived_path

    def _resolve_archived_rollout_path(
        self,
        rollout_path_text: str,
        thread_id: str,
        rollout_index: dict[tuple[bool, str], list[Path]] | None = None,
    ) -> Path:
        """Resolve the archived rollout path for a thread.

        @param rollout_path_text The rollout path stored in SQLite.
        @param thread_id The thread id to locate.
        @param rollout_index Optional per-refresh rollout filename index.
        @returns The archived rollout path.
        """

        archived_root = self._codex_home_path / "archived_sessions"
        stored_candidate = self._stored_rollout_candidate(rollout_path_text, archived_root, archived=True)
        if stored_candidate is not None and self._rollout_matches_thread(stored_candidate, thread_id, archived=True):
            return stored_candidate
        indexed_candidate = self._rollout_from_index(rollout_index, thread_id, archived=True)
        if indexed_candidate is not None:
            return indexed_candidate
        return self._find_rollout_by_thread_id(archived_root, thread_id, archived=True)

    def _restore_archived_rollout(self, archived_path: Path) -> Path:
        """Move an archived rollout into the active sessions tree.

        @param archived_path The archived rollout file path.
        @returns The restored rollout file path.
        """

        file_name = archived_path.name
        match = ROLLOUT_FILE_PATTERN.match(file_name)
        if not match:
            raise RuntimeError(f"Archived rollout filename does not contain a session date: {file_name}")

        year, month, day, _thread_uuid = match.groups()
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

    def _is_direct_child(self, path: Path, parent: Path) -> bool:
        """Return whether a path is a direct child of a directory.

        @param path The path to test.
        @param parent The expected parent directory.
        @returns True when the resolved path's parent is the resolved directory.
        """

        try:
            return path.resolve().parent == parent.resolve()
        except OSError:
            return False

    def _is_active_rollout_location(self, path: Path, sessions_root: Path) -> bool:
        """Return whether a path is in sessions/YYYY/MM/DD.

        @param path The rollout path to test.
        @param sessions_root The Codex sessions root directory.
        @returns True when the path is in the active rollout date tree.
        """

        try:
            relative_parent = path.resolve().parent.relative_to(sessions_root.resolve())
        except (OSError, ValueError):
            return False
        parts = relative_parent.parts
        return (
            len(parts) == 3
            and len(parts[0]) == 4
            and len(parts[1]) == 2
            and len(parts[2]) == 2
            and all(part.isdigit() for part in parts)
        )

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
        self._persisted_workspace_snapshot: tuple[tuple[str, str], ...] = tuple(
            (workspace.name, workspace.path) for workspace in self._config.workspaces
        )
        self._repository = ThreadRepository(state_db_path, codex_home_path)
        self._threads: list[ThreadRecord] = []
        self._thread_stats_cache: dict[str, ThreadUsageStats] = {}
        self._missing_rollout_count = 0
        self._orphan_subagent_count = 0

        self.setWindowTitle("codex-cli-startup")
        self.resize(*self._config.ui_state.window_size)

        self._workspace_list = WorkspaceListWidget()
        self._workspace_list.currentRowChanged.connect(self._handle_workspace_changed)
        self._workspace_list.workspaceOrderChanged.connect(self._handle_workspace_reordered)

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

        self._active_threads_button = QPushButton("Active")
        self._archived_toggle = QPushButton("Archived")
        for button in (self._active_threads_button, self._archived_toggle):
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._archive_mode_group = QButtonGroup(self)
        self._archive_mode_group.setExclusive(True)
        self._archive_mode_group.addButton(self._active_threads_button)
        self._archive_mode_group.addButton(self._archived_toggle)
        self._active_threads_button.setChecked(not self._config.ui_state.show_archived)
        self._archived_toggle.setChecked(self._config.ui_state.show_archived)
        self._active_threads_button.clicked.connect(lambda: self._set_archived_view(False))
        self._archived_toggle.clicked.connect(lambda: self._set_archived_view(True))

        archive_mode_picker = QWidget()
        archive_mode_picker.setObjectName("ArchiveModePicker")
        archive_mode_layout = QHBoxLayout(archive_mode_picker)
        archive_mode_layout.setContentsMargins(0, 0, 0, 0)
        archive_mode_layout.setSpacing(0)
        self._active_threads_button.setObjectName("ActiveModeButton")
        self._archived_toggle.setObjectName("ArchivedModeButton")
        archive_mode_layout.addWidget(self._active_threads_button)
        archive_mode_layout.addWidget(self._archived_toggle)
        archive_mode_picker.setStyleSheet(
            """
            #ArchiveModePicker QPushButton {
                background: #2d2d2d;
                border: 1px solid #5f5f5f;
                color: #f0f0f0;
                min-width: 72px;
                padding: 5px 14px;
            }
            #ArchiveModePicker QPushButton:checked {
                background: #55b7e6;
                border-color: #55b7e6;
                color: #101010;
            }
            #ArchiveModePicker QPushButton#ActiveModeButton {
                border-bottom-left-radius: 12px;
                border-right: 0;
                border-top-left-radius: 12px;
            }
            #ArchiveModePicker QPushButton#ArchivedModeButton {
                border-bottom-right-radius: 12px;
                border-top-right-radius: 12px;
            }
            """
        )

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._refresh_threads)

        self._thread_status_label = QLabel("Select a workspace to load threads.")
        self._thread_table = QTableWidget(0, 4)
        self._thread_table.setHorizontalHeaderLabels(["Title", "Cwd", "Updated", "Source"])
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
        self._clean_missing_button = QPushButton("Clean Orphaned")
        self._new_thread_button.clicked.connect(self._start_new_thread)
        self._resume_button.clicked.connect(self._resume_selected_thread)
        self._fork_button.clicked.connect(self._fork_selected_thread)
        self._archive_button.clicked.connect(self._archive_selected_thread)
        self._unarchive_button.clicked.connect(self._unarchive_selected_thread)
        self._delete_thread_button.clicked.connect(self._delete_selected_thread)
        self._delete_archived_button.clicked.connect(self._delete_all_archived_threads)
        self._clean_missing_button.clicked.connect(self._clean_missing_rollout_threads)

        self._detail_panel = QWidget()
        detail_layout = QGridLayout(self._detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(QLabel("Details"), 0, 0, 1, 2)
        self._detail_cwd = QLineEdit()
        self._detail_model = QLineEdit()
        self._detail_thread_id = QLineEdit()
        self._detail_usage = QLineEdit()
        self._detail_time = QLineEdit()
        for line_edit in (
            self._detail_cwd,
            self._detail_model,
            self._detail_thread_id,
            self._detail_usage,
            self._detail_time,
        ):
            line_edit.setReadOnly(True)
        self._detail_first_message = QPlainTextEdit()
        self._detail_first_message.setReadOnly(True)
        self._detail_first_message.setMinimumHeight(80)
        detail_layout.addWidget(QLabel("Cwd"), 1, 0)
        detail_layout.addWidget(self._detail_cwd, 1, 1)
        detail_layout.addWidget(QLabel("Model"), 2, 0)
        detail_layout.addWidget(self._detail_model, 2, 1)
        detail_layout.addWidget(QLabel("Thread ID"), 3, 0)
        detail_layout.addWidget(self._detail_thread_id, 3, 1)
        detail_layout.addWidget(QLabel("Usage"), 4, 0)
        detail_layout.addWidget(self._detail_usage, 4, 1)
        detail_layout.addWidget(QLabel("Time"), 5, 0)
        detail_layout.addWidget(self._detail_time, 5, 1)
        detail_layout.addWidget(QLabel("First User Message"), 6, 0)
        detail_layout.addWidget(self._detail_first_message, 6, 1)
        self._detail_panel.setVisible(False)

        self._thread_detail_splitter = QSplitter(Qt.Orientation.Vertical)
        self._thread_detail_splitter.addWidget(self._thread_table)
        self._thread_detail_splitter.addWidget(self._detail_panel)
        self._thread_detail_splitter.setCollapsible(0, False)
        self._thread_detail_splitter.setCollapsible(1, False)

        right_top = QHBoxLayout()
        right_top.addWidget(archive_mode_picker)
        right_top.addStretch(1)
        right_top.addWidget(self._clean_missing_button)
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
        right_layout.addWidget(self._thread_detail_splitter, 1)
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
        self._thread_detail_splitter.setSizes(list(self._config.ui_state.detail_splitter_sizes))
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
        all_item.setFlags(all_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled & ~Qt.ItemFlag.ItemIsDropEnabled)
        self._workspace_list.addItem(all_item)
        if (
            self._config.ui_state.selected_workspace == ALL_WORKSPACES_SELECTION
            or self._config.ui_state.thread_scope == THREAD_SCOPE_ALL_WORKSPACES
        ):
            selected_row = 0

        other_item = QListWidgetItem("Other")
        other_item.setToolTip("Show chats whose cwd is not in the configured workspace list")
        other_item.setData(Qt.ItemDataRole.UserRole, OTHER_WORKSPACES_SELECTION)
        other_item.setFlags(other_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled & ~Qt.ItemFlag.ItemIsDropEnabled)
        self._workspace_list.addItem(other_item)
        if (
            self._config.ui_state.selected_workspace == OTHER_WORKSPACES_SELECTION
            or self._config.ui_state.thread_scope == THREAD_SCOPE_OTHER_WORKSPACES
        ):
            selected_row = 1

        for index, workspace in enumerate(self._config.workspaces):
            item = QListWidgetItem(workspace.name)
            item.setToolTip(workspace.path)
            item.setData(Qt.ItemDataRole.UserRole, workspace.path)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            self._workspace_list.addItem(item)
            if (
                self._config.ui_state.thread_scope == THREAD_SCOPE_WORKSPACE
                and normalize_workspace_path(workspace.path) == selected_normalized
            ):
                selected_row = index + 2

        self._workspace_list.setCurrentRow(selected_row)

    def _selected_workspace(self) -> WorkspaceEntry | None:
        current_row = self._workspace_list.currentRow()
        if current_row <= 1:
            return None
        workspace_index = current_row - 2
        if workspace_index >= len(self._config.workspaces):
            return None
        return self._config.workspaces[workspace_index]

    def _is_all_workspaces_selected(self) -> bool:
        current_item = self._workspace_list.currentItem()
        return current_item is not None and current_item.data(Qt.ItemDataRole.UserRole) == ALL_WORKSPACES_SELECTION

    def _is_other_workspaces_selected(self) -> bool:
        current_item = self._workspace_list.currentItem()
        return current_item is not None and current_item.data(Qt.ItemDataRole.UserRole) == OTHER_WORKSPACES_SELECTION

    def _add_workspace(self) -> None:
        dialog = WorkspaceDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        workspace = dialog.workspace_entry()
        self._upsert_workspace(workspace, None)

    def _edit_workspace(self) -> None:
        current_row = self._workspace_list.currentRow()
        if current_row <= 1:
            QMessageBox.information(self, "Edit Workspace", "Please select a workspace first.")
            return

        editing_index = current_row - 2
        dialog = WorkspaceDialog(self, self._config.workspaces[editing_index])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        workspace = dialog.workspace_entry()
        self._upsert_workspace(workspace, editing_index)

    def _delete_workspace(self) -> None:
        current_row = self._workspace_list.currentRow()
        workspace = self._selected_workspace()
        if current_row <= 1 or workspace is None:
            QMessageBox.information(self, "Delete Workspace", "Please select a workspace first.")
            return

        confirmation = QMessageBox.question(
            self,
            "Delete Workspace",
            f"Remove '{workspace.name}' from the launcher list?",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        del self._config.workspaces[current_row - 2]
        if not self._config.workspaces:
            self._config.ui_state.selected_workspace = ALL_WORKSPACES_SELECTION
            self._config.ui_state.thread_scope = THREAD_SCOPE_ALL_WORKSPACES
        if not self._persist_ui_state(workspaces_changed=True):
            self._populate_workspace_list()
            return
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
            target_index = len(self._config.workspaces) + 1
        else:
            self._config.workspaces[editing_index] = workspace
            target_index = editing_index + 2

        self._config.ui_state.selected_workspace = workspace.path
        self._config.ui_state.thread_scope = THREAD_SCOPE_WORKSPACE
        if not self._persist_ui_state(workspaces_changed=True):
            self._populate_workspace_list()
            return
        self._populate_workspace_list()
        self._workspace_list.setCurrentRow(target_index)

    def _handle_workspace_changed(self, _row: int) -> None:
        workspace = self._selected_workspace()
        if self._is_all_workspaces_selected():
            self._config.ui_state.selected_workspace = ALL_WORKSPACES_SELECTION
            self._config.ui_state.thread_scope = THREAD_SCOPE_ALL_WORKSPACES
        elif self._is_other_workspaces_selected():
            self._config.ui_state.selected_workspace = OTHER_WORKSPACES_SELECTION
            self._config.ui_state.thread_scope = THREAD_SCOPE_OTHER_WORKSPACES
        else:
            self._config.ui_state.selected_workspace = workspace.path if workspace else ""
            self._config.ui_state.thread_scope = THREAD_SCOPE_WORKSPACE
        self._persist_ui_state()
        self._refresh_threads()

    def _handle_workspace_reordered(self, ordered_paths: list[str]) -> None:
        remaining_workspaces = list(self._config.workspaces)
        reordered_workspaces: list[WorkspaceEntry] = []

        for path in ordered_paths:
            normalized_path = normalize_workspace_path(path)
            matching_index = next(
                (
                    index
                    for index, workspace in enumerate(remaining_workspaces)
                    if normalize_workspace_path(workspace.path) == normalized_path
                ),
                None,
            )
            if matching_index is None:
                continue
            reordered_workspaces.append(remaining_workspaces.pop(matching_index))

        if remaining_workspaces:
            reordered_workspaces.extend(remaining_workspaces)
        if not reordered_workspaces:
            return

        self._config.workspaces = reordered_workspaces
        if not self._persist_ui_state(workspaces_changed=True):
            self._populate_workspace_list()
            return
        self._update_action_state()

    def _set_archived_view(self, archived: bool) -> None:
        self._active_threads_button.setChecked(not archived)
        self._archived_toggle.setChecked(archived)
        self._config.ui_state.show_archived = archived
        self._persist_ui_state()
        self._refresh_threads()

    def _current_thread_scope(self) -> str:
        if self._is_all_workspaces_selected():
            return THREAD_SCOPE_ALL_WORKSPACES
        if self._is_other_workspaces_selected():
            return THREAD_SCOPE_OTHER_WORKSPACES
        return THREAD_SCOPE_WORKSPACE

    def _current_thread_view(self) -> str:
        return THREAD_VIEW_CHATS

    def _configured_workspace_paths(self) -> list[str]:
        return [workspace.path for workspace in self._config.workspaces]

    def _refresh_threads(self) -> None:
        workspace = self._selected_workspace()
        thread_scope = self._current_thread_scope()
        thread_view = self._current_thread_view()
        self._thread_stats_cache.clear()
        if workspace is None and thread_scope == THREAD_SCOPE_WORKSPACE:
            self._threads = []
            self._missing_rollout_count = 0
            self._orphan_subagent_count = 0
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
                self._configured_workspace_paths(),
            )
            self._orphan_subagent_count = self._repository.count_orphan_subagent_threads(
                workspace.path if workspace else "",
                self._archived_toggle.isChecked(),
                thread_scope,
                self._configured_workspace_paths(),
            )
        except Exception as error:  # noqa: BLE001
            self._threads = []
            self._missing_rollout_count = 0
            self._orphan_subagent_count = 0
            self._thread_table.setRowCount(0)
            self._thread_status_label.setText("Failed to load threads.")
            self._update_action_state()
            QMessageBox.critical(self, "Thread Load Failed", str(error))
            return

        self._thread_table.setRowCount(len(self._threads))
        missing_rollout_count = 0
        for row_index, thread in enumerate(self._threads):
            if thread.rollout_missing:
                missing_rollout_count += 1
            values = [
                thread.title,
                strip_windows_verbatim_prefix(thread.cwd),
                thread.updated_at_text,
                thread.source,
            ]
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if thread.rollout_missing:
                    item.setToolTip(f"{value}\n\nRollout file is missing. This stale thread record is not actionable.")
                    item.setBackground(QBrush(QColor("#303030")))
                    item.setForeground(QBrush(QColor("#8a8a8a")))
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
                if column_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, thread.thread_id)
                self._thread_table.setItem(row_index, column_index, item)

        if thread_scope == THREAD_SCOPE_ALL_WORKSPACES:
            scope_text = "All Workspaces"
            workspace_text = "All"
        elif thread_scope == THREAD_SCOPE_OTHER_WORKSPACES:
            scope_text = "Other"
            workspace_text = "Other"
        else:
            scope_text = "Workspace"
            workspace_text = workspace.path if workspace else "(none)"
        archived_text = "only" if self._archived_toggle.isChecked() else "hidden"
        self._missing_rollout_count = missing_rollout_count
        missing_text = f" | Missing rollout: {missing_rollout_count}" if missing_rollout_count else ""
        orphan_text = (
            f" | Hidden orphan subagents: {self._orphan_subagent_count}"
            if self._orphan_subagent_count
            else ""
        )
        self._thread_status_label.setText(
            f"Threads: {len(self._threads)} | Scope: {scope_text} | "
            f"Archived: {archived_text}{missing_text}{orphan_text} | "
            f"Workspace: {workspace_text} | DB: {self._repository.database_path}"
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
        actionable_thread_selected = thread_selected and not thread.rollout_missing
        archived_selected = thread is not None and thread.archived and not thread.rollout_missing
        archived_view_active = self._archived_toggle.isChecked()
        active_thread_selected = actionable_thread_selected and not thread.archived and not archived_view_active

        self._new_thread_button.setVisible(workspace_selected)
        self._new_thread_button.setEnabled(workspace_selected)

        clean_missing_visible = self._missing_rollout_count > 0
        self._clean_missing_button.setVisible(clean_missing_visible)
        self._clean_missing_button.setEnabled(clean_missing_visible)

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
        if thread is None or thread.rollout_missing:
            self._detail_panel.setVisible(False)
            self._detail_cwd.clear()
            self._detail_model.clear()
            self._detail_thread_id.clear()
            self._detail_usage.clear()
            self._detail_time.clear()
            self._detail_first_message.clear()
            return

        usage_stats = self._thread_stats_cache.get(thread.thread_id)
        if usage_stats is None:
            usage_stats = self._repository.load_thread_usage_stats(thread)
            self._thread_stats_cache[thread.thread_id] = usage_stats

        self._detail_cwd.setText(strip_windows_verbatim_prefix(thread.cwd))
        self._detail_model.setText(self._format_model_detail(thread))
        self._detail_thread_id.setText(thread.thread_id)
        self._detail_usage.setText(self._format_usage_detail(usage_stats))
        self._detail_time.setText(self._format_time_detail(thread))
        self._detail_first_message.setPlainText(thread.first_user_message)
        self._detail_panel.setVisible(True)

    def _format_model_detail(self, thread: ThreadRecord) -> str:
        model_parts = [thread.model or "N/A"]
        if thread.reasoning_effort:
            model_parts.append(f"effort: {thread.reasoning_effort}")
        if thread.model_provider:
            model_parts.append(f"provider: {thread.model_provider}")
        return " | ".join(model_parts)

    def _format_usage_detail(self, usage_stats: ThreadUsageStats) -> str:
        if usage_stats.total_tokens is None and usage_stats.last_total_tokens is None:
            return "N/A"

        context_text = "Context N/A"
        if usage_stats.last_input_tokens is not None and usage_stats.model_context_window:
            context_percent = usage_stats.last_input_tokens / usage_stats.model_context_window * 100
            context_text = (
                f"Context {context_percent:.1f}% of {format_compact_number(usage_stats.model_context_window)}"
            )

        return (
            f"Total {format_compact_number(usage_stats.total_tokens)} | "
            f"Last {format_compact_number(usage_stats.last_total_tokens)} | "
            f"{context_text} | Cached {format_compact_number(usage_stats.last_cached_input_tokens)}"
        )

    def _format_time_detail(self, thread: ThreadRecord) -> str:
        created_text = thread.created_at_text or "N/A"
        updated_text = thread.updated_at_text or "N/A"
        return f"Created {created_text} | Updated {updated_text}"

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
            f"Archive this thread?\n\n{thread.title}\n{thread.thread_id}{self._orphan_subagent_notice()}",
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
            f"Delete this archived thread permanently?\n\n{thread.title}\n{thread.thread_id}"
            f"{self._orphan_subagent_notice()}",
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
                "Switch to Archived before deleting archived threads.",
            )
            return
        if not self._threads:
            QMessageBox.information(self, "Delete All Archived", "There are no archived threads to delete.")
            return

        workspace = self._selected_workspace()
        thread_scope = self._current_thread_scope()
        if thread_scope == THREAD_SCOPE_ALL_WORKSPACES:
            scope_text = "All"
        elif thread_scope == THREAD_SCOPE_OTHER_WORKSPACES:
            scope_text = "Other"
        else:
            scope_text = workspace.name if workspace else "Workspace"
        confirmation = QMessageBox.question(
            self,
            "Delete All Archived",
            f"Delete all {len(self._threads)} archived threads in {scope_text} permanently?"
            f"{self._orphan_subagent_notice()}",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        try:
            deleted_count = self._repository.delete_archived_threads(
                workspace.path if workspace else "",
                thread_scope,
                self._current_thread_view(),
                self._configured_workspace_paths(),
            )
        except Exception as error:  # noqa: BLE001
            QMessageBox.critical(self, "Delete All Archived Failed", str(error))
            return

        QMessageBox.information(
            self,
            "Delete All Archived",
            f"Deleted {deleted_count} archived threads.{self._orphan_subagent_notice()}",
        )
        self._refresh_threads()

    def _clean_missing_rollout_threads(self) -> None:
        if self._missing_rollout_count <= 0:
            QMessageBox.information(self, "Clean Orphaned", "There are no orphaned thread records to clean.")
            return

        workspace = self._selected_workspace()
        thread_scope = self._current_thread_scope()
        if thread_scope == THREAD_SCOPE_ALL_WORKSPACES:
            scope_text = "All"
        elif thread_scope == THREAD_SCOPE_OTHER_WORKSPACES:
            scope_text = "Other"
        else:
            scope_text = workspace.name if workspace else "Workspace"
        confirmation = QMessageBox.question(
            self,
            "Clean Orphaned",
            f"Clean {self._missing_rollout_count} orphaned thread records in {scope_text}?\n\n"
            "Only SQLite index rows with missing rollout files will be removed."
            f"{self._orphan_subagent_notice()}",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        try:
            deleted_count = self._repository.delete_missing_rollout_threads(
                workspace.path if workspace else "",
                self._archived_toggle.isChecked(),
                thread_scope,
                self._current_thread_view(),
                self._configured_workspace_paths(),
            )
        except Exception as error:  # noqa: BLE001
            QMessageBox.critical(self, "Clean Orphaned Failed", str(error))
            return

        QMessageBox.information(
            self,
            "Clean Orphaned",
            f"Cleaned {deleted_count} orphaned thread records.{self._orphan_subagent_notice()}",
        )
        self._refresh_threads()

    def _orphan_subagent_notice(self) -> str:
        """Return UI text describing hidden orphan subagents in the current scope.

        @param None.
        @returns A leading-newline notice, or an empty string when there are no hidden orphan subagents.
        """

        if self._orphan_subagent_count <= 0:
            return ""
        noun = "thread" if self._orphan_subagent_count == 1 else "threads"
        verb = "is" if self._orphan_subagent_count == 1 else "are"
        return (
            f"\n\n{self._orphan_subagent_count} orphan subagent {noun} "
            f"{verb} hidden and will not be changed automatically."
        )

    def _validated_selected_thread(self, dialog_title: str, allow_archived: bool = False) -> ThreadRecord | None:
        thread = self._selected_thread()
        if thread is None:
            QMessageBox.information(self, dialog_title, "Please select a thread first.")
            return None
        if not thread.thread_id:
            QMessageBox.warning(self, dialog_title, "The selected thread does not have a valid thread id.")
            return None
        if thread.rollout_missing:
            QMessageBox.warning(
                self,
                dialog_title,
                "The selected thread rollout file is missing, so this stale record cannot be used.",
            )
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

    def _find_command_source(self, command_name: str, shell_path: str) -> str | None:
        """Find a command as resolved by the launch shell.

        @param command_name The command name to find.
        @param shell_path The PowerShell executable used for the launch.
        @returns The command source reported by PowerShell, or None.
        """

        try:
            completed = subprocess.run(  # noqa: S603
                [
                    shell_path,
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

    def _persist_ui_state(self, *, workspaces_changed: bool = False) -> bool:
        """Persist current UI state without overwriting external workspace changes.

        @param workspaces_changed: Whether this save includes a local workspace mutation.
        @returns: True when saved, or False when an external workspace conflict was detected.
        """
        conflict_detected = False
        external_workspaces_changed = False
        with configuration_lock(self._config_path):
            disk_config = (
                load_app_config(self._config_path, recover=False)
                if self._config_path.exists()
                else AppConfig()
            )
            disk_workspace_snapshot = tuple(
                (workspace.name, workspace.path) for workspace in disk_config.workspaces
            )
            external_workspaces_changed = (
                disk_workspace_snapshot != self._persisted_workspace_snapshot
            )
            if workspaces_changed and external_workspaces_changed:
                self._config.workspaces = disk_config.workspaces
                self._persisted_workspace_snapshot = disk_workspace_snapshot
                conflict_detected = True
            else:
                if not workspaces_changed:
                    self._config.workspaces = disk_config.workspaces

                self._config.terminal = "wt"
                self._config.ui_state.window_size = (self.width(), self.height())
                self._config.ui_state.show_archived = self._archived_toggle.isChecked()
                self._config.ui_state.show_subagents = False
                self._config.ui_state.thread_scope = self._current_thread_scope()
                self._config.ui_state.thread_view = self._current_thread_view()
                self._config.ui_state.splitter_sizes = tuple(self._splitter.sizes()[:2])
                detail_splitter_sizes = tuple(self._thread_detail_splitter.sizes()[:2])
                if (
                    self._detail_panel.isVisible()
                    and len(detail_splitter_sizes) == 2
                    and detail_splitter_sizes[1] > 0
                ):
                    self._config.ui_state.detail_splitter_sizes = detail_splitter_sizes
                self._config.ui_state.column_widths = tuple(
                    self._thread_table.columnWidth(index)
                    for index in range(self._thread_table.columnCount())
                )
                _save_app_config_locked(self._config_path, self._config)
                self._persisted_workspace_snapshot = tuple(
                    (workspace.name, workspace.path) for workspace in self._config.workspaces
                )

        if external_workspaces_changed:
            signals_were_blocked = self._workspace_list.blockSignals(True)
            try:
                self._populate_workspace_list()
            finally:
                self._workspace_list.blockSignals(signals_were_blocked)
            self._update_action_state()

        if conflict_detected:
            QMessageBox.warning(
                self,
                "Workspace Configuration Changed",
                "Workspaces changed in another process. The latest configuration was reloaded.",
            )
            return False
        return True


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
    try:
        config_path = resolve_config_path()
    except ConfigPathError as error:
        QMessageBox.critical(None, "Configuration Error", str(error))
        return 1
    window = MainWindow(config_path, resolve_codex_state_db_path(), resolve_codex_home_path())
    if APP_ICON_PATH.exists():
        window.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
