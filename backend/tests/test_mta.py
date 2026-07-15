"""C1 Line Status: MTA normalize, card views, board integration, poll gating."""

from datetime import date, datetime

from app.adapters.mta import MTAAdapter, line_view
from app.core import db, snapshots
from app.display.service import compose_board
from app.jobs.scheduler import _active_adapters
from tests.test_display_service import setup_conn, store_weather

FIXTURE = {"feeds": {
    "subway": {"entity": [
        {"alert": {
            "transit_realtime.mercury_alert": {"alert_type": "Delays"},
            "header_text": {"translation": [
                {"text": "F trains running with delays after signal problems."}]},
            "informed_entity": [{"route_id": "F"}]}},
        {"alert": {
            "transit_realtime.mercury_alert": {"alert_type": "Planned Work"},
            "header_text": {"translation": [{"text": "A trains skip stops overnight."}]},
            "informed_entity": [{"route_id": "A"}]}},
    ]},
    "lirr": {"entity": [
        {"alert": {
            "transit_realtime.mercury_alert": {"alert_type": "Service Change"},
            "header_text": {"translation": [{"text": "Babylon branch: 8:02 canceled."}]},
            "informed_entity": [{"route_id": "5"}]}},
    ]},
}, "errors": []}


def test_normalize_classifies_and_rolls_up_rail():
    out = MTAAdapter().normalize(FIXTURE)
    assert out["lines"]["F"]["status"] == "delays"
    assert out["lines"]["A"]["status"] == "planned_work"
    assert out["lines"]["LIRR"]["status"] == "service_change"   # feed-level rollup


def test_line_view_states_and_attention():
    norm = MTAAdapter().normalize(FIXTURE)
    f = line_view("F", norm)
    assert f["label"] == "Delays" and f["attention"] == "amber"
    assert f["color"] == "#ff6319"
    q = line_view("Q", norm)                    # not in any alert => good service
    assert q["status"] == "good" and q["attention"] is None and q["dark_text"]
    lirr = line_view("LIRR", norm)
    assert lirr["attention"] == "amber" and "8:02" in norm["lines"]["LIRR"]["headline"]


def _monitor(conn, line):
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field, created_at)"
                     " VALUES ('mta', ?, '2026-07-15T06:00:00')", (line,))


def test_board_renders_line_cards(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 15, 7, 30)
    store_weather(conn, date(2026, 7, 15), fetched_at=now)
    _monitor(conn, "F")
    _monitor(conn, "Q")
    snapshots.save(conn, 1, "mta", FIXTURE, now=now)
    ctx = compose_board(conn, now=now)
    by_line = {t["line"]: t for t in ctx["transit"]}
    assert by_line["F"]["label"] == "Delays"
    assert by_line["Q"]["label"].endswith("Good service")
    assert "as of 7:30 AM" in ctx["stamp_transit"]


def test_no_snapshot_shows_unknown_never_good(tmp_path, monkeypatch):
    """Honesty: without data, a line must not claim good service."""
    conn = setup_conn(tmp_path, monkeypatch)
    _monitor(conn, "F")
    ctx = compose_board(conn, now=datetime(2026, 7, 15, 7, 30))
    assert ctx["transit"][0]["label"] == "Status unknown"
    assert ctx["transit"][0]["attention"] is None


def test_mta_polls_only_when_monitored(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "connect", lambda: conn)
    names = [a.manifest.name for a in _active_adapters(conn)]
    assert "mta" not in names
    _monitor(conn, "F")
    names = [a.manifest.name for a in _active_adapters(conn)]
    assert "mta" in names
