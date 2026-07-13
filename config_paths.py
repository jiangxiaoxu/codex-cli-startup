from __future__ import annotations

import msvcrt
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping


APP_CONFIG_DIRECTORY_NAME = "codex-cli-startup"
CONFIG_FILENAME = "codex-cli-startup_config.json"


class ConfigPathError(RuntimeError):
    """Describe an unavailable application configuration location."""


def resolve_config_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the application directory below the Windows local app data root.

    @param environ: Optional environment mapping used instead of the process environment.
    @returns: The application configuration directory.
    """
    environment: Mapping[str, str] = os.environ if environ is None else environ
    local_app_data = environment.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise ConfigPathError("The LOCALAPPDATA environment variable is not set.")
    return Path(local_app_data) / APP_CONFIG_DIRECTORY_NAME


def resolve_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the launcher configuration file below LOCALAPPDATA.

    @param environ: Optional environment mapping used instead of the process environment.
    @returns: The launcher configuration file path.
    """
    return resolve_config_directory(environ) / CONFIG_FILENAME


@contextmanager
def configuration_lock(config_path: Path) -> Iterator[None]:
    """Hold an inter-process lock for one configuration transaction.

    @param config_path: Configuration file protected by the lock.
    @returns: A context manager that releases the lock on exit.
    """
    lock_path = config_path.with_name(f"{config_path.name}.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
    except OSError as exc:
        raise ConfigPathError(f"Unable to open configuration lock {lock_path}: {exc}") from exc

    with lock_file:
        try:
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as exc:
            raise ConfigPathError(f"Unable to lock configuration file {config_path}: {exc}") from exc
        try:
            yield
        finally:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError as exc:
                raise ConfigPathError(
                    f"Unable to unlock configuration file {config_path}: {exc}"
                ) from exc


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 text file with flushed content.

    @param path: Destination file path.
    @param content: Complete text to write.
    @returns: None.
    """
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(path)
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ConfigPathError(f"Unable to write configuration file {path}: {exc}") from exc
