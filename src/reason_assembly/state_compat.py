from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from .identity import PRODUCT_SLUG, STATE_ENV


STATE_LOCK = ".reason-assembly-state.lock"


def _home(home: Path | None = None) -> Path:
    return (home or Path.home()).expanduser()


def default_state_root(home: Path | None = None) -> Path:
    return _home(home) / ".local" / "state" / PRODUCT_SLUG


def resolve_state_root(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get(STATE_ENV)
    return Path(configured).expanduser() if configured else default_state_root(home)


def _normalized(path: Path) -> Path:
    return path.expanduser().resolve()


def _ensure_private_directory(path: Path) -> bool:
    created = not path.exists()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(path)
    os.chmod(path, 0o700)
    return created


@contextmanager
def flock_exclusive(lock_path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Acquire a bounded advisory lock on a sibling lock file."""
    lock_path = _normalized(lock_path)
    _ensure_private_directory(lock_path.parent)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock contention: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def exclusive_state_lock(root: Path) -> Iterator[None]:
    root = _normalized(root)
    _ensure_private_directory(root)
    lock_path = root / STATE_LOCK
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def prepare_state_root(primary: Path) -> Path:
    primary = _normalized(primary)
    _ensure_private_directory(primary)
    return primary
