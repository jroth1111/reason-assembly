from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

from identity import LEGACY_STATE_ENV, PRODUCT_SLUG, STATE_ENV


MIGRATION_MARKER = ".legacy-ccycouncil-import-v1.json"
STATE_LOCK = ".reason-assembly-state.lock"
STAGING_DIRECTORY = ".legacy-run-staging"
RUN_IMPORT_MARKER = ".legacy-source.json"


@dataclass
class MigrationResult:
    canonical: Path
    legacy: Path
    copied_files: int = 0
    copied_directories: int = 0
    skipped_existing: int = 0
    skipped_incomplete: int = 0
    skipped_symlinks: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.copied_files or self.copied_directories)


def _home(home: Path | None = None) -> Path:
    return (home or Path.home()).expanduser()


def default_state_root(home: Path | None = None) -> Path:
    return _home(home) / ".local" / "state" / PRODUCT_SLUG


def default_legacy_state_root(home: Path | None = None) -> Path:
    return _home(home) / ".local" / "state" / "ccycouncil"


def resolve_state_root(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get(STATE_ENV) or values.get(LEGACY_STATE_ENV)
    if configured:
        return Path(configured).expanduser()
    return default_state_root(home)


def _normalized(path: Path) -> Path:
    return path.expanduser().resolve()


def _implicit_legacy_root(
    primary: Path,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    values = os.environ if environ is None else environ
    canonical_configured = values.get(STATE_ENV)
    legacy_configured = values.get(LEGACY_STATE_ENV)
    if canonical_configured and _normalized(Path(canonical_configured)) == primary:
        return _normalized(Path(legacy_configured)) if legacy_configured else None
    if primary == _normalized(default_state_root()):
        return _normalized(default_legacy_state_root())
    return None


def _ensure_private_directory(path: Path) -> bool:
    created = not path.exists()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(path)
    os.chmod(path, 0o700)
    return created


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


def _copy_file_without_overwrite(source: Path, target: Path) -> bool:
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(target, 0o600)
        source_stat = source.stat()
        os.utime(target, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return True


def _tree_signature(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        digest.update(relative.encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode())
        elif path.is_dir():
            digest.update(b"D")
        elif path.is_file():
            digest.update(b"F")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        else:
            digest.update(b"S")
            digest.update(str(path.lstat().st_mode).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_run_import_marker(staging: Path, source: Path, signature: str) -> None:
    payload = {
        "schema_version": 1,
        "legacy_run_id": source.name,
        "source_signature": signature,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    target = staging / RUN_IMPORT_MARKER
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _imported_run_is_current(canonical_run: Path, legacy_run: Path) -> bool:
    marker = canonical_run / RUN_IMPORT_MARKER
    if not marker.is_file():
        return True
    try:
        expected = json.loads(marker.read_text())["source_signature"]
        return expected == _tree_signature(legacy_run)
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _legacy_run_is_complete(source: Path) -> bool:
    try:
        manifest = json.loads((source / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        manifest.get("schema_version") == 4
        and manifest.get("status") in {"completed", "blocked", "failed"}
        and bool(manifest.get("completed_at"))
    )


def _copy_run_atomically(
    source: Path,
    target: Path,
    result: MigrationResult,
) -> None:
    if os.path.lexists(target):
        if target.is_dir() and not _imported_run_is_current(target, source):
            result.errors.append(f"run-stale:{source.name}:LegacyChanged")
        result.skipped_existing += 1
        return
    _ensure_private_directory(target.parent)
    staging_root = target.parent.parent / STAGING_DIRECTORY
    _ensure_private_directory(staging_root)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f"{source.name}-{secrets.token_hex(4)}-",
            dir=staging_root,
        )
    )
    os.chmod(staging, 0o700)
    copied_files = 0
    copied_directories = 1
    errors_before = len(result.errors)
    try:
        source_signature = _tree_signature(source)
        for source_root, directory_names, file_names in os.walk(
            source, topdown=True, followlinks=False
        ):
            source_directory = Path(source_root)
            relative = source_directory.relative_to(source)
            staging_directory = staging / relative
            traversable: list[str] = []
            for name in directory_names:
                child = source_directory / name
                relative_child = child.relative_to(source)
                if child.is_symlink():
                    result.skipped_symlinks += 1
                    continue
                try:
                    _ensure_private_directory(staging / relative_child)
                    copied_directories += 1
                    traversable.append(name)
                except OSError as error:
                    result.errors.append(
                        f"run-directory:{source.name}/{relative_child}:"
                        f"{type(error).__name__}"
                    )
            directory_names[:] = traversable
            for name in file_names:
                child = source_directory / name
                relative_child = child.relative_to(source)
                if child.is_symlink():
                    result.skipped_symlinks += 1
                    continue
                if not child.is_file():
                    result.errors.append(
                        f"run-file:{source.name}/{relative_child}:UnsupportedFileType"
                    )
                    continue
                try:
                    if _copy_file_without_overwrite(
                        child, staging_directory / name
                    ):
                        copied_files += 1
                except OSError as error:
                    result.errors.append(
                        f"run-file:{source.name}/{relative_child}:"
                        f"{type(error).__name__}"
                    )
        if len(result.errors) != errors_before:
            return
        if _tree_signature(source) != source_signature:
            result.errors.append(f"run-changed:{source.name}:SourceChangedDuringCopy")
            return
        _write_run_import_marker(staging, source, source_signature)
        if os.path.lexists(target):
            result.skipped_existing += 1
            return
        os.rename(staging, target)
        result.copied_files += copied_files
        result.copied_directories += copied_directories
    except OSError as error:
        result.errors.append(f"run:{source.name}:{type(error).__name__}")
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _write_marker(result: MigrationResult) -> None:
    now = datetime.now(timezone.utc).isoformat()
    legacy_available = result.legacy.is_dir()
    status = (
        "incomplete"
        if result.errors
        else "complete"
        if legacy_available
        else "no_legacy_source"
    )
    payload = {
        "schema_version": 1,
        "migration": "legacy-ccycouncil-copy-missing",
        "canonical_product": PRODUCT_SLUG,
        "status": status,
        "updated_at": now,
        "legacy_available": legacy_available,
        "copied_files": result.copied_files,
        "copied_directories": result.copied_directories,
        "skipped_existing": result.skipped_existing,
        "skipped_incomplete": result.skipped_incomplete,
        "skipped_symlinks": result.skipped_symlinks,
        "errors": len(result.errors),
        "complete": not result.errors,
        "legacy_preserved": True,
    }
    if not result.errors:
        payload["completed_at"] = now
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    target = result.canonical / MIGRATION_MARKER
    temp = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        os.chmod(target, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def _migrate_legacy_state_locked(canonical: Path, legacy: Path) -> MigrationResult:
    canonical = _normalized(canonical)
    legacy = _normalized(legacy)
    result = MigrationResult(canonical=canonical, legacy=legacy)
    if canonical == legacy:
        return result
    if _ensure_private_directory(canonical):
        result.copied_directories += 1
    if not legacy.is_dir():
        try:
            _write_marker(result)
        except OSError as error:
            result.errors.append(f"marker:{type(error).__name__}")
        return result

    staging_root = canonical / STAGING_DIRECTORY
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)

    for source_root, directory_names, file_names in os.walk(
        legacy, topdown=True, followlinks=False
    ):
        source_directory = Path(source_root)
        relative = source_directory.relative_to(legacy)
        target_directory = canonical / relative
        try:
            if _ensure_private_directory(target_directory):
                result.copied_directories += 1
        except OSError as error:
            result.errors.append(f"directory:{relative}:{type(error).__name__}")
            directory_names[:] = []
            continue

        traversable: list[str] = []
        for name in directory_names:
            if relative == Path(".") and name == STAGING_DIRECTORY:
                continue
            source = source_directory / name
            relative_child = source.relative_to(legacy)
            if source.is_symlink():
                result.skipped_symlinks += 1
                continue
            target = canonical / relative_child
            if relative == Path("runs"):
                if os.path.lexists(target) or _legacy_run_is_complete(source):
                    _copy_run_atomically(source, target, result)
                else:
                    result.skipped_incomplete += 1
                continue
            try:
                if _ensure_private_directory(target):
                    result.copied_directories += 1
                traversable.append(name)
            except OSError as error:
                result.errors.append(
                    f"directory:{relative_child}:{type(error).__name__}"
                )
        directory_names[:] = traversable

        for name in file_names:
            if relative == Path(".") and name in {MIGRATION_MARKER, STATE_LOCK}:
                continue
            source = source_directory / name
            relative_child = source.relative_to(legacy)
            target = canonical / relative_child
            if source.is_symlink():
                result.skipped_symlinks += 1
                continue
            if not source.is_file():
                result.errors.append(
                    f"file:{relative_child}:UnsupportedFileType"
                )
                continue
            try:
                if _copy_file_without_overwrite(source, target):
                    result.copied_files += 1
                else:
                    result.skipped_existing += 1
            except OSError as error:
                result.errors.append(f"file:{relative_child}:{type(error).__name__}")

    try:
        staging_root.rmdir()
    except OSError:
        pass

    try:
        _write_marker(result)
    except OSError as error:
        result.errors.append(f"marker:{type(error).__name__}")
    return result


def migrate_legacy_state(canonical: Path, legacy: Path) -> MigrationResult:
    canonical = _normalized(canonical)
    legacy = _normalized(legacy)
    if canonical == legacy:
        return MigrationResult(canonical=canonical, legacy=legacy)
    with exclusive_state_lock(canonical):
        return _migrate_legacy_state_locked(canonical, legacy)


def prepare_state_root(
    primary: Path,
    *,
    legacy: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> MigrationResult:
    primary = _normalized(primary)
    selected_legacy = (
        _normalized(legacy)
        if legacy is not None
        else _implicit_legacy_root(primary, environ)
    )
    if selected_legacy is None:
        _ensure_private_directory(primary)
        return MigrationResult(primary, default_legacy_state_root())
    return migrate_legacy_state(primary, selected_legacy)


def compatible_state_roots(
    primary: Path,
    *,
    legacy: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    primary = _normalized(primary)
    selected_legacy = (
        _normalized(legacy)
        if legacy is not None
        else _implicit_legacy_root(primary, environ)
    )
    roots = [primary]
    if (
        selected_legacy is not None
        and selected_legacy != primary
        and selected_legacy.is_dir()
    ):
        roots.append(selected_legacy)
    return roots


def locate_run_root(
    primary: Path,
    run_id: str,
    *,
    legacy: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    roots = compatible_state_roots(primary, legacy=legacy, environ=environ)
    canonical = roots[0]
    canonical_run = canonical / "runs" / run_id
    if canonical_run.is_dir():
        for legacy_root in roots[1:]:
            legacy_run = legacy_root / "runs" / run_id
            if legacy_run.is_dir() and not _imported_run_is_current(
                canonical_run, legacy_run
            ):
                return legacy_root
        return canonical
    for legacy_root in roots[1:]:
        if (legacy_root / "runs" / run_id).is_dir():
            return legacy_root
    return canonical


def iter_run_roots(
    primary: Path,
    *,
    legacy: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, Path]]:
    roots = compatible_state_roots(primary, legacy=legacy, environ=environ)
    run_ids: set[str] = set()
    for root in roots:
        runs = root / "runs"
        if runs.is_dir():
            run_ids.update(path.name for path in runs.iterdir() if path.is_dir())
    selected_legacy = roots[1] if len(roots) > 1 else None
    return [
        (
            run_id,
            locate_run_root(
                roots[0],
                run_id,
                legacy=selected_legacy,
                environ=environ,
            ),
        )
        for run_id in sorted(run_ids)
    ]
