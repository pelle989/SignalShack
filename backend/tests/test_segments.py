"""Segment-scoped line monitoring: gtfs dataset, alert filtering, board wiring."""

import json
from datetime import datetime

from app.adapters.mta import MTAAdapter, line_view
from app.core import db, snapshots
from app.display.service import compose_board
from app.transit import gtfs
from tests.test_display_service import setup_conn

DATASET = {"routes": {"F": [
    {"id": "F14", "name": "2 Av"}, {"id": "F18", "name": "Bergen St"},
    {"id": "F20", "name": "Jay St-MetroTech"}, {"id": "F24", "name": "7 Av"},
    {"id": "F35", "name": "Church Av"}, {"id": "F39", "name": "Kings Hwy"},
]}}

FEEDS = {"feeds": {"subway": {"entity": [
    {"alert": {   # line-wide delays: always relevant
        "transit_realtime.mercury_alert": {"alert_type": "Delays"},
        "header_text": {"translation": [{"text": "F trains running with delays."}]},
        "informed_entity": [{"route_id": "F"}]}},
    {"alert": {   # planned work scoped to the far end of the line
        "transit_realtime.mercury_alert": {"alert_type": "Planned Work"},
        "header_text": {"translation": [{"text": "Kings Hwy station work."}]},
        "informed_entity": [{"route_id": "F"},
                            {"stop_id": "F39N"}, {"stop_id": "F39S"}]}},
]}}, "errors": []}

SCOPED_ONLY = {"feeds": {"subway": {"entity": [
    {"alert": {
        "transit_realtime.mercury_alert": {"alert_type": "Planned Work"},
        "header_text": {"translation": [{"text": "Kings Hwy station work."}]},
        "informed_entity": [{"route_id": "F"}, {"stop_id": "F39N"}]}},
]}}, "errors": []}


def use_dataset(tmp_path, monkeypatch):
    path = tmp_path / "gtfs.json"
    path.write_text(json.dumps(DATASET))
    monkeypatch.setattr(gtfs, "DATA_FILE", path)
    gtfs._cache["mtime"] = None


def test_parent_and_segment(tmp_path, monkeypatch):
    use_dataset(tmp_path, monkeypatch)
    assert gtfs.parent("F20N") == "F20" and gtfs.parent("F20") == "F20"
    seg = gtfs.segment_ids("F", "F18", "F24")
    assert seg == {"F18", "F20", "F24"}
    assert gtfs.segment_ids("F", "F24", "F18") == seg      # direction-agnostic
    assert gtfs.segment_ids("F", "F18", "NOPE") is None    # unknown -> fallback


def test_segment_filters_scoped_alerts():
    norm = MTAAdapter().normalize(FEEDS)
    # whole line: worst is delays (line-wide beats planned work in rank)
    assert line_view("F", norm)["status"] == "delays"
    # segment Bergen->7 Av: Kings Hwy work is outside; line-wide delays remain
    seg = {"F18", "F20", "F24"}
    assert line_view("F", norm, segment=seg)["status"] == "delays"
    # scoped-only feed: outside-segment work suppressed entirely
    norm2 = MTAAdapter().normalize(SCOPED_ONLY)
    assert line_view("F", norm2)["status"] == "planned_work"     # whole line sees it
    assert line_view("F", norm2, segment=seg)["status"] == "good"  # segment doesn't
    # rider AT Kings Hwy still sees it
    assert line_view("F", norm2, segment={"F35", "F39"})["status"] == "planned_work"


def test_board_uses_segment_config(tmp_path, monkeypatch):
    use_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 30)
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field, config_json, created_at)"
                     " VALUES ('mta', 'F', ?, '2026-07-16T06:00:00')",
                     (json.dumps({"from_id": "F18", "to_id": "F24",
                                  "from_name": "Bergen St", "to_name": "7 Av"}),))
    snapshots.save(conn, 1, "mta", SCOPED_ONLY, now=now)
    ctx = compose_board(conn, now=now)
    card = ctx["transit"][0]
    assert card["status"] == "good"                       # far-end work filtered
    assert card["segment_label"] == "Bergen St → 7 Av"


def test_segment_route_saves_and_clears(tmp_path, monkeypatch):
    from tests.test_admin import client, do_setup, get_csrf
    use_dataset(tmp_path, monkeypatch)
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        c.post("/admin/transit", data={"line": "F", "csrf": csrf})
        conn = db.connect()
        mid = conn.execute("SELECT id FROM monitor").fetchone()["id"]
        c.post(f"/admin/transit/{mid}/segment",
               data={"from_stop": "F18", "to_stop": "F24", "csrf": csrf})
        cfg = json.loads(conn.execute("SELECT config_json FROM monitor"
                                      " WHERE id=?", (mid,)).fetchone()["config_json"])
        assert cfg["from_name"] == "Bergen St" and cfg["to_id"] == "F24"
        # invalid pair rejected (same stop) — config unchanged
        c.post(f"/admin/transit/{mid}/segment",
               data={"from_stop": "F18", "to_stop": "F18", "csrf": csrf})
        assert conn.execute("SELECT config_json FROM monitor WHERE id=?",
                            (mid,)).fetchone()["config_json"] is not None
        # blank clears
        c.post(f"/admin/transit/{mid}/segment",
               data={"from_stop": "", "to_stop": "", "csrf": csrf})
        assert conn.execute("SELECT config_json FROM monitor WHERE id=?",
                            (mid,)).fetchone()["config_json"] is None
        conn.close()
