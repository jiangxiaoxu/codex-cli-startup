from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[4]
BUILD_SCRIPT = PROJECT_ROOT / "build_list_project.py"
SOURCE_EXECUTABLE = PROJECT_ROOT / "dist" / "list-project.exe"
SOURCE_WRAPPER = PROJECT_ROOT / "list-project.ps1"
TARGET_DIRECTORY_NAME = "codex-cli-startup"


class DeploymentError(RuntimeError):
    """Describe a failed list-project deployment."""


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    """Describe deployed CLI artifact paths."""

    executable_path: Path
    wrapper_path: Path


def resolve_target_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the deployment directory below the current user's local app data.

    @param environ: Optional environment mapping used instead of the process environment.
    @returns: The resolved deployment directory.
    """
    environment: Mapping[str, str] = os.environ if environ is None else environ
    local_app_data = environment.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise DeploymentError("The LOCALAPPDATA environment variable is not set.")
    return Path(local_app_data) / TARGET_DIRECTORY_NAME


def build_executable() -> None:
    """Build the standalone list-project executable with the active Python environment.

    @param None.
    @returns: None.
    """
    if not BUILD_SCRIPT.is_file():
        raise DeploymentError(f"Build script was not found: {BUILD_SCRIPT}")
    completed = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise DeploymentError(f"Executable build failed with exit code {completed.returncode}.")


def deploy() -> DeploymentResult:
    """Build and atomically deploy the CLI executable and PowerShell wrapper.

    @param None.
    @returns: Paths to the deployed artifacts.
    """
    build_executable()
    sources = (SOURCE_EXECUTABLE, SOURCE_WRAPPER)
    missing_sources = [source for source in sources if not source.is_file()]
    if missing_sources:
        raise DeploymentError(f"Deployment source was not found: {missing_sources[0]}")

    target_directory = resolve_target_directory()
    try:
        target_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeploymentError(
            f"Unable to create deployment directory {target_directory}: {exc}"
        ) from exc

    executable_path = target_directory / SOURCE_EXECUTABLE.name
    wrapper_path = target_directory / SOURCE_WRAPPER.name
    _copy_atomically(SOURCE_EXECUTABLE, executable_path)
    _copy_atomically(SOURCE_WRAPPER, wrapper_path)
    _verify_copy(SOURCE_EXECUTABLE, executable_path)
    _verify_copy(SOURCE_WRAPPER, wrapper_path)
    _smoke_test_executable(executable_path)
    return DeploymentResult(executable_path=executable_path, wrapper_path=wrapper_path)


def _copy_atomically(source: Path, destination: Path) -> None:
    temporary_path = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary_path)
        temporary_path.replace(destination)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DeploymentError(f"Unable to deploy {destination}: {exc}") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DeploymentError(f"Unable to hash {path}: {exc}") from exc
    return digest.hexdigest()


def _verify_copy(source: Path, destination: Path) -> None:
    if _file_sha256(source) != _file_sha256(destination):
        raise DeploymentError(f"Deployed file hash does not match its source: {destination}")


def _smoke_test_executable(executable_path: Path) -> None:
    completed = subprocess.run(
        [str(executable_path), "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0 or "Select a configured Codex project path" not in completed.stdout:
        raise DeploymentError(
            f"Deployed executable smoke test failed with exit code {completed.returncode}."
        )


def main() -> int:
    """Deploy list-project artifacts and print their resolved paths.

    @param None.
    @returns: A process exit code.
    """
    try:
        result = deploy()
    except DeploymentError as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        return 1

    print(f"Deployed executable: {result.executable_path}")
    print(f"Deployed PowerShell wrapper: {result.wrapper_path}")
    print("Executable smoke test: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
