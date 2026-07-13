from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import list_project
from textual.binding import Binding
from textual.widgets import OptionList, Static


class ListProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        self.local_app_data = self.root / "LocalAppData"
        self.environment_patch = mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(self.local_app_data)},
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.alpha_path = self.root / "Alpha"
        self.beta_path = self.root / "Beta"
        self.alpha_path.mkdir()
        self.beta_path.mkdir()

    def _write_config(self, workspaces: object) -> Path:
        config_path = self.local_app_data / "codex-cli-startup" / "codex-cli-startup_config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"workspaces": workspaces}), encoding="utf-8")
        return config_path

    def test_save_workspaces_preserves_unrelated_top_level_fields_and_unicode(self) -> None:
        config_path = self.local_app_data / "codex-cli-startup" / "codex-cli-startup_config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "workspaces": [{"name": "Old", "path": r"C:\Old"}],
                    "terminal": "Windows Terminal",
                    "ui_state": {"width": 900},
                    "custom": [1, 2, 3],
                }
            ),
            encoding="utf-8",
        )

        list_project.save_workspaces(
            config_path,
            [list_project.Workspace("中文项目", r"C:\新项目")],
        )

        saved_text = config_path.read_text(encoding="utf-8")
        saved = json.loads(saved_text)
        self.assertEqual(saved["workspaces"], [{"name": "中文项目", "path": r"C:\新项目"}])
        self.assertEqual(saved["terminal"], "Windows Terminal")
        self.assertEqual(saved["ui_state"], {"width": 900})
        self.assertEqual(saved["custom"], [1, 2, 3])
        self.assertIn("中文项目", saved_text)

    def test_save_workspaces_rejects_stale_management_snapshot(self) -> None:
        config_path = self.local_app_data / "codex-cli-startup" / "codex-cli-startup_config.json"
        original = list_project.Workspace("Original", str(self.alpha_path))
        external = list_project.Workspace("External", str(self.beta_path))
        list_project.save_workspaces(config_path, [original])
        list_project.save_workspaces(config_path, [external], expected_workspaces=[original])

        with self.assertRaisesRegex(list_project.ProjectSelectionError, "another process"):
            list_project.save_workspaces(
                config_path,
                [list_project.Workspace("Stale", str(self.alpha_path))],
                expected_workspaces=[original],
            )

        self.assertEqual(list_project.load_workspaces(config_path), [external])

    def test_load_workspaces_preserves_configuration_order(self) -> None:
        config_path = self._write_config(
            [
                {"name": "Beta", "path": str(self.beta_path)},
                {"name": "Alpha", "path": str(self.alpha_path)},
            ]
        )

        workspaces = list_project.load_workspaces(config_path)

        self.assertEqual([workspace.name for workspace in workspaces], ["Beta", "Alpha"])

    def test_select_workspace_matches_name_case_insensitively(self) -> None:
        workspace = list_project.Workspace("Alpha", str(self.alpha_path))

        selected = list_project.select_workspace([workspace], "aLpHa")

        self.assertIs(selected, workspace)

    def test_select_workspace_normalizes_windows_path(self) -> None:
        workspace = list_project.Workspace("Alpha", r"C:\Projects\Alpha")

        selected = list_project.select_workspace([workspace], "c:/projects/./ALPHA")

        self.assertIs(selected, workspace)

    def test_filter_workspaces_matches_name_and_normalized_path_fragment(self) -> None:
        workspaces = [
            list_project.Workspace("Alpha Tools", r"C:\Projects\Alpha"),
            list_project.Workspace("Beta", r"D:\Work\Beta"),
        ]

        self.assertEqual(list_project.filter_workspaces(workspaces, "TOOLS"), [workspaces[0]])
        self.assertEqual(list_project.filter_workspaces(workspaces, "work/bet"), [workspaces[1]])
        self.assertEqual(list_project.filter_workspaces(workspaces, ""), workspaces)

    def test_duplicate_name_is_ambiguous_but_index_can_select(self) -> None:
        workspaces = [
            list_project.Workspace("Alpha", str(self.alpha_path)),
            list_project.Workspace("ALPHA", str(self.beta_path)),
        ]

        with self.assertRaisesRegex(list_project.ProjectSelectionError, "ambiguous"):
            list_project.select_workspace(workspaces, "alpha")
        self.assertIs(list_project.select_workspace(workspaces, "2", allow_index=True), workspaces[1])

    def test_load_reports_missing_invalid_and_empty_configurations(self) -> None:
        with self.assertRaisesRegex(list_project.ProjectSelectionError, "not found"):
            list_project.load_workspaces(self.root / "missing.json")

        invalid_json_path = self.root / "invalid.json"
        invalid_json_path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(list_project.ProjectSelectionError, "invalid JSON"):
            list_project.load_workspaces(invalid_json_path)

        empty_path = self._write_config([])
        with self.assertRaisesRegex(list_project.ProjectSelectionError, "No workspaces"):
            list_project.load_workspaces(empty_path)

    def test_load_reports_invalid_workspace_structure(self) -> None:
        config_path = self._write_config([{"name": "Alpha"}])

        with self.assertRaisesRegex(list_project.ProjectSelectionError, "invalid or empty 'path'"):
            list_project.load_workspaces(config_path)

    def test_main_direct_query_prints_only_path_to_stdout(self) -> None:
        self._write_config([{"name": "Alpha", "path": str(self.alpha_path)}])
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = list_project.main(
            ["alpha"],
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), f"{self.alpha_path}\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_main_default_config_uses_local_app_data(self) -> None:
        self._write_config([{"name": "Alpha", "path": str(self.alpha_path)}])
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = list_project.main(["Alpha"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), f"{self.alpha_path}\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_main_tui_selection_writes_output_file_without_stdout(self) -> None:
        self._write_config(
            [
                {"name": "Alpha", "path": str(self.alpha_path)},
                {"name": "Beta", "path": str(self.beta_path)},
            ]
        )
        output_path = self.root / "selected.txt"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch("list_project.run_project_selector") as run_selector:
            run_selector.return_value = list_project.Workspace("Beta", str(self.beta_path))
            exit_code = list_project.main(
                ["--output-file", str(output_path)],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output_path.read_text(encoding="utf-8"), f"{self.beta_path}\n")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_main_missing_config_still_opens_empty_tui(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch("list_project.run_project_selector", return_value=None) as run_selector:
            exit_code = list_project.main([], stdout=stdout, stderr=stderr)

        expected_config_path = (
            self.local_app_data / "codex-cli-startup" / "codex-cli-startup_config.json"
        )
        run_selector.assert_called_once_with([], expected_config_path, Path.cwd())
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Selection was cancelled", stderr.getvalue())

    def test_main_cancelled_tui_removes_stale_output_file(self) -> None:
        self._write_config([{"name": "Alpha", "path": str(self.alpha_path)}])
        output_path = self.root / "selected.txt"
        output_path.write_text("stale", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch("list_project.run_project_selector", return_value=None):
            exit_code = list_project.main(
                ["--output-file", str(output_path)],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Selection was cancelled", stderr.getvalue())
        self.assertFalse(output_path.exists())

    def test_main_reports_missing_selected_path(self) -> None:
        missing_path = self.root / "Missing"
        self._write_config([{"name": "Missing", "path": str(missing_path)}])
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = list_project.main(
            ["Missing"],
            stdout=stdout,
            stderr=stderr,
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Project path does not exist", stderr.getvalue())

    def test_main_rejects_existing_file_as_project_path(self) -> None:
        file_path = self.root / "not-a-project.txt"
        file_path.write_text("content", encoding="utf-8")
        self._write_config([{"name": "File", "path": str(file_path)}])
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = list_project.main(
            ["File"],
            stdout=stdout,
            stderr=stderr,
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("not a directory", stderr.getvalue())

    def test_main_reports_missing_local_app_data_without_traceback(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.dict(os.environ, {}, clear=True):
            exit_code = list_project.main(["Alpha"], stdout=stdout, stderr=stderr)

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("LOCALAPPDATA", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class ProjectSelectorAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_footer_uses_full_control_key_names(self) -> None:
        bindings = {
            binding.key: binding.key_display
            for binding in list_project.ProjectSelectorApp.BINDINGS
            if isinstance(binding, Binding)
        }

        self.assertEqual(bindings["ctrl+q"], "Ctrl+Q")
        self.assertEqual(bindings["ctrl+f"], "Ctrl+F")

    async def test_empty_config_can_add_chinese_workspace_and_refresh_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            config_path = root / "config.json"
            launch_directory = root / "Current Project"
            launch_directory.mkdir()
            app = list_project.ProjectSelectorApp([], config_path, launch_directory)

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("f2", "a")
                await pilot.pause()
                self.assertIsInstance(app.screen, list_project.WorkspaceNameModal)
                await pilot.press(*list("中文项目"), "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, list_project.WorkspaceManagementScreen)
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, list_project.WorkspaceManagementScreen)
                self.assertEqual(app.workspaces, (list_project.Workspace("中文项目", str(launch_directory)),))
                options = app.query_one("#projects", OptionList)
                self.assertEqual(options.option_count, 1)
                await pilot.press("escape")

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["workspaces"],
                [{"name": "中文项目", "path": str(launch_directory)}],
            )

    async def test_management_rename_and_delete_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            config_path = root / "config.json"
            project_path = root / "Project"
            launch_directory = root / "Other"
            project_path.mkdir()
            launch_directory.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "workspaces": [{"name": "Original", "path": str(project_path)}],
                        "terminal": "preserved",
                    }
                ),
                encoding="utf-8",
            )
            app = list_project.ProjectSelectorApp(
                [list_project.Workspace("Original", str(project_path))],
                config_path,
                launch_directory,
            )

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("f2", "r")
                await pilot.pause()
                await pilot.press(*list("重命名项目"), "enter")
                await pilot.pause()
                management = app.screen
                self.assertIsInstance(management, list_project.WorkspaceManagementScreen)
                self.assertEqual(
                    management.workspaces,
                    [list_project.Workspace("重命名项目", str(project_path))],
                )

                await pilot.press("d", "n")
                await pilot.pause()
                self.assertIsInstance(app.screen, list_project.WorkspaceManagementScreen)
                self.assertEqual(len(management.workspaces), 1)
                saved_after_cancel = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(len(saved_after_cancel["workspaces"]), 1)

                await pilot.press("d", "y")
                await pilot.pause()
                self.assertEqual(management.workspaces, [])
                saved_after_confirm = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved_after_confirm["workspaces"], [])
                self.assertEqual(saved_after_confirm["terminal"], "preserved")
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.workspaces, ())
                self.assertEqual(app.query_one("#projects", OptionList).option_count, 0)
                await pilot.press("escape")

    async def test_management_rejects_normalized_duplicate_launch_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            config_path = root / "config.json"
            launch_directory = root / "Project"
            launch_directory.mkdir()
            workspace = list_project.Workspace("Existing", f"{launch_directory}\\.")
            config_path.write_text(
                json.dumps({"workspaces": [{"name": workspace.name, "path": workspace.path}]}),
                encoding="utf-8",
            )
            app = list_project.ProjectSelectorApp([workspace], config_path, launch_directory)

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("f2", "a")
                await pilot.pause()
                self.assertIsInstance(app.screen, list_project.WorkspaceManagementScreen)
                status = str(app.screen.query_one("#manage-status", Static).render())
                self.assertIn("Already configured", status)
                self.assertEqual(app.screen.workspaces, [workspace])
                await pilot.press("escape", "escape")

    async def test_management_reopens_with_external_workspace_changes_after_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            config_path = root / "config.json"
            original_path = root / "Original"
            external_path = root / "External"
            original_path.mkdir()
            external_path.mkdir()
            original = list_project.Workspace("Original", str(original_path))
            external = list_project.Workspace("External", str(external_path))
            list_project.save_workspaces(config_path, [original])
            app = list_project.ProjectSelectorApp([original], config_path, root)

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("f2")
                await pilot.pause()
                list_project.save_workspaces(
                    config_path,
                    [external],
                    expected_workspaces=[original],
                )
                await pilot.press("r", *list("冲突名称"), "enter")
                await pilot.pause()
                management = app.screen
                self.assertIsInstance(management, list_project.WorkspaceManagementScreen)
                status = str(management.query_one("#manage-status", Static).render())
                self.assertIn("changed in another process", status)

                await pilot.press("escape", "f2")
                await pilot.pause()
                management = app.screen
                self.assertIsInstance(management, list_project.WorkspaceManagementScreen)
                self.assertEqual(management.workspaces, [external])
                await pilot.press("r", *list("外部项目新名称"), "enter", "escape")
                await pilot.pause()
                self.assertEqual(
                    app.workspaces,
                    (list_project.Workspace("外部项目新名称", str(external_path)),),
                )
                await pilot.press("escape")

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["workspaces"],
                [{"name": "外部项目新名称", "path": str(external_path)}],
            )

    async def test_management_moves_selected_workspace_to_top(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            config_path = root / "config.json"
            workspaces = [
                list_project.Workspace("Alpha", str(root / "Alpha")),
                list_project.Workspace("Beta", str(root / "Beta")),
                list_project.Workspace("Gamma", str(root / "Gamma")),
            ]
            config_path.write_text(
                json.dumps(
                    {
                        "workspaces": [
                            {"name": workspace.name, "path": workspace.path}
                            for workspace in workspaces
                        ],
                        "terminal": "preserved",
                    }
                ),
                encoding="utf-8",
            )
            app = list_project.ProjectSelectorApp(workspaces, config_path, root)

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("f2", "down", "down", "up", "down")
                management = app.screen
                self.assertIsInstance(management, list_project.WorkspaceManagementScreen)
                self.assertEqual(
                    management.query_one("#manage-projects", OptionList).highlighted,
                    2,
                )
                await pilot.press("t")
                await pilot.pause()
                expected = [workspaces[2], workspaces[0], workspaces[1]]
                self.assertEqual(management.workspaces, expected)
                self.assertEqual(
                    management.query_one("#manage-projects", OptionList).highlighted,
                    0,
                )
                status = str(management.query_one("#manage-status", Static).render())
                self.assertIn("Moved to top: Gamma", status)

                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.workspaces, tuple(expected))
                await pilot.press("escape")

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [workspace["name"] for workspace in saved["workspaces"]],
                ["Gamma", "Alpha", "Beta"],
            )
            self.assertEqual(saved["terminal"], "preserved")

    async def test_management_list_selection_does_not_exit_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            config_path = root / "config.json"
            project_path = root / "Project"
            project_path.mkdir()
            workspace = list_project.Workspace("Project", str(project_path))
            list_project.save_workspaces(config_path, [workspace])
            app = list_project.ProjectSelectorApp([workspace], config_path, root)

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("f2", "enter")
                await pilot.pause()
                self.assertIsNone(app.return_value)
                self.assertIsInstance(app.screen, list_project.WorkspaceManagementScreen)
                await pilot.press("escape", "escape")

    async def test_ctrl_q_cancels_while_search_is_focused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            project_path = Path(temp_directory) / "Project"
            project_path.mkdir()
            app = list_project.ProjectSelectorApp(
                [list_project.Workspace("Project", str(project_path))]
            )

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("ctrl+q")

            self.assertIsNone(app.return_value)

    async def test_search_filters_and_enter_selects_highlighted_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            alpha_path = root / "Alpha"
            beta_path = root / "Beta"
            alpha_path.mkdir()
            beta_path.mkdir()
            beta = list_project.Workspace("Beta", str(beta_path))
            app = list_project.ProjectSelectorApp(
                [list_project.Workspace("Alpha", str(alpha_path)), beta]
            )

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("b", "e", "t", "a", "enter")

            self.assertEqual(app.return_value, beta)

    async def test_rapid_filter_and_enter_has_no_pending_mount_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            earlier_path = root / "Code Other"
            target_path = root / "codex-cli-startup"
            earlier_path.mkdir()
            target_path.mkdir()
            target = list_project.Workspace("codex-cli-startup", str(target_path))
            app = list_project.ProjectSelectorApp(
                [list_project.Workspace("Code Other", str(earlier_path)), target]
            )

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press(*list("codex-cli-startup"), "enter")

            self.assertEqual(app.return_value, target)

    async def test_missing_path_is_marked_and_cannot_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            missing_path = Path(temp_directory) / "Missing"
            app = list_project.ProjectSelectorApp(
                [list_project.Workspace("Missing", str(missing_path))]
            )

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                status = str(app.query_one("#status", Static).render())
                self.assertIn("does not exist", status)
                self.assertIsNone(app.return_value)
                await pilot.press("escape")

            self.assertIsNone(app.return_value)

    async def test_file_path_is_marked_and_cannot_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            file_path = Path(temp_directory) / "not-a-project.txt"
            file_path.write_text("content", encoding="utf-8")
            app = list_project.ProjectSelectorApp(
                [list_project.Workspace("File", str(file_path))]
            )

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                status = str(app.query_one("#status", Static).render())
                self.assertIn("not a directory", status)
                self.assertIsNone(app.return_value)
                await pilot.press("escape")

            self.assertIsNone(app.return_value)


if __name__ == "__main__":
    unittest.main()
