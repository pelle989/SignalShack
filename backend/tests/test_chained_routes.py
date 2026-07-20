"""Duplicate line monitors + chained multi-leg routes."""

import json
from datetime import datetime

from app.core import db, snapshots
from app.display.service import compose_board
from app.jobs.scheduler import _active_adapters
from tests.test_admin import client, do_setup, get_csrf
from tests.test_display_service import setup_conn

# extend the test dataset with an R line for chaining; "t" = scheduled
# seconds from the canonical trip's start (powers chain time estimates)
R_DATASET = {"routes": {
    "F": [{"id": "F18", "name": "Bergen St", "t": 0},
          {"id": "F20", "name": "Jay St-MetroTech", "t": 240},
          {"id": "F24", "name": "7 Av", "t": 600}],
    "R": [{"id": "R36", "name": "25 St", "t": 0},
          {"id": "R31", "name": "Atlantic Av-Barclays Ctr", "t": 360},
          {"id": "R28", "name": "Court St", "t": 660}],
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


def test_transit_summary_route_worst_wins(tmp_path, monkeypatch):
    """Combined card headline + border come from the worst entry."""
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 30)
    add_route(conn)
    snapshots.save(conn, 1, "mta", R_DELAYS, now=now)
    ctx = compose_board(conn, now=now)
    s = ctx["transit_summary"]
    assert s["status"] == "delays" and s["attention"] == "amber"
    # headline names the culprit LEG, not the verdict (the row shows that)
    assert s["headline"] == "Work commute — R leg delays"
    assert s["sub"] == "R trains delayed."


def test_transit_summary_line_worst_is_named(tmp_path, monkeypatch):
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 30)
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field) VALUES ('mta', 'R')")
        conn.execute("INSERT INTO monitor (adapter, field) VALUES ('mta', 'F')")
    snapshots.save(conn, 1, "mta", R_DELAYS, now=now)
    ctx = compose_board(conn, now=now)
    s = ctx["transit_summary"]
    assert s["headline"] == "R train — Delays"
    assert s["sub"] == "R trains delayed."      # MTA headline as subtext
    assert s["attention"] == "amber"


def test_transit_summary_all_good(tmp_path, monkeypatch):
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 30)
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field) VALUES ('mta', 'F')")
    add_route(conn)
    snapshots.save(conn, 1, "mta", {"feeds": {}, "errors": []}, now=now)
    ctx = compose_board(conn, now=now)
    s = ctx["transit_summary"]
    assert s["status"] == "good" and s["attention"] is None
    assert "Good service" in s["headline"]


def test_transit_summary_unavailable_is_honest(tmp_path, monkeypatch):
    """No MTA snapshot => 'Status unknown', never a green all-clear."""
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field) VALUES ('mta', 'R')")
    ctx = compose_board(conn, now=datetime(2026, 7, 16, 7, 30))
    s = ctx["transit_summary"]
    assert s["status"] == "unknown" and s["headline"] == "Status unknown"
    assert s["attention"] is None


def test_chain_time_estimate_scheduled(tmp_path, monkeypatch):
    """Ride time per leg from the timetable + 5 min per transfer."""
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 30)
    add_route(conn)
    snapshots.save(conn, 1, "mta", {"feeds": {}, "errors": []}, now=now)
    ctx = compose_board(conn, now=now)
    # R 25 St→Barclays 360s + F Bergen→7 Av 600s = 16 min ride + 5 transfer
    assert ctx["transit_routes"][0]["est_min"] == 21


def test_chain_estimate_absent_on_untimed_dataset(tmp_path, monkeypatch):
    """Dataset from before time capture (no 't') => no estimate, no crash."""
    from app.transit import gtfs
    old = {"routes": {r: [{k: s[k] for k in ("id", "name")} for s in stops]
                      for r, stops in R_DATASET["routes"].items()}}
    path = tmp_path / "gtfs_old.json"
    path.write_text(json.dumps(old))
    monkeypatch.setattr(gtfs, "DATA_FILE", path)
    gtfs._cache["mtime"] = None
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 30)
    add_route(conn)
    snapshots.save(conn, 1, "mta", {"feeds": {}, "errors": []}, now=now)
    ctx = compose_board(conn, now=now)
    assert ctx["transit_routes"][0]["est_min"] is None


