from __future__ import annotations

import argparse
import json
import ntpath
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Sequence, TextIO, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from config_paths import ConfigPathError, configuration_lock, resolve_config_path


class ProjectSelectionError(ValueError):
    """Describe a configuration or project selection error."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """Represent one configured Codex workspace."""

    name: str
    path: str


def normalize_windows_path(path: str) -> str:
    """Normalize a Windows path for comparison.

    @param path: Path text to normalize.
    @returns: A normalized, case-insensitive comparison key.
    """
    return ntpath.normcase(ntpath.normpath(path.strip()))


def load_workspaces(config_path: Path) -> list[Workspace]:
    """Load and validate workspaces from a launcher configuration file.

    @param config_path: JSON configuration file to read.
    @returns: Workspaces in configuration order.
    """
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            raw_data = cast(object, json.load(config_file))
    except FileNotFoundError as exc:
        raise ProjectSelectionError(f"Configuration file was not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectSelectionError(
            f"Configuration file contains invalid JSON at line {exc.lineno}, column {exc.colno}: {config_path}"
        ) from exc
    except OSError as exc:
        raise ProjectSelectionError(f"Unable to read configuration file {config_path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise ProjectSelectionError("Configuration root must be a JSON object.")
    data = cast(dict[str, object], raw_data)

    raw_workspace_value = data.get("workspaces")
    if not isinstance(raw_workspace_value, list):
        raise ProjectSelectionError("Configuration field 'workspaces' must be an array.")
    raw_workspaces = cast(list[object], raw_workspace_value)
    if not raw_workspaces:
        raise ProjectSelectionError("No workspaces are configured.")

    workspaces: list[Workspace] = []
    for index, raw_workspace in enumerate(raw_workspaces, start=1):
        if not isinstance(raw_workspace, dict):
            raise ProjectSelectionError(f"Workspace {index} must be a JSON object.")
        workspace_data = cast(dict[str, object], raw_workspace)
        name = workspace_data.get("name")
        path = workspace_data.get("path")
        if not isinstance(name, str) or not name.strip():
            raise ProjectSelectionError(f"Workspace {index} has an invalid or empty 'name'.")
        if not isinstance(path, str) or not path.strip():
            raise ProjectSelectionError(f"Workspace {index} has an invalid or empty 'path'.")
        workspaces.append(Workspace(name=name, path=path))

    return workspaces


def load_optional_workspaces(config_path: Path) -> list[Workspace]:
    """Load workspaces while accepting a missing or empty configuration.

    @param config_path: JSON configuration file to read.
    @returns: Valid configured workspaces, or an empty list when none exist.
    """
    try:
        return load_workspaces(config_path)
    except ProjectSelectionError as exc:
        if not config_path.exists() or str(exc) == "No workspaces are configured.":
            return []
        raise


def save_workspaces(
    config_path: Path,
    workspaces: Sequence[Workspace],
    *,
    expected_workspaces: Sequence[Workspace] | None = None,
) -> None:
    """Atomically replace workspaces while preserving other configuration fields.

    @param config_path: JSON configuration file to update.
    @param workspaces: Workspaces to persist in display order.
    @param expected_workspaces: Optional prior state required before writing.
    @returns: None.
    """
    with configuration_lock(config_path):
        _save_workspaces_locked(config_path, workspaces, expected_workspaces)


def _save_workspaces_locked(
    config_path: Path,
    workspaces: Sequence[Workspace],
    expected_workspaces: Sequence[Workspace] | None,
) -> None:
    data: dict[str, object]
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            raw_data = cast(object, json.load(config_file))
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as exc:
        raise ProjectSelectionError(
            f"Configuration file contains invalid JSON at line {exc.lineno}, column {exc.colno}: {config_path}"
        ) from exc
    except OSError as exc:
        raise ProjectSelectionError(f"Unable to read configuration file {config_path}: {exc}") from exc
    else:
        if not isinstance(raw_data, dict):
            raise ProjectSelectionError("Configuration root must be a JSON object.")
        data = cast(dict[str, object], raw_data)

    if expected_workspaces is not None:
        expected_payload = [
            {"name": workspace.name, "path": workspace.path}
            for workspace in expected_workspaces
        ]
        existing_payload = data.get("workspaces", [])
        if existing_payload != expected_payload:
            raise ProjectSelectionError(
                "Workspace configuration changed in another process. Reopen Manage and try again."
            )

    data["workspaces"] = [
        {"name": workspace.name, "path": workspace.path} for workspace in workspaces
    ]
    temporary_path: Path | None = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(data, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(config_path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ProjectSelectionError(f"Unable to write configuration file {config_path}: {exc}") from exc


def select_workspace(
    workspaces: Sequence[Workspace],
    query: str,
    *,
    allow_index: bool = False,
) -> Workspace:
    """Select a workspace by exact name, configured path, or optional index.

    @param workspaces: Configured workspaces in display order.
    @param query: User-supplied selection text.
    @param allow_index: Whether a one-based numeric index is accepted.
    @returns: The selected workspace.
    """
    selection = query.strip()
    if not selection:
        raise ProjectSelectionError("Selection was cancelled.")

    if allow_index and selection.isdecimal():
        index = int(selection)
        if 1 <= index <= len(workspaces):
            return workspaces[index - 1]
        raise ProjectSelectionError(f"Project number must be between 1 and {len(workspaces)}.")

    name_matches = [workspace for workspace in workspaces if workspace.name.casefold() == selection.casefold()]
    if len(name_matches) > 1:
        raise ProjectSelectionError(
            f"Project name is ambiguous: {selection!r}. Select it by number or exact configured path."
        )
    if name_matches:
        return name_matches[0]

    normalized_selection = normalize_windows_path(selection)
    path_matches = [
        workspace
        for workspace in workspaces
        if normalize_windows_path(workspace.path) == normalized_selection
    ]
    if path_matches:
        return path_matches[0]

    raise ProjectSelectionError(f"No configured project matches: {selection!r}.")


def ensure_workspace_exists(workspace: Workspace) -> None:
    """Verify that a selected workspace path is an existing directory.

    @param workspace: Workspace whose configured path must exist.
    @returns: None.
    """
    workspace_path = Path(workspace.path)
    if not workspace_path.exists():
        raise ProjectSelectionError(f"Project path does not exist: {workspace.path}")
    if not workspace_path.is_dir():
        raise ProjectSelectionError(f"Project path is not a directory: {workspace.path}")


def filter_workspaces(workspaces: Sequence[Workspace], query: str) -> list[Workspace]:
    """Filter workspaces by a case-insensitive name or path fragment.

    @param workspaces: Configured workspaces in display order.
    @param query: Search text to match against workspace names and paths.
    @returns: Matching workspaces in their original order.
    """
    search_text = query.strip().casefold()
    if not search_text:
        return list(workspaces)
    path_search_text = search_text.replace("/", "\\")
    return [
        workspace
        for workspace in workspaces
        if search_text in workspace.name.casefold()
        or path_search_text in workspace.path.casefold().replace("/", "\\")
    ]


def _project_option(workspace: Workspace, index: int) -> Option:
    workspace_path = Path(workspace.path)
    path_is_directory = workspace_path.is_dir()
    prompt = Text(workspace.name, style="bold")
    prompt.append("\n")
    prompt.append(workspace.path, style="dim" if path_is_directory else "bold red")
    if not workspace_path.exists():
        prompt.append("  [PATH NOT FOUND]", style="bold red")
    elif not path_is_directory:
        prompt.append("  [NOT A DIRECTORY]", style="bold red")
    return Option(prompt, id=f"project-{index}")


class WorkspaceNameModal(ModalScreen[str | None]):
    """Prompt for a non-empty workspace display name."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "accept_name", "Save", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]
    CSS: ClassVar[str] = """
    WorkspaceNameModal {
        align: center middle;
        background: $background 60%;
    }

    #name-dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #name-prompt {
        margin-bottom: 1;
        text-style: bold;
    }

    #name-error {
        height: 1;
        margin-top: 1;
        color: $error;
    }
    """

    def __init__(self, prompt: str, initial_name: str) -> None:
        """Initialize the display-name prompt.

        @param prompt: Prompt shown above the input.
        @param initial_name: Initial input value.
        @returns: None.
        """
        super().__init__()
        self.prompt = prompt
        self.initial_name = initial_name

    def compose(self) -> ComposeResult:
        """Compose the name dialog.

        @param None.
        @returns: Widgets that make up the dialog.
        """
        with Vertical(id="name-dialog"):
            yield Label(self.prompt, id="name-prompt")
            yield Input(value=self.initial_name, id="workspace-name")
            yield Static("", id="name-error")

    def on_mount(self) -> None:
        """Focus and select the display name.

        @param None.
        @returns: None.
        """
        name_input = self.query_one("#workspace-name", Input)
        name_input.focus()
        name_input.action_select_all()

    def action_accept_name(self) -> None:
        """Return the entered non-empty name.

        @param None.
        @returns: None.
        """
        name = self.query_one("#workspace-name", Input).value.strip()
        if not name:
            self.query_one("#name-error", Static).update("Display name cannot be empty.")
            self.app.bell()
            return
        self.dismiss(name)

    @on(Input.Submitted, "#workspace-name")
    def on_input_submitted(self, _event: Input.Submitted) -> None:
        """Submit the dialog from the focused input.

        @param _event: Input submission notification.
        @returns: None.
        """
        self.action_accept_name()

    def action_cancel(self) -> None:
        """Close the dialog without a name.

        @param None.
        @returns: None.
        """
        self.dismiss(None)


