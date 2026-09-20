"""Immutable raw snapshots, manifests, operational state, run lock, and run summaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StorageError(RuntimeError):
    """Raised for missing or corrupt snapshots and lock conflicts."""


class SnapshotMissing(StorageError):
    """An offline command needed a snapshot that has not been captured."""


class RunLocked(StorageError):
    """Another crawler invocation holds the run lock."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def combined_content_hash(file_hashes: dict[str, str]) -> str:
    """Deterministic hash over sorted relative filenames and their SHA-256 values."""
    digest = hashlib.sha256()
    for name in sorted(file_hashes):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hashes[name].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class SnapshotRef:
    market_source: str
    source_company_id: str
    notification_id: str
    content_hash: str
    path: Path

    @property
    def relative_path(self) -> str:
        return f"raw/{self.market_source}/{self.source_company_id}/{self.notification_id}/{self.content_hash}"


class RawStore:
    """data/raw/{source}/{company}/{notification}/{hash}/ with atomic directory publication."""

    MANIFEST = "metadata.json"

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "raw"
        self._tmp = self.root / ".tmp"

    def snapshot_dir(self, market_source: str, source_company_id: str, notification_id: str, content_hash: str) -> Path:
        for value in (market_source, source_company_id, notification_id, content_hash):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise StorageError(f"invalid snapshot identifier {value!r}")
        return self.root / market_source / source_company_id / notification_id / content_hash

    def exists(self, market_source: str, source_company_id: str, notification_id: str, content_hash: str) -> bool:
        target = self.snapshot_dir(market_source, source_company_id, notification_id, content_hash)
        return (target / self.MANIFEST).is_file()

    def write_snapshot(
        self,
        *,
        market_source: str,
        source_company_id: str,
        notification_id: str,
        files: dict[str, bytes],
        manifest: dict[str, Any],
    ) -> tuple[SnapshotRef, bool]:
        """Persist files plus manifest. Returns (ref, created). An existing identical
        snapshot is left untouched and created is False."""
        if not files:
            raise StorageError("refusing to write a snapshot with no files")
        for name in files:
            if "\\" in name or ":" in name or name in ("", ".") or name.startswith("/") or ".." in Path(name).parts or name == self.MANIFEST or name.startswith("parsed/"):
                raise StorageError(f"illegal snapshot filename {name!r}")
        file_hashes = {name: sha256_bytes(data) for name, data in files.items()}
        content_hash = combined_content_hash(file_hashes)
        final_dir = self.snapshot_dir(market_source, source_company_id, notification_id, content_hash)
        ref = SnapshotRef(market_source, source_company_id, notification_id, content_hash, final_dir)
        if (final_dir / self.MANIFEST).is_file():
            return ref, False

        full_manifest = {
            **manifest,
            "market_source": market_source,
            "source_company_id": source_company_id,
            "notification_id": notification_id,
            "content_hash": content_hash,
            "files": {name: {"sha256": digest, "size": len(files[name])} for name, digest in file_hashes.items()},
        }
        for forbidden in ("authorization", "cookie", "api_key", "password"):
            if any(forbidden in str(key).lower() for key in full_manifest):
                raise StorageError(f"manifest must not contain credentials ({forbidden})")

        self._tmp.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{notification_id}.", dir=self._tmp))
        try:
            for name, data in files.items():
                write_atomic(staging / name, data)
            write_atomic(staging / self.MANIFEST, dump_json(full_manifest).encode("utf-8"))
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(staging, final_dir)
            except (FileExistsError, OSError):
                if (final_dir / self.MANIFEST).is_file():
                    return ref, False
                raise
        finally:
            if staging.exists():
                for child in sorted(staging.rglob("*"), reverse=True):
                    child.unlink() if child.is_file() else child.rmdir()
                staging.rmdir()
        return ref, True

    def read_manifest(self, ref: SnapshotRef) -> dict[str, Any]:
        manifest_path = ref.path / self.MANIFEST
        if not manifest_path.is_file():
            raise SnapshotMissing(f"snapshot manifest missing: {manifest_path}")
        try:
            return json.loads(manifest_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"corrupt manifest {manifest_path}: {exc}") from exc

    def read_files(self, ref: SnapshotRef) -> dict[str, bytes]:
        manifest = self.read_manifest(ref)
        files: dict[str, bytes] = {}
        for name, meta in manifest.get("files", {}).items():
            file_path = ref.path / name
            if not file_path.is_file():
                raise SnapshotMissing(f"snapshot file missing: {file_path}")
            data = file_path.read_bytes()
            if sha256_bytes(data) != meta.get("sha256"):
                raise StorageError(f"hash mismatch for {file_path}; snapshot is corrupt")
            files[name] = data
        return files

    def write_parsed(self, ref: SnapshotRef, parser_version: str, payload: dict[str, Any]) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", parser_version) or ".." in parser_version:
            raise StorageError("invalid parser version")
        target = ref.path / "parsed" / f"{parser_version}.json"
        write_atomic(target, dump_json(payload).encode("utf-8"))
        return target

    def list_snapshots(self, market_source: str, source_company_id: str) -> list[SnapshotRef]:
        company_dir = self.root / market_source / source_company_id
        refs: list[SnapshotRef] = []
        if not company_dir.is_dir():
            return refs
        for notification_dir in company_dir.iterdir():
            if not notification_dir.is_dir():
                continue
            for hash_dir in notification_dir.iterdir():
                if (hash_dir / self.MANIFEST).is_file():
                    refs.append(SnapshotRef(market_source, source_company_id, notification_dir.name, hash_dir.name, hash_dir))
        return refs

    def snapshots_for_notification(self, market_source: str, source_company_id: str, notification_id: str) -> list[SnapshotRef]:
        return [ref for ref in self.list_snapshots(market_source, source_company_id) if ref.notification_id == notification_id]

    def latest_snapshot(self, market_source: str, source_company_id: str) -> SnapshotRef | None:
        """Latest by publication time, numeric notification id, then first retrieval time."""
        best: tuple[tuple, SnapshotRef] | None = None
        for ref in self.list_snapshots(market_source, source_company_id):
            manifest = self.read_manifest(ref)
            key = (
                manifest.get("published_at") or "",
                _numeric(ref.notification_id),
                ref.notification_id,
                manifest.get("first_retrieved_at") or "",
            )
            if best is None or key > best[0]:
                best = (key, ref)
        return best[1] if best else None


