"""Status pip truth table (plan §7.5) + alert severity tiers on the board."""

from datetime import date, datetime

from app.core import snapshots
from app.display.service import compose_board
from tests.test_display_service import setup_conn, store_weather


def test_pip_green_when_all_fresh(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, date(2026, 7, 14), fetched_at=now)
    snapshots.save(conn, 1, "nws", {"features": []}, now=now)
    ctx = compose_board(conn, now=now)
    assert ctx["pip"]["state"] == "green"


def test_pip_amber_when_stale(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, date(2026, 7, 14), fetched_at=datetime(2026, 7, 14, 2, 0))
    snapshots.save(conn, 1, "nws", {"features": []}, now=now)
    ctx = compose_board(conn, now=now)
    assert ctx["pip"]["state"] == "amber"


def test_pip_red_when_nothing_usable(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    ctx = compose_board(conn, now=datetime(2026, 7, 14, 7, 0))
    assert ctx["pip"]["state"] == "red"


def test_severity_tiers_and_attention(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, date(2026, 7, 14), fetched_at=now)
    snapshots.save(conn, 1, "nws", {"features": [
        {"properties": {"event": "Heat Advisory", "severity": "Moderate",
                        "headline": "Heat index near 100", "ends": None}},
        {"properties": {"event": "Tornado Warning", "severity": "Extreme",
                        "headline": "Take shelter", "ends": None}},
        {"properties": {"event": "Flood Watch", "severity": "Moderate",
                        "headline": "Through tonight", "ends": None}},
    ]}, now=now)
    ctx = compose_board(conn, now=now)
    tiers = {a["event"]: a["tier"] for a in ctx["alerts"]}
    assert tiers == {"Tornado Warning": "warning", "Flood Watch": "watch",
                     "Heat Advisory": "advisory"}
    assert ctx["alerts_attention"] == "red"
    assert ctx["alerts"][0]["event"] == "Tornado Warning"  # severity-sorted first
