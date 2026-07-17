"""C2 slice 3 — live chain times from GTFS-RT trip updates."""

import json
from datetime import datetime

from app.adapters.mta_rt import MTARealtimeAdapter, feeds_for, live_chain
from app.core import snapshots
from app.display.service import compose_board
from app.jobs.scheduler import _active_adapters
from tests.test_chained_routes import LEGS, add_route, use_r_dataset
from tests.test_display_service import setup_conn

NOW = datetime(2026, 7, 16, 7, 30)
T0 = int(NOW.timestamp())

CFG_LEGS = [{"line": leg["line"], "from_id": leg["from_id"],
             "to_id": leg["to_id"]} for leg in LEGS]


def trips(offset=0):
    """R 25 St -> Barclays (6 min ride), transfer, F Bergen -> 7 Av (10 min)."""
    return [
        {"route": "R", "stops": {"R36": T0 + 300 + offset,
                                 "R31": T0 + 660 + offset}},
        {"route": "R", "stops": {"R36": T0 + 900, "R31": T0 + 1260}},
        {"route": "F", "stops": {"F18": T0 + 840 + offset,
                                 "F24": T0 + 1440 + offset}},
    ]


def test_live_chain_rides_next_available_trains():
    est = live_chain(CFG_LEGS, trips(), T0)
    # depart 7:35, R arrives +660, +2min walk, F departs +840, arrives +1440
    assert est["depart_epoch"] == T0 + 300
    assert est["arrive_epoch"] == T0 + 1440
    assert est["total_min"] == 19                    # (1440-300)/60
    # per-leg picks computed for EVERY leg, before the total
    assert [lg["ride_min"] for lg in est["legs"]] == [6, 10]
    assert est["legs"][1]["depart"] >= est["legs"][0]["arrive"] + 120


def test_live_chain_skips_departed_trains():
    later_f = {"route": "F", "stops": {"F18": T0 + 1500, "F24": T0 + 2100}}
    est = live_chain(CFG_LEGS, [*trips(), later_f], T0 + 400)  # first R gone
    assert est["depart_epoch"] == T0 + 900           # next R
    assert est["arrive_epoch"] == T0 + 2100          # first F misses transfer
    assert est["total_min"] == 20


def test_live_chain_none_when_leg_uncovered():
    only_r = [t for t in trips() if t["route"] == "R"]
    assert live_chain(CFG_LEGS, only_r, T0) is None
    # wrong direction (arrival before departure) is not a usable trip
    wrong = [{"route": "R", "stops": {"R36": T0 + 660, "R31": T0 + 300}},
             trips()[2]]
    assert live_chain(CFG_LEGS, wrong, T0) is None


def test_feeds_for_maps_lines_to_feed_urls():
    urls = feeds_for({"R", "F"})
    assert any(u.endswith("-nqrw") for u in urls)     # R
    assert any(u.endswith("-bdfm") for u in urls)     # F
    assert len(urls) == 2
    assert feeds_for({"1"}) == [feeds_for({"1"})[0]]  # main feed, no suffix
    assert not feeds_for(set())


def test_board_shows_live_estimate_when_fresh(tmp_path, monkeypatch):
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    add_route(conn)
    snapshots.save(conn, 1, "mta", {"feeds": {}, "errors": []}, now=NOW)
    snapshots.save(conn, 1, "mta_rt", {"trips": trips(), "errors": []}, now=NOW)
    ctx = compose_board(conn, now=NOW)
    route = ctx["transit_routes"][0]
    assert route["live"]["total_min"] == 19
    assert route["live"]["depart_label"] == "7:35"
    assert route["est_min"] == 21                    # scheduled kept alongside
    # per-leg live ride times on each row
    assert [(lg["ride_min"], lg.get("ride_live")) for lg in route["legs"]] \
        == [(6, True), (10, True)]