def _numeric(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return -1


class StateStore:
    """Small mutable operational state under data/state/: host cooldowns and revalidation."""

    def __init__(self, data_dir: Path, clock: Clock = utcnow) -> None:
        self.root = data_dir / "state"
        self.clock = clock
        self._hosts = self.root / "hosts.json"
        self._revalidation = self.root / "revalidation.json"

    def _load(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"corrupt operational state: {path}; restore or review it before retrying") from exc

    def _save(self, path: Path, payload: dict[str, Any]) -> None:
        write_atomic(path, dump_json(payload).encode("utf-8"))

    def host_cooldown(self, host: str) -> tuple[datetime | None, str | None]:
        entry = self._load(self._hosts).get(host)
        if not entry:
            return None, None
        until = datetime.fromisoformat(entry["next_allowed_at"])
        if until <= self.clock():
            return None, None
        return until, entry.get("reason")

    def set_host_cooldown(self, host: str, until: datetime, reason: str) -> None:
        payload = self._load(self._hosts)
        payload[host] = {"next_allowed_at": until.isoformat(), "reason": reason, "set_at": self.clock().isoformat()}
        self._save(self._hosts, payload)

    def set_host_blocked(self, host: str, reason: str) -> None:
        self.set_host_cooldown(host, self.clock() + timedelta(days=3650), f"blocked: {reason}")

    def clear_host(self, host: str) -> None:
        payload = self._load(self._hosts)
        if payload.pop(host, None) is not None:
            self._save(self._hosts, payload)

    def coverage(self, key: str) -> list[int]:
        """Fiscal years already collected for this company, across every past run."""
        entry = self._load(self._revalidation).get(f"coverage:{key}", {})
        return sorted(int(year) for year in entry.get("years", []))

    def add_coverage(self, key: str, years: list[int]) -> None:
        """Union `years` into this company's collected set. Freshness is judged against this,
        so changing KAP_YEARS makes the company due again instead of being skipped as fresh."""
        payload = self._load(self._revalidation)
        name = f"coverage:{key}"
        entry = payload.get(name, {})
        entry["years"] = sorted({int(y) for y in entry.get("years", [])} | {int(y) for y in years})
        entry["last_collected_at"] = self.clock().isoformat()
        collected = entry.setdefault("collected_at_by_year", {})
        for year in years:
            collected[str(year)] = self.clock().isoformat()
        payload[name] = entry
        self._save(self._revalidation, payload)

    def fresh_coverage(self, key: str, *, now: datetime, max_age: timedelta) -> set[int]:
        """A recent fetch of 2025 must not renew stale coverage of 2020.

        Legacy entries without per-year timestamps are deliberately due again.
        """
        entry = self._load(self._revalidation).get(f"coverage:{key}", {})
        return {
            int(year) for year, timestamp in entry.get("collected_at_by_year", {}).items()
            if timedelta(0) <= now - datetime.fromisoformat(timestamp) < max_age
        }

    def revalidation(self, key: str) -> dict[str, Any]:
        return self._load(self._revalidation).get(key, {})

    def set_revalidation(self, key: str, *, validators: dict[str, str] | None = None) -> None:
        payload = self._load(self._revalidation)
        entry = payload.get(key, {})
        entry["last_revalidated_at"] = self.clock().isoformat()
        if validators is not None:
            entry["validators"] = validators
        payload[key] = entry
        self._save(self._revalidation, payload)


class RunLock:
    """OS lock on a stable guard file; compatible with shell flock on Linux.

    Keep the guard inode in place. Metadata is diagnostic only. Process death releases
    the lock; elapsed time and container hostnames never authorize stealing it.
    """
    def __init__(self, data_dir: Path, *, stale_after: timedelta = timedelta(hours=6), clock: Clock = utcnow) -> None:
        self.path = data_dir / "state" / "crawler.lock"
        self.guard = self.path.with_name("crawler.guard")
        self.clock = clock
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.guard.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RunLocked(f"another crawler or backup holds {self.guard}") from exc
        self._handle = handle
        try:
            write_atomic(self.path, dump_json({"pid": os.getpid(), "hostname": socket.gethostname(), "acquired_at": self.clock()}).encode())
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self._handle is not None:
            self.path.unlink(missing_ok=True)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class RunSummary:
    """Compact JSON summary under data/runs/{run_id}/summary.json."""

    def __init__(self, data_dir: Path, command: str, clock: Clock = utcnow) -> None:
        self.clock = clock
        self.run_id = f"{clock().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.path = data_dir / "runs" / self.run_id / "summary.json"
        self.data: dict[str, Any] = {
            "run_id": self.run_id,
            "command": command,
            "started_at": clock().isoformat(),
            "companies": [],
            "request_attempts": 0,
            "pending": [],
            "stopped_reason": None,
        }

    def add_company(self, entry: dict[str, Any]) -> None:
        self.data["companies"].append(entry)

    def finish(self, **fields: Any) -> Path:
        self.data.update(fields)
        self.data["finished_at"] = self.clock().isoformat()
        write_atomic(self.path, dump_json(self.data).encode("utf-8"))
        return self.path

    def iter_changes(self) -> Iterator[dict[str, Any]]:
        for entry in self.data["companies"]:
            if entry.get("changed_fields"):
                yield entry
