"""Display composition from stored snapshots — honest states end to end."""

from datetime import date, datetime

from app.core import db, snapshots
from app.display.service import compose_board
from tests.test_weather_adapters import synthetic_payload


def setup_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.migrate()
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO location (label, latitude, longitude, is_primary)"
                     " VALUES ('Home', 40.68, -73.47, 1)")
    return conn


def store_weather(conn, day, fetched_at=None):
    hourly, daily = synthetic_payload(day)
    snapshots.save(conn, 1, "open_meteo", {"hourly": hourly, "daily": daily},
                   now=fetched_at)


def test_fresh_board_composes_real_message(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    day = date(2026, 7, 14)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, day, fetched_at=now)
    snapshots.save(conn, 1, "nws", {"features": []}, now=now)
    ctx = compose_board(conn, now=now)
    assert "Thunderstorms" in ctx["primary_msg"]
    assert ctx["weather_state"] == "fresh"
    assert ctx["alerts_state"] == "fresh" and ctx["alerts"] == []
    assert "as of 7:00 AM" in ctx["stamp_weather"]


def test_stale_weather_is_labeled_never_blank(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    day = date(2026, 7, 14)
    fetched = datetime(2026, 7, 14, 2, 0)
    now = datetime(2026, 7, 14, 7, 0)           # 5h old > 2h stale threshold
    store_weather(conn, day, fetched_at=fetched)
    ctx = compose_board(conn, now=now)
    assert ctx["weather_state"] == "stale"
    assert "stale" in ctx["stamp_weather"]
    assert ctx["primary_msg"]                    # still composed from cache


def test_missing_alerts_render_unknown_not_none(tmp_path, monkeypatch):
    """SAFETY: no NWS snapshot must never claim 'no active alerts'."""
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, date(2026, 7, 14), fetched_at=now)
    ctx = compose_board(conn, now=now)
    assert ctx["alerts_state"] == "unknown"


def test_warning_suppresses_low_bands_on_board(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, date(2026, 7, 14), fetched_at=now)
    snapshots.save(conn, 1, "nws", {"features": [{"properties": {
        "event": "Severe Thunderstorm Warning", "severity": "Severe",
        "headline": "Take cover", "ends": None}}]}, now=now)
    ctx = compose_board(conn, now=now)
    assert ctx["alerts"][0]["event"] == "Severe Thunderstorm Warning"
    # T7 (76) survives; P-band mugginess etc. suppressed
    assert all("Humid" not in m for m in ctx["secondary_msgs"])


def test_expired_announcements_absent(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    with conn:
        conn.execute("INSERT INTO announcement (text, priority, created_at, expires_at)"
                     " VALUES ('gone', 0, '2026-07-13T06:00:00', '2026-07-13T18:00:00')")
        conn.execute("INSERT INTO announcement (text, priority, created_at, expires_at)"
                     " VALUES ('trash night', 1, ?, NULL)",
                     (now.isoformat(timespec='seconds'),))
    ctx = compose_board(conn, now=now)
    texts = [a["text"] for a in ctx["announcements"]]
    assert "trash night" in texts and "gone" not in texts


def test_disabled_seed_rule_does_not_fire(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, date(2026, 7, 14), fetched_at=now)
    with conn:
        conn.execute("INSERT INTO signal_rule (seed_id, name, adapter, condition_json,"
                     " output_text, priority, is_seed) VALUES"
                     " ('T7', 'storms', 'open_meteo', '{}', 'x', 76, 1)")
        rid = conn.execute("SELECT id FROM signal_rule WHERE seed_id='T7'").fetchone()["id"]
        conn.execute("INSERT INTO rule_user_state (rule_id, enabled) VALUES (?, 0)", (rid,))
    ctx = compose_board(conn, now=now)
    assert "Thunderstorms" not in (ctx["primary_msg"] or "")