def test_board_falls_back_to_scheduled_when_rt_stale(tmp_path, monkeypatch):
    """SAFETY: a 10-minute-old 'next train' must not render as live."""
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    add_route(conn)
    snapshots.save(conn, 1, "mta", {"feeds": {}, "errors": []}, now=NOW)
    old = datetime(2026, 7, 16, 7, 20)
    snapshots.save(conn, 1, "mta_rt", {"trips": trips(), "errors": []}, now=old)
    ctx = compose_board(conn, now=NOW)
    route = ctx["transit_routes"][0]
    assert route["live"] is None
    assert route["est_min"] == 21
    # legs fall back to scheduled ride times, unmarked as live
    assert [(lg["ride_min"], lg.get("ride_live")) for lg in route["legs"]] \
        == [(6, None), (10, None)]


def test_scheduler_polls_rt_for_chain_lines_only(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    add_route(conn)
    rt = [a for a in _active_adapters(conn)
          if a.manifest.name == "mta_rt"]
    assert rt and rt[0].lines == {"R", "F"}
    # no chains -> no RT polling (line monitors alone don't need trips)
    with conn:
        conn.execute("DELETE FROM commute_profile")
        conn.execute("INSERT INTO monitor (adapter, field) VALUES ('mta', 'R')")
    names = [a.manifest.name for a in _active_adapters(conn)]
    assert "mta" in names and "mta_rt" not in names


def test_next_departures_direction_filtered_max3():
    from app.adapters.mta_rt import next_departures
    all_trips = [*trips(),
                 {"route": "R", "stops": {"R36": T0 + 1500, "R31": T0 + 1860}},
                 {"route": "R", "stops": {"R36": T0 + 2100, "R31": T0 + 2460}},
                 # wrong direction: arrives at R36 AFTER R31
                 {"route": "R", "stops": {"R36": T0 + 400, "R31": T0 + 100}},
                 # departed already
                 {"route": "R", "stops": {"R36": T0 - 300, "R31": T0 + 60}}]
    deps = next_departures(all_trips, "R", "R36", "R31", T0)
    assert deps == [T0 + 300, T0 + 900, T0 + 1500]       # 3, sorted, filtered


def test_board_rows_show_next_trains(tmp_path, monkeypatch):
    use_r_dataset(tmp_path, monkeypatch)
    conn = setup_conn(tmp_path, monkeypatch)
    add_route(conn)
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field, config_json) VALUES"
                     " ('mta', 'R', ?)",
                     (json.dumps({"from_id": "R36", "to_id": "R31",
                                  "from_name": "25 St", "to_name": "Barclays"}),))
    snapshots.save(conn, 1, "mta", {"feeds": {}, "errors": []}, now=NOW)
    snapshots.save(conn, 1, "mta_rt", {"trips": trips(), "errors": []}, now=NOW)
    ctx = compose_board(conn, now=NOW)
    assert ctx["transit"][0]["next_trains"] == ["7:35", "7:45"]
    by_line = {leg["line"]: leg for leg in ctx["transit_routes"][0]["legs"]}
    assert by_line["R"]["next_trains"] == ["7:35", "7:45"]
    assert by_line["F"]["next_trains"] == ["7:44"]
    # stale RT: times vanish rather than lie
    later = datetime(2026, 7, 16, 7, 40)     # snapshot now 10 min old
    ctx = compose_board(conn, now=later)
    assert ctx["transit"][0]["next_trains"] == []


def test_scheduler_rt_includes_segment_monitors(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field, config_json) VALUES"
                     " ('mta', 'L', ?)",
                     (json.dumps({"from_id": "L10", "to_id": "L03"}),))
        conn.execute("INSERT INTO monitor (adapter, field) VALUES ('mta', 'G')")
    rt = [a for a in _active_adapters(conn) if a.manifest.name == "mta_rt"]
    assert rt and rt[0].lines == {"L"}       # segment yes, line-wide no


def test_rt_adapter_registry_and_json_normalize():
    a = MTARealtimeAdapter(lines={"R"})
    assert a.manifest.validate() == []
    out = a.normalize({"trips": [{"route": "R", "stops": {"R36": 1}}]})
    assert json.dumps(out)                           # snapshot-safe (JSON)
