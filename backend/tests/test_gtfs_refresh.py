"""Quiet 90-day GTFS stop-data auto-refresh (scheduler)."""

import asyncio
import os
import time

from app.core import snapshots
from app.jobs import scheduler
from app.transit import gtfs
from tests.test_display_service import setup_conn


def _plant_dataset(tmp_path, monkeypatch, age_days: float):
    path = tmp_path / "gtfs.json"
    path.write_text('{"routes": {}}')
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))
    monkeypatch.setattr(gtfs, "DATA_FILE", path)
    gtfs._cache["mtime"] = None
    return path


def test_refresh_fires_after_90_days_once_per_day(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    _plant_dataset(tmp_path, monkeypatch, age_days=91)
    calls = []
    monkeypatch.setattr(gtfs, "build_dataset", lambda: calls.append(1))
    asyncio.run(scheduler._maybe_refresh_gtfs(conn, engaged=True))
    assert calls == [1]
    asyncio.run(scheduler._maybe_refresh_gtfs(conn, engaged=True))
    assert calls == [1]                       # daily attempt guard


def test_refresh_skips_fresh_idle_and_missing(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(gtfs, "build_dataset", lambda: calls.append(1))
    # fresh dataset: nothing to do
    _plant_dataset(tmp_path, monkeypatch, age_days=30)
    asyncio.run(scheduler._maybe_refresh_gtfs(conn, engaged=True))
    # old but nobody watching: stay quiet
    _plant_dataset(tmp_path, monkeypatch, age_days=91)
    asyncio.run(scheduler._maybe_refresh_gtfs(conn, engaged=False))
    # household never downloaded stop data: never download on our own
    monkeypatch.setattr(gtfs, "DATA_FILE", tmp_path / "nope.json")
    asyncio.run(scheduler._maybe_refresh_gtfs(conn, engaged=True))
    assert calls == []


def test_refresh_failure_logged_not_raised(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    _plant_dataset(tmp_path, monkeypatch, age_days=91)

    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(gtfs, "build_dataset", boom)
    asyncio.run(scheduler._maybe_refresh_gtfs(conn, engaged=True))   # no raise
    row = conn.execute("SELECT * FROM event_log WHERE service='gtfs'").fetchone()
    assert row["event_type"] == "refresh_failed"
    # attempt recorded even on failure — no hammering a down endpoint
    assert snapshots.kv_get(conn, "gtfs_refresh_attempt", None)
