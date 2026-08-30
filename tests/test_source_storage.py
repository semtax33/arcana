from __future__ import annotations

import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

from engine.core.source_storage import (
    _WINDOWS_REPLACE_RETRY_DELAYS,
    SourceArchiveSession,
    SourceRefreshLock,
    json_source_validator,
    sha256_file,
)


class SourceStorageTest(unittest.TestCase):
    def test_commit_archives_existing_source_before_atomic_replace(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            target = root / "bronze" / "sec" / "companyfacts" / "CIK1.json"
            staged = root / "staging" / "CIK1.json"
            target.parent.mkdir(parents=True)
            staged.parent.mkdir(parents=True)
            target.write_text('{"version": 1}', encoding="utf-8")
            staged.write_text('{"version": 2}', encoding="utf-8")
            old_hash = sha256_file(target)

            with SourceArchiveSession("us", run_id="run-1", data_lake_root=root) as session:
                entry = session.commit_file(
                    staged,
                    target,
                    source="sec-companyfacts",
                    validator=json_source_validator,
                )

            archive = root / entry["archive"]
            self.assertEqual(entry["status"], "committed")
            self.assertEqual(sha256_file(archive), old_hash)
            self.assertEqual(
                json.loads(archive.read_text(encoding="utf-8")),
                {"version": 1},
            )
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"version": 2},
            )

    def test_invalid_source_never_changes_current_file(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            target = root / "bronze" / "sec" / "companyfacts" / "CIK1.json"
            staged = root / "staging" / "CIK1.json"
            target.parent.mkdir(parents=True)
            staged.parent.mkdir(parents=True)
            target.write_text('{"version": 1}', encoding="utf-8")
            staged.write_text("{invalid", encoding="utf-8")

            session = SourceArchiveSession("us", run_id="run-2", data_lake_root=root)
            with self.assertRaises(json.JSONDecodeError):
                session.commit_file(
                    staged,
                    target,
                    source="sec-companyfacts",
                    validator=json_source_validator,
                )

            self.assertEqual(target.read_text(encoding="utf-8"), '{"version": 1}')
            self.assertFalse((root / "source-archive").exists())
            self.assertTrue(staged.exists())

    def test_unchanged_source_is_not_archived_or_replaced(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            target = root / "bronze" / "fred" / "rates" / "rate.csv"
            staged = root / "staging" / "rate.csv"
            target.parent.mkdir(parents=True)
            staged.parent.mkdir(parents=True)
            target.write_bytes(b"DATE,VALUE\n2026-01-01,1\n")
            staged.write_bytes(target.read_bytes())

            with SourceArchiveSession("kr", run_id="run-3", data_lake_root=root) as session:
                entry = session.commit_file(staged, target, source="fred")

            self.assertEqual(entry["status"], "unchanged")
            self.assertIsNone(entry["archive"])
            self.assertFalse((root / "source-archive").exists())

    def test_replace_failure_keeps_old_current_and_archived_copy(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            target = root / "bronze" / "source.json"
            staged = root / "staging" / "source.json"
            target.parent.mkdir(parents=True)
            staged.parent.mkdir(parents=True)
            target.write_text('{"version": 1}', encoding="utf-8")
            staged.write_text('{"version": 2}', encoding="utf-8")
            real_replace = __import__("os").replace

            def fail_target_replace(source, destination):
                if Path(destination) == target:
                    raise OSError("replace failed")
                return real_replace(source, destination)

            session = SourceArchiveSession("kr", run_id="run-4", data_lake_root=root)
            with (
                patch("engine.core.source_storage.os.replace", side_effect=fail_target_replace),
                self.assertRaises(OSError),
            ):
                session.commit_file(staged, target, source="test")

            self.assertEqual(target.read_text(encoding="utf-8"), '{"version": 1}')
            manifest = json.loads(session.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"][0]["status"], "pending")
            archive = root / manifest["files"][0]["archive"]
            self.assertEqual(archive.read_text(encoding="utf-8"), '{"version": 1}')

    def test_manifest_replace_retries_transient_permission_error(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            session = SourceArchiveSession("kr", run_id="run-5", data_lake_root=root)
            real_replace = __import__("os").replace
            attempts = 0

            def transient_manifest_lock(source, destination):
                nonlocal attempts
                if Path(destination) == session.manifest_path:
                    attempts += 1
                    if attempts <= 2:
                        raise PermissionError("manifest is temporarily locked")
                return real_replace(source, destination)

            with (
                patch(
                    "engine.core.source_storage.os.replace",
                    side_effect=transient_manifest_lock,
                ),
                patch("engine.core.source_storage.time.sleep") as sleep,
            ):
                session.complete()

            self.assertEqual(attempts, 3)
            self.assertEqual(
                sleep.call_args_list,
                [
                    call(_WINDOWS_REPLACE_RETRY_DELAYS[0]),
                    call(_WINDOWS_REPLACE_RETRY_DELAYS[1]),
                ],
            )
            manifest = json.loads(session.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "committed")

    def test_manifest_replace_cleans_staged_file_after_persistent_permission_error(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            session = SourceArchiveSession("kr", run_id="run-6", data_lake_root=root)

            with (
                patch(
                    "engine.core.source_storage.os.replace",
                    side_effect=PermissionError("manifest remains locked"),
                ),
                patch("engine.core.source_storage.time.sleep") as sleep,
                self.assertRaises(PermissionError),
            ):
                session.complete()

            self.assertEqual(sleep.call_count, len(_WINDOWS_REPLACE_RETRY_DELAYS))
            staged_files = list(
                session.manifest_path.parent.glob(
                    f".{session.manifest_path.name}.*.tmp"
                )
            )
            self.assertEqual(staged_files, [])

    def test_market_lock_rejects_concurrent_refresh(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            with SourceRefreshLock("kr", data_lake_root=root):
                with self.assertRaises(RuntimeError):
                    SourceRefreshLock("kr", data_lake_root=root).acquire()

    def test_market_lock_recovers_dead_same_host_process(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            lock = SourceRefreshLock("us", data_lake_root=root)
            lock.path.parent.mkdir(parents=True)
            lock.path.write_text(
                json.dumps(
                    {
                        "market": "us",
                        "pid": 999_999_999,
                        "host": socket.gethostname(),
                        "created_at": "2020-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "engine.core.source_storage._process_is_running",
                return_value=False,
            ):
                with lock:
                    current = json.loads(lock.path.read_text(encoding="utf-8"))
                    self.assertEqual(current["pid"], __import__("os").getpid())
                    self.assertEqual(current["market"], "us")

            self.assertFalse(lock.path.exists())

    def test_market_lock_keeps_foreign_host_lock(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data-lake"
            lock = SourceRefreshLock("us", data_lake_root=root)
            lock.path.parent.mkdir(parents=True)
            lock.path.write_text(
                json.dumps(
                    {
                        "market": "us",
                        "pid": 999_999_999,
                        "host": "another-host",
                        "created_at": "2020-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                lock.acquire()

            self.assertTrue(lock.path.exists())


if __name__ == "__main__":
    unittest.main()