def test_travel_seconds_and_secs_parser(tmp_path, monkeypatch):
    from app.transit import gtfs
    use_r_dataset(tmp_path, monkeypatch)
    assert gtfs.travel_seconds("R", "R36", "R31") == 360
    assert gtfs.travel_seconds("R", "R31", "R36") == 360   # direction-agnostic
    assert gtfs.travel_seconds("R", "R36", "XX") is None
    assert gtfs._secs("07:32:30") == 7 * 3600 + 32 * 60 + 30
    assert gtfs._secs("25:01:00") == 25 * 3600 + 60        # >24h trips exist
    assert gtfs._secs("") is None and gtfs._secs(None) is None


def test_build_dataset_captures_time_offsets(tmp_path, monkeypatch):
    """Synthetic GTFS zip -> dataset with per-stop scheduled offsets."""
    import io
    import zipfile

    from app.transit import gtfs
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("stops.txt",
                    "stop_id,stop_name\nR36,25 St\nR36N,25 St\nR31,Barclays\n")
        zf.writestr("trips.txt",
                    "trip_id,route_id,direction_id\ntrip1,R,0\ntrip2,R,1\n")
        zf.writestr("stop_times.txt",
                    "trip_id,stop_id,stop_sequence,arrival_time\n"
                    "trip1,R36N,1,07:00:00\ntrip1,R31N,2,07:06:00\n")

    class FakeResp:
        content = buf.getvalue()
        def raise_for_status(self):
            pass

    monkeypatch.setattr(gtfs.httpx, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(gtfs, "DATA_FILE", tmp_path / "out.json")
    gtfs._cache["mtime"] = None
    summary = gtfs.build_dataset()
    assert summary["routes"] == 1
    stops = gtfs.stops_for("R")
    assert [s["id"] for s in stops] == ["R36", "R31"]     # platform suffix folded
    assert stops[0]["t"] == 0 and stops[1]["t"] == 360    # offsets from start
    assert gtfs.travel_seconds("R", "R36", "R31") == 360


def test_route_edit_via_admin(tmp_path, monkeypatch):
    use_r_dataset(tmp_path, monkeypatch)
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        c.post("/admin/transit/route", data={
            "csrf": csrf, "route_name": "Work commute",
            "leg_line": ["R", "F"], "leg_from": ["R36", "F18"],
            "leg_to": ["R31", "F24"]})
        conn = db.connect()
        rid = conn.execute("SELECT id FROM commute_profile").fetchone()["id"]
        conn.close()
        # edit form pre-fills name and selections
        page = c.get(f"/admin/transit?edit={rid}")
        assert "Save changes" in page.text
        assert 'value="Work commute"' in page.text
        assert 'value="R36" selected' in page.text
        # update: reversed direction + new name
        c.post(f"/admin/transit/route/{rid}", data={
            "csrf": csrf, "route_name": "Home commute",
            "leg_line": ["F", "R"], "leg_from": ["F24", "R31"],
            "leg_to": ["F18", "R36"]})
        conn = db.connect()
        row = conn.execute("SELECT name, legs_json FROM commute_profile"
                           " WHERE id=?", (rid,)).fetchone()
        legs = json.loads(row["legs_json"])
        assert row["name"] == "Home commute"
        assert legs[0]["line"] == "F" and legs[0]["from_name"] == "7 Av"
        # invalid edit (single leg) leaves the route untouched
        c.post(f"/admin/transit/route/{rid}", data={
            "csrf": csrf, "route_name": "Broken",
            "leg_line": "R", "leg_from": "R36", "leg_to": "R31"})
        row = conn.execute("SELECT name FROM commute_profile WHERE id=?",
                           (rid,)).fetchone()
        assert row["name"] == "Home commute"
        conn.close()


def test_scheduler_wakes_for_routes_alone(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    assert "mta" not in [a.manifest.name for a in _active_adapters(conn)]
    add_route(conn)
    assert "mta" in [a.manifest.name for a in _active_adapters(conn)]
