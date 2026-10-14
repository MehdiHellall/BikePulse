from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bikepulse.collector import manage, worker
from bikepulse.config import load_settings


def test_schedule_wakes_before_boundary_and_skips_missed_intervals() -> None:
    assert worker.seconds_until_poll(datetime(2026, 8, 1, 12, 0, tzinfo=UTC)) == 270
    assert worker.seconds_until_poll(datetime(2026, 8, 1, 12, 4, 29, tzinfo=UTC)) == 1
    assert worker.seconds_until_poll(datetime(2026, 8, 1, 12, 4, 30, tzinfo=UTC)) == 300


def test_information_failure_does_not_prevent_status_capture(
    settings_config: Path,
    monkeypatch,
    caplog,
) -> None:
    settings = load_settings(settings_config)
    calls = []

    def fake_run(kind, settings):
        calls.append(kind)
        if kind == "information":
            raise RuntimeError("information offline")
        return SimpleNamespace(snapshot_id="test")

    monkeypatch.setattr(worker, "run_once", fake_run)
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    assert worker.collect_cycle(settings, refresh_information=True) is False
    assert calls == ["information", "status"]
    records = list((settings.paths.data_dir / "failures").rglob("*.json"))
    assert json.loads(records[0].read_text())["kind"] == "information"
    assert "5 GiB" in caplog.text
    monkeypatch.setattr(worker, "run_once", lambda *args: SimpleNamespace(snapshot_id="ok"))
    assert worker.collect_cycle(settings, refresh_information=False)


def test_worker_refreshes_once_and_survives_compaction_failure(
    settings_config: Path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        worker,
        "collect_cycle",
        lambda s, refresh_information: calls.append(refresh_information) or True,
    )
    monkeypatch.setattr(worker, "compact_status_hour", lambda *args: None)
    monkeypatch.setattr(worker, "build_daily_quality_report", lambda **kwargs: {})
    readiness_calls = []
    monkeypatch.setattr(
        worker,
        "refresh_readiness_reports",
        lambda _settings, days: readiness_calls.append(days) or set(days),
    )
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        worker.run_worker(settings_config)
    assert calls == [True, False]
    assert len(readiness_calls) == 1
    assert len(readiness_calls[0]) == 2

    def broken(*args):
        raise RuntimeError("disk failure")

    monkeypatch.setattr(worker, "compact_status_hour", broken)
    with pytest.raises(KeyboardInterrupt):
        worker.run_worker(settings_config)
    config = settings_config.read_text().replace(
        "expected_interval_minutes = 5", "expected_interval_minutes = 10"
    )
    settings_config.write_text(config)
    with pytest.raises(ValueError, match="five-minute"):
        worker.run_worker(settings_config)


def test_user_service_commands_are_local_and_do_not_enable_boot_start(
    settings_config: Path,
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    commands = []
    monkeypatch.setattr(manage.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(manage.subprocess, "run", lambda args, **kwargs: commands.append(args))
    for action in ["start", "status", "stop"]:
        monkeypatch.setattr("sys.argv", ["manage", action, "--config", str(settings_config)])
        manage.main()
    assert ["systemctl", "--user", "start", manage.SERVICE] in commands
    assert ["systemctl", "--user", "stop", manage.SERVICE] in commands
    assert all("enable" not in command for command in commands)
    unit = tmp_path / ".config/systemd/user" / manage.SERVICE
    text = unit.read_text()
    assert "WorkingDirectory=/" in text
    assert "--config" in text
    assert "48h audit" in capsys.readouterr().out
