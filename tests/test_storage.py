from __future__ import annotations

import gzip
from datetime import UTC, datetime

import pytest

from bikepulse.collector.storage import read_json_gz, store_snapshot


def test_store_snapshot_writes_readable_gzip_without_temp_files(tmp_path) -> None:
    raw_content = b'{"data":{"stations":[]},"ttl":60}'
    payload = {"data": {"stations": []}, "ttl": 60}
    stored = store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
        payload=payload,
        content_sha256="a" * 64,
        content=raw_content,
    )

    assert stored.path.exists()
    assert read_json_gz(stored.path) == payload
    with gzip.open(stored.path, "rb") as handle:
        assert handle.read() == raw_content
    assert list(stored.path.parent.glob("*.tmp")) == []


def test_repeated_content_creates_auditable_time_based_snapshots(tmp_path) -> None:
    first = store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
        payload={"same": True},
        content_sha256="b" * 64,
    )
    second = store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 21, 5, tzinfo=UTC),
        payload={"same": True},
        content_sha256="b" * 64,
    )

    assert first.snapshot_id != second.snapshot_id
    assert first.path != second.path


def test_store_snapshot_refuses_to_overwrite_same_snapshot_id(tmp_path) -> None:
    kwargs = {
        "data_dir": tmp_path,
        "kind": "status",
        "collected_at_utc": datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
        "payload": {"same": True},
        "content_sha256": "b" * 64,
    }
    store_snapshot(**kwargs)

    with pytest.raises(FileExistsError):
        store_snapshot(**kwargs)
