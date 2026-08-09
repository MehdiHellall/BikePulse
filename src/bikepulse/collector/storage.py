from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bikepulse.io import atomic_write_bytes, atomic_write_text

SnapshotKind = Literal["status", "information"]


@dataclass(frozen=True)
class StoredSnapshot:
    snapshot_id: str
    kind: SnapshotKind
    collected_at_utc: datetime
    path: Path
    content_sha256: str


def snapshot_id(kind: SnapshotKind, collected_at_utc: datetime, content_sha256: str) -> str:
    stamp = collected_at_utc.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{stamp}-{content_sha256[:12]}"


def raw_snapshot_path(
    data_dir: Path, kind: SnapshotKind, collected_at_utc: datetime, snap_id: str
) -> Path:
    stamp = collected_at_utc.astimezone(UTC)
    return (
        data_dir
        / "raw"
        / kind
        / f"{stamp:%Y}"
        / f"{stamp:%m}"
        / f"{stamp:%d}"
        / f"{stamp:%H}"
        / f"{snap_id}.json.gz"
    )


def gzip_bytes(content: bytes) -> bytes:
    return gzip.compress(content, mtime=0)


def atomic_write_json_gz(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    atomic_write_raw_gz(path, content)


def atomic_write_raw_gz(path: Path, content: bytes) -> None:
    atomic_write_bytes(path, gzip_bytes(content), overwrite=False)


def read_json_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
        return payload


def store_snapshot(
    *,
    data_dir: Path,
    kind: SnapshotKind,
    collected_at_utc: datetime,
    payload: dict[str, Any],
    content_sha256: str,
    content: bytes | None = None,
) -> StoredSnapshot:
    snap_id = snapshot_id(kind, collected_at_utc, content_sha256)
    path = raw_snapshot_path(data_dir, kind, collected_at_utc, snap_id)
    if content is None:
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    atomic_write_raw_gz(path, content)
    return StoredSnapshot(
        snapshot_id=snap_id,
        kind=kind,
        collected_at_utc=collected_at_utc,
        path=path,
        content_sha256=content_sha256,
    )


def poll_record_path(data_dir: Path, collected_at_utc: datetime, snap_id: str) -> Path:
    stamp = collected_at_utc.astimezone(UTC)
    return data_dir / "polls" / f"{stamp:%Y-%m-%d}" / f"{snap_id}.json"


def write_poll_record(
    *,
    data_dir: Path,
    stored: StoredSnapshot,
    url: str,
    latency_ms: int,
    attempts: int,
    status_code: int,
    valid_record_count: int,
    error_count: int,
    error_codes: list[str],
) -> Path:
    record = {
        "snapshot_id": stored.snapshot_id,
        "kind": stored.kind,
        "collected_at_utc": stored.collected_at_utc.isoformat(),
        "url": url,
        "path": str(stored.path),
        "content_sha256": stored.content_sha256,
        "latency_ms": latency_ms,
        "attempts": attempts,
        "status_code": status_code,
        "valid_record_count": valid_record_count,
        "error_count": error_count,
        "error_codes": error_codes,
    }
    path = poll_record_path(data_dir, stored.collected_at_utc, stored.snapshot_id)
    atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True))
    return path
