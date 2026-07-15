"""Secrets vault (envelope encryption) + AirNow adapter + AQ card."""

from datetime import date, datetime

from app.adapters.airnow import AirNowAdapter
from app.core import secrets, snapshots
from app.display.service import compose_board
from app.jobs.scheduler import _active_adapters
from tests.test_display_service import setup_conn, store_weather

OBS = {"observations": [
    {"ParameterName": "O3", "AQI": 42, "Category": {"Number": 1, "Name": "Good"}},
    {"ParameterName": "PM2.5", "AQI": 132,
     "Category": {"Number": 3, "Name": "Unhealthy for Sensitive Groups"}},
]}


# ---------------- secrets vault

def test_secret_roundtrip_and_ciphertext_at_rest(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    secrets.store(conn, "airnow", "MY-REAL-KEY-123", label="test")
    assert secrets.retrieve(conn, "airnow") == "MY-REAL-KEY-123"
    # at rest: ciphertext only, never plaintext
    row = conn.execute("SELECT ciphertext FROM secret").fetchone()
    assert b"MY-REAL-KEY-123" not in row["ciphertext"]
    raw_db = (tmp_path / "t.db").read_bytes()
    assert b"MY-REAL-KEY-123" not in raw_db
    # replace + delete
    secrets.store(conn, "airnow", "NEW-KEY")
    assert secrets.retrieve(conn, "airnow") == "NEW-KEY"
    secrets.delete(conn, "airnow")
    assert not secrets.exists(conn, "airnow")


def test_clean_pasted_key():
    assert secrets.clean_pasted_key("  API_KEY=abc-123 \n") == "abc-123"
    assert secrets.clean_pasted_key('"abc-123"') == "abc-123"
    assert secrets.clean_pasted_key("abc-123") == "abc-123"
    assert secrets.clean_pasted_key("   ") == ""


def test_stored_key_never_in_backup_or_diagnostics(tmp_path, monkeypatch):
    from app.core import backup, diagnostics
    conn = setup_conn(tmp_path, monkeypatch)
    secrets.store(conn, "airnow", "SUPER-SECRET-KEY-999")
    import json
    assert "SUPER-SECRET-KEY-999" not in json.dumps(backup.export_bundle(conn))
    assert b"SUPER-SECRET-KEY-999" not in diagnostics.build_bundle(conn)


# ---------------- adapter + card

def test_normalize_worst_pollutant_wins():
    out = AirNowAdapter().normalize(OBS)
    assert out["aqi"] == 132 and out["aqi_pollutant"] == "PM2.5"
    assert "sensitive groups" in out["meaning"]
    assert AirNowAdapter().normalize({"observations": []})["aqi"] is None


def test_board_shows_aq_card_only_when_key_exists(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 15, 7, 0)
    store_weather(conn, date(2026, 7, 15), fetched_at=now)
    assert compose_board(conn, now=now)["air"] is None       # no key: no card
    secrets.store(conn, "airnow", "k")
    snapshots.save(conn, 1, "airnow", OBS, now=now)
    air = compose_board(conn, now=now)["air"]
    assert air["aqi"] == 132 and air["state"] == "fresh"
    assert "windows" not in air["meaning"]                    # cat 3, not 4


def test_scheduler_gates_on_key(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    names = [a.manifest.name for a in _active_adapters(conn)]
    assert "airnow" not in names
    secrets.store(conn, "airnow", "k-123")
    adapters = _active_adapters(conn)
    by_name = {a.manifest.name: a for a in adapters}
    assert by_name["airnow"].api_key == "k-123"               # key injected
