"""Duplicate line monitors + chained multi-leg routes."""

import json
from datetime import datetime

from app.core import db, snapshots
from app.display.service import compose_board
from app.jobs.scheduler import _active_adapters
from tests.test_admin import client, do_setup, get_csrf
from tests.test_display_service import setup_conn

# extend the test dataset with an R line for chaining
R_DATASET = {"routes": {
    "F": [{"id": "F18", "name": "Bergen St"},
          {"id": "F20", "name": "Jay St-MetroTech"},
          {"id": "F24", "name": "7 Av"}],
    "R": [{"id": "R36", "name": "25 St"},
          {"id": "R31", "name": "Atlantic Av-Barclays Ctr"},
          {"id": "R28", "name": "Court St"}],
}}

R_DELAYS = {"feeds": {"subway": {"entity": [
    {"alert": {
        "transit_realtime.mercury_alert": {"alert_type": "Delays"},
        "header_text": {"translation": [{"text": "R trains delayed."}]},
        "informed_entity": [{"route_id": "R"}]}},
]}}, "errors": []}


def use_r_dataset(tmp_path, monkeypatch):
    from app.transit import gtfs
    path = tmp_path / "gtfs.json"
    path.write_text(json.dumps(R_DATASET))
    monkeypatch.setattr(gtfs, "DATA_FILE", path)
    gtfs._cache["mtime"] = None


LEGS = [{"line": "R", "from_id": "R36", "to_id": "R31",
         "from_name": "25 St", "to_name": "Atlantic Av-Barclays Ctr"},
        {"line": "F", "from_id": "F18", "to_id": "F24",
         "from_name": "Bergen St", "to_name": "7 Av"}]


def add_route(conn, name="Work commute"):
    with conn:
        conn.execute("INSERT INTO commute_profile (name, mode, legs_json,"
                     " actively_monitored) VALUES (?, 'train', ?, 1)",
                     (name, json.dumps(LEGS)))


def test_duplicate_line_monitors_allowed(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        c.post("/admin/transit", data={"line": "R", "csrf": csrf})
        c.post("/admin/transit", data={"line": "R", "csrf": csrf})
        conn = db.connect()
        n = conn.execute("SELECT COUNT(*) c FROM monitor WHERE field='R'"
                         ).fetchone()["c"]
        conn.close()
        assert n == 2                       # two R commutes, two cards


def test_route_card_combines_leg_statuses(tmp_path, monkeypatch):
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 30)
    add_route(conn)
    snapshots.save(conn, 1, "mta", R_DELAYS, now=now)
    ctx = compose_board(conn, now=now)
    route = ctx["transit_routes"][0]
    assert route["name"] == "Work commute"
    assert route["status"] == "delays"              # worst leg wins
    assert route["label"] == "Delays en route"
    by_line = {leg["line"]: leg for leg in route["legs"]}
    assert by_line["R"]["status"] == "delays"       # the culprit is visible
    assert by_line["F"]["status"] == "good"
    assert by_line["R"]["from_name"] == "25 St"
    assert "transit" in ctx["layout"]               # routes alone light the card


def test_route_save_and_delete_via_admin(tmp_path, monkeypatch):
    use_r_dataset(tmp_path, monkeypatch)
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        c.post("/admin/transit/route", data={
            "csrf": csrf, "route_name": "Work commute",
            "leg_line": ["R", "F"], "leg_from": ["R36", "F18"],
            "leg_to": ["R31", "F24"]})
        conn = db.connect()
        row = conn.execute("SELECT id, legs_json FROM commute_profile").fetchone()
        legs = json.loads(row["legs_json"])
        assert len(legs) == 2 and legs[0]["from_name"] == "25 St"
        # single-leg submission rejected (a chain needs 2+)
        c.post("/admin/transit/route", data={
            "csrf": csrf, "route_name": "Short",
            "leg_line": "R", "leg_from": "R36", "leg_to": "R31"})
        assert conn.execute("SELECT COUNT(*) c FROM commute_profile"
                            ).fetchone()["c"] == 1
        c.post(f"/admin/transit/route/{row['id']}/delete", data={"csrf": csrf})
        assert conn.execute("SELECT COUNT(*) c FROM commute_profile"
                            ).fetchone()["c"] == 0
        conn.close()


def test_scheduler_wakes_for_routes_alone(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    assert "mta" not in [a.manifest.name for a in _active_adapters(conn)]
    add_route(conn)
    assert "mta" in [a.manifest.name for a in _active_adapters(conn)]
