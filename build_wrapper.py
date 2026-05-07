"""Build the lightweight launcher wrapper executable."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
WRAPPER_PROJECT = PROJECT_ROOT / "launcher_wrapper" / "CodexWorkspaceLauncherWrapper.csproj"
PUBLISH_DIR = PROJECT_ROOT / ".launcher_wrapper_publish"
OUTPUT_PATH = PROJECT_ROOT / "launch_launcher.exe"
OUTPUT_FILES = (
    "launch_launcher.deps.json",
    "launch_launcher.dll",
    "launch_launcher.exe",
    "launch_launcher.runtimeconfig.json",
)


def run_command(command: Sequence[str]) -> None:
    """Run a command from the project root and fail on non-zero exit.

    @param command: Command arguments to execute.
    @returns: None.
    """
    print(f"+ {' '.join(command)}")
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def ensure_dotnet_available() -> None:
    """Raise with install guidance when dotnet is not available.

    @param None.
    @returns: None.
    """
    if shutil.which("dotnet") is None:
        raise RuntimeError("dotnet SDK is not available on PATH.")


def build_wrapper() -> Path:
    """Build the launcher wrapper executable.

    @param None.
    @returns: The generated wrapper executable path.
    """
    ensure_dotnet_available()

    if PUBLISH_DIR.exists():
        shutil.rmtree(PUBLISH_DIR)

    run_command(
        [
            "dotnet",
            "publish",
            str(WRAPPER_PROJECT),
            "-c",
            "Release",
            "-r",
            "win-x64",
            "--self-contained",
            "false",
            "-p:PublishSingleFile=false",
            "-p:DebugType=none",
            "-p:DebugSymbols=false",
            "-o",
            str(PUBLISH_DIR),
        ]
    )

    for file_name in OUTPUT_FILES:
        published_file = PUBLISH_DIR / file_name
        if not published_file.exists():
            raise FileNotFoundError(f"Expected wrapper output was not created: {published_file}")
        shutil.copy2(published_file, PROJECT_ROOT / file_name)

    return OUTPUT_PATH


def main() -> int:
    """Build the wrapper executable and print the output path.

    @param None.
    @returns: A process exit code.
    """
    try:
        output_path = build_wrapper()
    except Exception as error:  # noqa: BLE001
        print(f"Wrapper build failed: {error}", file=sys.stderr)
        return 1

    print(f"Built wrapper executable: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
