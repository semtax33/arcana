from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import pandas as pd

from engine.core.paths import DATA_LAKE


SourceValidator = Callable[[Path], None]
_ACTIVE_SOURCE_SESSION: "SourceArchiveSession | None" = None
_ACTIVE_SOURCE_SESSION_LOCK = threading.RLock()
_WINDOWS_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)


def new_source_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replace_file_with_permission_retry(source: Path, destination: Path) -> None:
    """Replace a file, tolerating short-lived Windows file locks."""
    for delay in (*_WINDOWS_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def validate_nonempty_file(path: str | Path) -> None:
    resolved = Path(path)
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ValueError(f"source file is empty or missing: {resolved}")


def csv_source_validator(
    *required_columns: str,
    min_columns: int = 1,
) -> SourceValidator:
    expected = {str(column) for column in required_columns}

    def validate(path: Path) -> None:
        validate_nonempty_file(path)
        frame = pd.read_csv(path, nrows=5)
        if len(frame.columns) < max(1, int(min_columns)):
            raise ValueError(
                f"source CSV has too few columns: expected>={min_columns}; path={path}"
            )
        missing = sorted(expected - set(frame.columns))
        if missing:
            raise ValueError(
                f"source CSV is missing required columns: {', '.join(missing)}; path={path}"
            )

    return validate


def json_source_validator(path: str | Path) -> None:
    validate_nonempty_file(path)
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        json.load(handle)


def xlsx_source_validator(path: str | Path) -> None:
    validate_nonempty_file(path)
    try:
        with ZipFile(path) as archive:
            if "[Content_Types].xml" not in archive.namelist():
                raise ValueError(f"invalid XLSX source: {path}")
    except BadZipFile as exc:
        raise ValueError(f"invalid XLSX source: {path}") from exc


class SourceRefreshLock(AbstractContextManager["SourceRefreshLock"]):
    def __init__(
        self,
        market: str,
        *,
        data_lake_root: str | Path = DATA_LAKE.root,
    ):
        self.market = str(market).strip().lower()
        self.path = Path(data_lake_root) / "meta" / "refresh_locks" / f"{self.market}.lock"
        self._acquired = False

    def acquire(self) -> "SourceRefreshLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "market": self.market,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        descriptor = None
        for attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                break
            except FileExistsError as exc:
                if attempt == 0 and self._remove_stale_lock():
                    continue
                raise RuntimeError(
                    f"another refresh may already be running for market={self.market}; "
                    f"lock={self.path}"
                ) from exc
        if descriptor is None:
            raise RuntimeError(f"failed to acquire refresh lock: {self.path}")
        try:
            os.write(descriptor, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(descriptor)
        self._acquired = True
        return self

    def _remove_stale_lock(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            lock_market = str(payload.get("market", "")).strip().lower()
            lock_host = str(payload.get("host", "")).strip().lower()
            lock_pid = int(payload.get("pid", 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if (
            lock_market != self.market
            or lock_host != socket.gethostname().strip().lower()
            or _process_is_running(lock_pid)
        ):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False

    def __enter__(self) -> "SourceRefreshLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.release()
        return False


def _process_is_running(pid: int) -> bool:
    """Return whether a local PID exists without signaling or mutating it."""
    if int(pid) <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                process_query_limited_information,
                False,
                int(pid),
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class SourceArchiveSession(AbstractContextManager["SourceArchiveSession"]):
    def __init__(
        self,
        market: str,
        *,
        run_id: str | None = None,
        data_lake_root: str | Path = DATA_LAKE.root,
    ):
        self.market = str(market).strip().lower()
        if self.market not in {"kr", "us"}:
            raise ValueError("market must be 'kr' or 'us'")
        self.data_lake_root = Path(data_lake_root).resolve()
        self.run_id = run_id or new_source_run_id()
        self.archive_root = (
            self.data_lake_root / "source-archive" / self.market / self.run_id
        )
        self.manifest_path = (
            self.data_lake_root
            / "meta"
            / "refresh-manifests"
            / self.market
            / f"{self.run_id}.json"
        )
        self.data: dict[str, Any] = {
            "run_id": self.run_id,
            "market": self.market,
            "status": "pending",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "files": [],
        }
        if self.manifest_path.exists():
            self.data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.data["status"] = "pending"
        self._commit_lock = threading.RLock()
        self._save_manifest()

    def commit_file(
        self,
        staged_path: str | Path,
        target_path: str | Path,
        *,
        source: str,
        validator: SourceValidator | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._commit_lock:
            return self._commit_file_locked(
                staged_path,
                target_path,
                source=source,
                validator=validator,
                metadata=metadata,
            )

    def _commit_file_locked(
        self,
        staged_path: str | Path,
        target_path: str | Path,
        *,
        source: str,
        validator: SourceValidator | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        staged = Path(staged_path)
        target = Path(target_path)
        validate_nonempty_file(staged)
        if validator is not None:
            validator(staged)

        target.parent.mkdir(parents=True, exist_ok=True)
        staged_for_replace = self._stage_next_to_target(staged, target)
        new_hash = sha256_file(staged_for_replace)
        new_size = staged_for_replace.stat().st_size
        old_hash = sha256_file(target) if target.exists() else None
        old_size = target.stat().st_size if target.exists() else None
        relative_target = self._relative_target(target)

        if old_hash == new_hash:
            self._unlink(staged_for_replace)
            if staged != staged_for_replace:
                self._unlink(staged)
            entry = {
                "target": relative_target,
                "source": source,
                "status": "unchanged",
                "old_sha256": old_hash,
                "new_sha256": new_hash,
                "old_size": old_size,
                "new_size": new_size,
                "archive": None,
                "metadata": metadata or {},
                "committed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.data["files"].append(entry)
            self._save_manifest()
            return entry

        archive_relative = None
        if target.exists():
            archive = self.archive_root / relative_target
            archive.parent.mkdir(parents=True, exist_ok=True)
            if archive.exists():
                archive = archive.with_name(
                    f"{archive.stem}.{old_hash[:12]}{archive.suffix}"
                )
                collision = 1
                while archive.exists():
                    archive = archive.with_name(
                        f"{archive.stem}.{collision}{archive.suffix}"
                    )
                    collision += 1
            archive_staged = archive.with_name(f".{archive.name}.{uuid4().hex}.tmp")
            shutil.copy2(target, archive_staged)
            if sha256_file(archive_staged) != old_hash:
                self._unlink(archive_staged)
                self._unlink(staged_for_replace)
                raise RuntimeError(f"source archive hash verification failed: {target}")
            replace_file_with_permission_retry(archive_staged, archive)
            archive_relative = archive.relative_to(self.data_lake_root).as_posix()

        entry = {
            "target": relative_target,
            "source": source,
            "status": "pending",
            "old_sha256": old_hash,
            "new_sha256": new_hash,
            "old_size": old_size,
            "new_size": new_size,
            "archive": archive_relative,
            "metadata": metadata or {},
            "committed_at": None,
        }
        self.data["files"].append(entry)
        self._save_manifest()

        try:
            replace_file_with_permission_retry(staged_for_replace, target)
        except Exception:
            self._save_manifest()
            raise
        finally:
            if staged != staged_for_replace:
                self._unlink(staged)

        if sha256_file(target) != new_hash:
            raise RuntimeError(f"source replacement hash verification failed: {target}")
        entry["status"] = "committed"
        entry["committed_at"] = datetime.now(timezone.utc).isoformat()
        self._save_manifest()
        return entry

    def commit_bytes(
        self,
        payload: bytes,
        target_path: str | Path,
        *,
        source: str,
        validator: SourceValidator | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{self.run_id}.download")
        staged.write_bytes(payload)
        try:
            return self.commit_file(
                staged,
                target,
                source=source,
                validator=validator,
                metadata=metadata,
            )
        except Exception:
            self._unlink(staged)
            raise

    def complete(self) -> None:
        self.data["status"] = "committed"
        self.data["completed_at"] = datetime.now(timezone.utc).isoformat()
        self._save_manifest()

    def fail(self, error: BaseException) -> None:
        self.data["status"] = "failed"
        self.data["error"] = f"{type(error).__name__}: {error}"
        self._save_manifest()

    def _stage_next_to_target(self, staged: Path, target: Path) -> Path:
        target_parent = target.parent.resolve()
        try:
            same_parent = staged.parent.resolve() == target_parent
        except FileNotFoundError:
            same_parent = False
        if same_parent:
            return staged
        sibling = target.with_name(f".{target.name}.{self.run_id}.staged")
        shutil.copy2(staged, sibling)
        return sibling

    def _relative_target(self, target: Path) -> str:
        resolved = target.resolve()
        try:
            return resolved.relative_to(self.data_lake_root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"source target must be inside data lake: {target}"
            ) from exc

    def _save_manifest(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        staged = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{uuid4().hex}.tmp"
        )
        staged.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            replace_file_with_permission_retry(staged, self.manifest_path)
        except Exception:
            self._unlink(staged)
            raise

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "SourceArchiveSession":
        global _ACTIVE_SOURCE_SESSION
        with _ACTIVE_SOURCE_SESSION_LOCK:
            if _ACTIVE_SOURCE_SESSION not in {None, self}:
                raise RuntimeError("another source archive session is already active")
            _ACTIVE_SOURCE_SESSION = self
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        global _ACTIVE_SOURCE_SESSION
        try:
            if exc is None:
                self.complete()
            else:
                self.fail(exc)
        finally:
            with _ACTIVE_SOURCE_SESSION_LOCK:
                if _ACTIVE_SOURCE_SESSION is self:
                    _ACTIVE_SOURCE_SESSION = None
        return False


def active_source_session() -> SourceArchiveSession | None:
    with _ACTIVE_SOURCE_SESSION_LOCK:
        return _ACTIVE_SOURCE_SESSION


def write_source_bytes(
    target_path: str | Path,
    payload: bytes,
    *,
    source: str,
    validator: SourceValidator | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    session = active_source_session()
    if session is not None:
        session.commit_bytes(
            payload,
            target,
            source=source,
            validator=validator,
            metadata=metadata,
        )
        return target

    staged = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    staged.write_bytes(payload)
    try:
        validate_nonempty_file(staged)
        if validator is not None:
            validator(staged)
        replace_file_with_permission_retry(staged, target)
    except Exception:
        SourceArchiveSession._unlink(staged)
        raise
    return target


def write_source_text(
    target_path: str | Path,
    text: str,
    *,
    source: str,
    encoding: str = "utf-8",
    validator: SourceValidator | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    return write_source_bytes(
        target_path,
        str(text).encode(encoding),
        source=source,
        validator=validator,
        metadata=metadata,
    )


def write_source_dataframe(
    target_path: str | Path,
    frame: pd.DataFrame,
    *,
    source: str,
    encoding: str = "utf-8-sig",
    index: bool = False,
    validator: SourceValidator | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{uuid4().hex}.csv.tmp")
    frame.to_csv(staged, index=index, encoding=encoding)
    session = active_source_session()
    if session is not None:
        try:
            session.commit_file(
                staged,
                target,
                source=source,
                validator=validator,
                metadata=metadata,
            )
        except Exception:
            SourceArchiveSession._unlink(staged)
            raise
        return target
    try:
        validate_nonempty_file(staged)
        if validator is not None:
            validator(staged)
        replace_file_with_permission_retry(staged, target)
    except Exception:
        SourceArchiveSession._unlink(staged)
        raise
    return target
