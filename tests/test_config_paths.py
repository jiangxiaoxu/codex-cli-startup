from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import config_paths


class ConfigPathsTests(unittest.TestCase):
    def test_resolve_config_path_uses_local_app_data_environment(self) -> None:
        local_app_data = Path("C:/Users/example/AppData/Local")

        resolved = config_paths.resolve_config_path({"LOCALAPPDATA": str(local_app_data)})

        self.assertEqual(
            resolved,
            local_app_data / "codex-cli-startup" / "codex-cli-startup_config.json",
        )

    def test_resolve_config_path_rejects_missing_environment_variable(self) -> None:
        with self.assertRaisesRegex(config_paths.ConfigPathError, "LOCALAPPDATA"):
            config_paths.resolve_config_path({})

    def test_configuration_lock_serializes_other_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            config_path = root / "codex-cli-startup_config.json"
            marker_path = root / "child-entered.txt"
            child_code = (
                "import sys\n"
                "from pathlib import Path\n"
                "from config_paths import configuration_lock\n"
                "with configuration_lock(Path(sys.argv[1])):\n"
                "    Path(sys.argv[2]).write_text('entered', encoding='utf-8')\n"
            )

            with config_paths.configuration_lock(config_path):
                process = subprocess.Popen(
                    [sys.executable, "-c", child_code, str(config_path), str(marker_path)],
                    cwd=Path(__file__).resolve().parents[1],
                )
                time.sleep(0.25)
                self.assertFalse(marker_path.exists())

            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(marker_path.read_text(encoding="utf-8"), "entered")


if __name__ == "__main__":
    unittest.main()