class DeleteWorkspaceModal(ModalScreen[bool]):
    """Ask the user to confirm workspace removal."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "Yes", priority=True),
        Binding("n", "cancel", "No", priority=True),
        Binding("escape", "cancel", "No", priority=True),
    ]
    CSS: ClassVar[str] = """
    DeleteWorkspaceModal {
        align: center middle;
        background: $background 60%;
    }

    #delete-dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }
    """

    def __init__(self, workspace: Workspace) -> None:
        """Initialize the removal confirmation.

        @param workspace: Workspace proposed for removal.
        @returns: None.
        """
        super().__init__()
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        """Compose the confirmation dialog.

        @param None.
        @returns: Widgets that make up the dialog.
        """
        with Vertical(id="delete-dialog"):
            yield Label(f"Remove {self.workspace.name}?", id="delete-prompt")
            yield Static("Press Y to remove, or N/Esc to keep it.")

    def action_confirm(self) -> None:
        """Confirm removal.

        @param None.
        @returns: None.
        """
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Cancel removal.

        @param None.
        @returns: None.
        """
        self.dismiss(False)


class WorkspaceManagementScreen(Screen[tuple[Workspace, ...]]):
    """Manage configured workspaces without leaving the selector."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "add_workspace", "Add", priority=True),
        Binding("r", "rename_workspace", "Rename", priority=True),
        Binding("t", "move_workspace_to_top", "Move to top", priority=True),
        Binding("d", "delete_workspace", "Remove", priority=True),
        Binding("escape", "close", "Back", priority=True),
    ]
    CSS: ClassVar[str] = """
    #manage-content {
        height: 1fr;
        padding: 1 2;
    }

    #manage-title {
        height: 2;
        text-style: bold;
        color: $accent;
    }

    #manage-projects {
        height: 1fr;
        border: round $primary;
    }

    #manage-status {
        height: 3;
        padding: 0 2;
        border-top: solid $primary;
    }

    #manage-status.error {
        color: $error;
        text-style: bold;
    }
    """

    def __init__(
        self,
        config_path: Path,
        workspaces: Sequence[Workspace],
        launch_directory: Path,
    ) -> None:
        """Initialize workspace management.

        @param config_path: Launcher configuration file to update.
        @param workspaces: Currently configured workspaces.
        @param launch_directory: CLI startup directory used by Add.
        @returns: None.
        """
        super().__init__()
        self.config_path = config_path
        self.workspaces: list[Workspace] = list(workspaces)
        self.launch_directory = launch_directory
        self._rename_index: int | None = None
        self._delete_index: int | None = None

    def compose(self) -> ComposeResult:
        """Compose the management screen.

        @param None.
        @returns: Widgets that make up the screen.
        """
        yield Header()
        with Vertical(id="manage-content"):
            yield Label("Manage projects", id="manage-title")
            yield OptionList(id="manage-projects")
        yield Static(
            "A: add cwd  R: rename  T: move to top  D: remove  Esc: back",
            id="manage-status",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Populate and focus the workspace list.

        @param None.
        @returns: None.
        """
        self._refresh_options()
        self.query_one("#manage-projects", OptionList).focus()

    def action_add_workspace(self) -> None:
        """Add the CLI launch directory after prompting for its name.

        @param None.
        @returns: None.
        """
        path = str(self.launch_directory)
        normalized_path = normalize_windows_path(path)
        if any(normalize_windows_path(item.path) == normalized_path for item in self.workspaces):
            self._set_status(f"Already configured: {path}", error=True)
            self.app.bell()
            return
        self.app.push_screen(
            WorkspaceNameModal("Display name for the current directory", self.launch_directory.name),
            self._complete_add,
        )

    def action_move_cursor(self, direction: int) -> None:
        """Move the highlighted project within the management list.

        @param direction: Negative for up and positive for down.
        @returns: None.
        """
        project_list = self.query_one("#manage-projects", OptionList)
        if not self.workspaces:
            return
        project_list.focus()
        if direction < 0:
            project_list.action_cursor_up()
        else:
            project_list.action_cursor_down()

    def action_rename_workspace(self) -> None:
        """Prompt for a new name for the highlighted workspace.

        @param None.
        @returns: None.
        """
        index = self._selected_index()
        if index is None:
            self._set_status("Select a project to rename.", error=True)
            return
        workspace = self.workspaces[index]
        self._rename_index = index
        self.app.push_screen(
            WorkspaceNameModal("New display name", workspace.name),
            self._complete_rename,
        )

    def action_delete_workspace(self) -> None:
        """Ask to remove the highlighted workspace.

        @param None.
        @returns: None.
        """
        index = self._selected_index()
        if index is None:
            self._set_status("Select a project to remove.", error=True)
            return
        self._delete_index = index
        self.app.push_screen(DeleteWorkspaceModal(self.workspaces[index]), self._complete_delete)

    def action_move_workspace_to_top(self) -> None:
        """Move the highlighted workspace to the start of the configured order.

        @param None.
        @returns: None.
        """
        index = self._selected_index()
        if index is None:
            self._set_status("Select a project to move.", error=True)
            return

        workspace = self.workspaces[index]
        if index == 0:
            self._set_status(f"Already at top: {workspace.name}")
            return

        candidate = [workspace, *self.workspaces[:index], *self.workspaces[index + 1 :]]
        if self._persist(candidate):
            self.workspaces = candidate
            self._refresh_options(highlighted=0)
            self._set_status(f"Moved to top: {workspace.name}")

    def action_close(self) -> None:
        """Return the updated workspace list to the selector.

        @param None.
        @returns: None.
        """
        self.dismiss(tuple(self.workspaces))

    def _complete_add(self, name: str | None) -> None:
        if name is None:
            return
        workspace = Workspace(name=name, path=str(self.launch_directory))
        candidate = [*self.workspaces, workspace]
        if self._persist(candidate):
            self.workspaces = candidate
            self._refresh_options(highlighted=len(candidate) - 1)
            self._set_status(f"Added: {name}")

    def _complete_rename(self, name: str | None) -> None:
        index = self._rename_index
        self._rename_index = None
        if name is None or index is None or not 0 <= index < len(self.workspaces):
            return
        current = self.workspaces[index]
        candidate = list(self.workspaces)
        candidate[index] = Workspace(name=name, path=current.path)
        if self._persist(candidate):
            self.workspaces = candidate
            self._refresh_options(highlighted=index)
            self._set_status(f"Renamed: {name}")

    def _complete_delete(self, confirmed: bool | None) -> None:
        index = self._delete_index
        self._delete_index = None
        if not confirmed or index is None or not 0 <= index < len(self.workspaces):
            return
        removed = self.workspaces[index]
        candidate = [item for position, item in enumerate(self.workspaces) if position != index]
        if self._persist(candidate):
            self.workspaces = candidate
            self._refresh_options(highlighted=min(index, len(candidate) - 1))
            self._set_status(f"Removed: {removed.name}")

    def _persist(self, workspaces: Sequence[Workspace]) -> bool:
        try:
            save_workspaces(
                self.config_path,
                workspaces,
                expected_workspaces=self.workspaces,
            )
        except (ConfigPathError, ProjectSelectionError) as exc:
            self._set_status(str(exc), error=True)
            self.app.bell()
            return False
        return True

    def _selected_index(self) -> int | None:
        index = self.query_one("#manage-projects", OptionList).highlighted
        if index is None or not 0 <= index < len(self.workspaces):
            return None
        return index

    def _refresh_options(self, *, highlighted: int | None = None) -> None:
        project_list = self.query_one("#manage-projects", OptionList)
        project_list.set_options(
            _project_option(workspace, index) for index, workspace in enumerate(self.workspaces)
        )
        if self.workspaces:
            project_list.highlighted = 0 if highlighted is None else highlighted
        else:
            project_list.highlighted = None

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#manage-status", Static)
        status.update(message)
        status.set_class(error, "error")


