from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SPEC_PATH = PROJECT_ROOT / "CodexWorkspaceLauncher.spec"
OUTPUT_PATH = PROJECT_ROOT / "dist" / "CodexWorkspaceLauncher.exe"


def ensure_pyinstaller_available() -> None:
    """Raise with install guidance when PyInstaller is not installed.

    @param None.
    @returns None.
    """

    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError(
            "PyInstaller is not installed. Run: python -m pip install -r requirements-build.txt"
        )


def build_executable() -> Path:
    """Build the GUI launcher executable with PyInstaller.

    @param None.
    @returns The expected executable output path.
    """

    ensure_pyinstaller_available()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC_PATH),
    ]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    if not OUTPUT_PATH.exists():
        raise FileNotFoundError(f"Expected executable was not created: {OUTPUT_PATH}")
    return OUTPUT_PATH


def main() -> int:
    """Build the executable and print the output path.

    @param None.
    @returns A process exit code.
    """

    try:
        output_path = build_executable()
    except Exception as error:  # noqa: BLE001
        print(f"Build failed: {error}", file=sys.stderr)
        return 1

    print(f"Built executable: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
