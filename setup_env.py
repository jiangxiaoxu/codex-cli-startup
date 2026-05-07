"""Create the project virtual environment and install dependencies."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"


def venv_python_path(venv_dir: Path) -> Path:
    """Return the Python executable path inside a virtual environment.

    @param venv_dir: Virtual environment directory.
    @returns: Platform-specific Python executable path.
    """
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run_command(command: Sequence[str]) -> None:
    """Run a command from the project root and fail on non-zero exit.

    @param command: Command arguments to execute.
    @returns: None.
    """
    print(f"+ {' '.join(command)}")
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def ensure_virtual_environment(venv_dir: Path) -> Path:
    """Create the virtual environment if it does not already exist.

    @param venv_dir: Virtual environment directory.
    @returns: Python executable path inside the virtual environment.
    """
    venv_python = venv_python_path(venv_dir)
    if not venv_python.exists():
        run_command([sys.executable, "-m", "venv", str(venv_dir)])
    return venv_python


def install_requirements(venv_python: Path, requirement_file: Path) -> None:
    """Install dependencies into the project virtual environment.

    @param venv_python: Python executable path inside the virtual environment.
    @param requirement_file: Requirements file to install.
    @returns: None.
    """
    run_command([str(venv_python), "-m", "pip", "install", "-r", str(requirement_file)])


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    @param argv: Command-line arguments without the executable name.
    @returns: Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="Create .venv and install project dependencies.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Also install requirements-build.txt for PyInstaller builds.",
    )
    parser.add_argument(
        "--upgrade-pip",
        action="store_true",
        help="Upgrade pip inside .venv before installing dependencies.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run environment setup.

    @param argv: Optional command-line arguments without the executable name.
    @returns: Process exit code.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    venv_python = ensure_virtual_environment(VENV_DIR)

    if args.upgrade_pip:
        run_command([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])

    install_requirements(venv_python, PROJECT_ROOT / "requirements.txt")

    if args.build:
        install_requirements(venv_python, PROJECT_ROOT / "requirements-build.txt")

    print(f"Environment ready: {venv_python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