class ProjectSelectorApp(App[Workspace | None]):
    """Provide a searchable terminal UI for selecting a workspace."""

    TITLE: str | None = "Codex Project Selector"
    SUB_TITLE: str | None = "Choose a configured workspace"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_cursor(-1)", "Previous", priority=True),
        Binding("down", "move_cursor(1)", "Next", priority=True),
        Binding("enter", "select_current", "Select"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+q", "cancel", "Cancel", key_display="Ctrl+Q"),
        Binding("ctrl+f", "focus_search", "Search", key_display="Ctrl+F"),
        Binding("f2", "manage", "Manage", priority=True),
    ]
    CSS: ClassVar[str] = """
    Screen {
        background: $background;
    }

    #content {
        height: 1fr;
        padding: 1 2;
    }

    #title {
        height: 2;
        text-style: bold;
        color: $accent;
    }

    #search {
        margin-bottom: 1;
    }

    #summary {
        height: 1;
        color: $text-muted;
        margin-bottom: 1;
    }

    #projects {
        height: 1fr;
        border: round $primary;
    }

    #status {
        height: 3;
        padding: 0 2;
        border-top: solid $primary;
    }

    #status.error {
        color: $error;
        text-style: bold;
    }
    """

    def __init__(
        self,
        workspaces: Sequence[Workspace],
        config_path: Path | None = None,
        launch_directory: Path | None = None,
    ) -> None:
        """Initialize the project selector.

        @param workspaces: Configured workspaces in display order.
        @param config_path: Configuration file used by workspace management.
        @param launch_directory: CLI startup directory used by Add.
        @returns: None.
        """
        super().__init__()
        self.workspaces: tuple[Workspace, ...] = tuple(workspaces)
        self.filtered_workspaces: list[Workspace] = list(workspaces)
        self._option_workspaces: tuple[Workspace, ...] = tuple(workspaces)
        self.config_path = config_path
        self.launch_directory = launch_directory if launch_directory is not None else Path.cwd()

    def compose(self) -> ComposeResult:
        """Compose the selector interface.

        @param None.
        @returns: Widgets that make up the selector screen.
        """
        yield Header()
        with Vertical(id="content"):
            yield Label("Select a project", id="title")
            yield Input(placeholder="Filter by project name or configured path", id="search")
            yield Static(id="summary")
            yield OptionList(
                *(
                    _project_option(workspace, index)
                    for index, workspace in enumerate(self.filtered_workspaces)
                ),
                id="projects",
            )
        yield Static("Type to filter, then use Up/Down and Enter.", id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize focus and selection status after mounting.

        @param None.
        @returns: None.
        """
        self.query_one("#search", Input).focus()
        self._update_summary()
        self._update_highlight_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Rebuild the project list when the search text changes.

        @param event: Input change containing the current filter text.
        @returns: None.
        """
        self.filtered_workspaces = filter_workspaces(self.workspaces, event.value)
        self._option_workspaces = tuple(self.filtered_workspaces)
        project_list = self.query_one("#projects", OptionList)
        project_list.set_options(
            _project_option(workspace, index)
            for index, workspace in enumerate(self._option_workspaces)
        )
        project_list.highlighted = 0 if self._option_workspaces else None
        self._update_summary()
        self._update_highlight_status()

    @on(Input.Submitted, "#search")
    def on_search_submitted(self, _event: Input.Submitted) -> None:
        """Select the first current match when search is submitted.

        @param _event: Search input submission notification.
        @returns: None.
        """
        self.action_select_current()

    @on(OptionList.OptionHighlighted, "#projects")
    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        """Update status when keyboard navigation changes the current row.

        @param _event: Option highlight notification.
        @returns: None.
        """
        self._update_highlight_status()

    @on(OptionList.OptionSelected, "#projects")
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle mouse or native list selection.

        @param event: Option selection notification.
        @returns: None.
        """
        workspace = self._workspace_at(event.option_index)
        if workspace is not None:
            self._finish_selection(workspace)

    def action_move_cursor(self, direction: int) -> None:
        """Move the highlighted project up or down.

        @param direction: Negative for up and positive for down.
        @returns: None.
        """
        current_screen = self.screen
        if isinstance(current_screen, WorkspaceManagementScreen):
            current_screen.action_move_cursor(direction)
            return

        project_list = self.query_one("#projects", OptionList)
        if not self._option_workspaces:
            return
        project_list.focus()
        if direction < 0:
            project_list.action_cursor_up()
        else:
            project_list.action_cursor_down()

    def action_select_current(self) -> None:
        """Select the currently highlighted project.

        @param None.
        @returns: None.
        """
        search = self.query_one("#search", Input)
        if search.has_focus:
            current_matches: list[Workspace] = filter_workspaces(self.workspaces, search.value)
            if current_matches:
                self._finish_selection(current_matches[0])
            return
        project_list = self.query_one("#projects", OptionList)
        workspace = self._workspace_at(project_list.highlighted)
        if workspace is not None:
            self._finish_selection(workspace)

    def action_focus_search(self) -> None:
        """Move keyboard focus to the search input.

        @param None.
        @returns: None.
        """
        self.query_one("#search", Input).focus()

    def action_cancel(self) -> None:
        """Exit without selecting a project.

        @param None.
        @returns: None.
        """
        self.exit(None)

    def action_manage(self) -> None:
        """Open the workspace management screen.

        @param None.
        @returns: None.
        """
        if isinstance(
            self.screen,
            (WorkspaceManagementScreen, WorkspaceNameModal, DeleteWorkspaceModal),
        ):
            return
        config_path = self.config_path
        if config_path is None:
            try:
                config_path = resolve_config_path()
            except ConfigPathError as exc:
                self._set_status(str(exc), error=True)
                self.bell()
                return
            self.config_path = config_path
        try:
            current_workspaces = load_optional_workspaces(config_path)
        except ProjectSelectionError as exc:
            self._set_status(str(exc), error=True)
            self.bell()
            return
        self.push_screen(
            WorkspaceManagementScreen(config_path, current_workspaces, self.launch_directory),
            self._management_closed,
        )

    def _management_closed(self, workspaces: tuple[Workspace, ...] | None) -> None:
        if workspaces is None:
            return
        config_path = self.config_path
        if config_path is None:
            return
        try:
            self.workspaces = tuple(load_optional_workspaces(config_path))
        except ProjectSelectionError as exc:
            self._set_status(str(exc), error=True)
            self.bell()
            return
        search = self.query_one("#search", Input)
        self.filtered_workspaces = filter_workspaces(self.workspaces, search.value)
        self._option_workspaces = tuple(self.filtered_workspaces)
        project_list = self.query_one("#projects", OptionList)
        project_list.set_options(
            _project_option(workspace, index)
            for index, workspace in enumerate(self._option_workspaces)
        )
        project_list.highlighted = 0 if self._option_workspaces else None
        self._update_summary()
        self._update_highlight_status()
        search.focus()

    def _finish_selection(self, workspace: Workspace) -> None:
        try:
            ensure_workspace_exists(workspace)
        except ProjectSelectionError as exc:
            self._set_status(str(exc), error=True)
            self.bell()
            return
        self.exit(workspace)

    def _workspace_at(self, index: int | None) -> Workspace | None:
        if index is None or not 0 <= index < len(self._option_workspaces):
            return None
        return self._option_workspaces[index]

    def _update_summary(self) -> None:
        summary = self.query_one("#summary", Static)
        summary.update(
            f"Showing {len(self.filtered_workspaces)} of {len(self.workspaces)} configured projects"
        )

    def _update_highlight_status(self) -> None:
        project_list = self.query_one("#projects", OptionList)
        workspace = self._workspace_at(project_list.highlighted)
        if workspace is None:
            message = "No projects match the current filter."
            if self.filtered_workspaces:
                message = "Use Up/Down to choose a project and Enter to select."
            self._set_status(message, error=not self.filtered_workspaces)
            return
        workspace_path = Path(workspace.path)
        if workspace_path.is_dir():
            self._set_status(f"Current selection: {workspace.name} — {workspace.path}")
        elif workspace_path.exists():
            self._set_status(f"Unavailable: project path is not a directory: {workspace.path}", error=True)
        else:
            self._set_status(f"Unavailable: project path does not exist: {workspace.path}", error=True)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.update(message)
        status.set_class(error, "error")


def run_project_selector(
    workspaces: Sequence[Workspace],
    config_path: Path | None = None,
    launch_directory: Path | None = None,
) -> Workspace | None:
    """Run the full-screen terminal project selector.

    @param workspaces: Configured workspaces to display.
    @param config_path: Configuration file used by workspace management.
    @param launch_directory: CLI startup directory used by Add.
    @returns: The selected workspace, or None when cancelled.
    """
    return ProjectSelectorApp(workspaces, config_path, launch_directory).run()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select a configured Codex project path.")
    parser.add_argument(
        "--output-file",
        type=Path,
        help="write the selected path to this UTF-8 file instead of stdout",
    )
    parser.add_argument("project", nargs="?", help="exact project name or configured path")
    return parser


@dataclass(frozen=True, slots=True)
class _CliArguments:
    output_file: Path | None
    project: str | None


def _parse_args(argv: Sequence[str] | None) -> _CliArguments:
    namespace = _build_parser().parse_args(argv)
    return _CliArguments(
        output_file=cast(Path | None, namespace.output_file),
        project=cast(str | None, namespace.project),
    )


def _remove_output_file(output_file: Path | None) -> None:
    if output_file is None:
        return
    try:
        output_file.unlink(missing_ok=True)
    except OSError as exc:
        raise ProjectSelectionError(f"Unable to prepare output file {output_file}: {exc}") from exc


def _write_selected_path(path: str, output_file: Path | None, stdout: TextIO) -> None:
    if output_file is None:
        print(path, file=stdout)
        return
    try:
        output_file.write_text(f"{path}\n", encoding="utf-8")
    except OSError as exc:
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProjectSelectionError(f"Unable to write output file {output_file}: {exc}") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the project selector command-line interface.

    @param argv: Command-line arguments excluding the executable name.
    @param stdout: Optional stream for the selected path.
    @param stderr: Optional stream for prompts and errors.
    @returns: Zero on success, otherwise a nonzero exit code.
    """
    output_stream = stdout if stdout is not None else sys.stdout
    error_stream = stderr if stderr is not None else sys.stderr
    try:
        args = _parse_args(argv)
        _remove_output_file(args.output_file)
        config_path = resolve_config_path()
        if args.project is None:
            workspaces = load_optional_workspaces(config_path)
            workspace = run_project_selector(workspaces, config_path, Path.cwd())
            if workspace is None:
                raise ProjectSelectionError("Selection was cancelled.")
        else:
            workspaces = load_workspaces(config_path)
            workspace = select_workspace(workspaces, args.project)
            ensure_workspace_exists(workspace)
        _write_selected_path(workspace.path, args.output_file, output_stream)
    except (ConfigPathError, ProjectSelectionError) as exc:
        print(f"Error: {exc}", file=error_stream)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
