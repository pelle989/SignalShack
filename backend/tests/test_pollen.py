"""Pollen card (V1.2 slice 1): adapter, admin toggles, board, poll gating."""

from datetime import date, datetime

from app.adapters.google_pollen import GooglePollenAdapter
from app.core import db, secrets, snapshots
from app.display.service import compose_board
from app.jobs.scheduler import _active_adapters
from tests.test_admin import client, do_setup, get_csrf
from tests.test_display_service import setup_conn, store_weather

SAMPLE = {"dailyInfo": [{
    "date": {"year": 2026, "month": 7, "day": 16},
    "pollenTypeInfo": [
        {"code": "GRASS", "displayName": "Grass",
         "indexInfo": {"value": 4, "category": "High"}},
        {"code": "TREE", "displayName": "Tree",
         "indexInfo": {"value": 1, "category": "Very Low"}},
        {"code": "WEED", "displayName": "Weed"},          # no index today
    ],
    "plantInfo": [
        {"code": "RAGWEED", "displayName": "Ragweed", "inSeason": True,
         "indexInfo": {"value": 5, "category": "Very High"}},
        {"code": "OAK", "displayName": "Oak", "inSeason": False},   # no index
    ]}]}


def _enable(conn, types=("GRASS", "TREE")):
    secrets.store(conn, "google_pollen", "k" * 30, label="test")
    with conn:
        for t in types:
            conn.execute("INSERT INTO monitor (adapter, field)"
                         " VALUES ('pollen', ?)", (t,))


def test_normalize_defaults_and_categories():
    out = GooglePollenAdapter().normalize(SAMPLE)["types"]
    assert out["GRASS"]["index"] == 4 and out["GRASS"]["category"] == "High"
    assert "meds" in out["GRASS"]["meaning"]
    assert out["TREE"]["index"] == 1
    assert out["WEED"]["index"] == 0          # missing indexInfo -> 0, no crash
    assert GooglePollenAdapter().normalize({})["types"]["TREE"]["index"] == 0


def test_normalize_reports_location_species():
    plants = GooglePollenAdapter().normalize(SAMPLE)["plants"]
    assert plants["RAGWEED"]["index"] == 5 and plants["RAGWEED"]["in_season"]
    assert "windows" in plants["RAGWEED"]["meaning"]
    assert plants["OAK"]["index"] == 0                # no index -> 0, no crash


def test_board_species_rows_and_missing_species(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 0)
    store_weather(conn, date(2026, 7, 16), fetched_at=now)
    _enable(conn, types=("GRASS", "RAGWEED", "BIRCH"))  # birch not in feed
    snapshots.save(conn, 1, "google_pollen", SAMPLE, now=now)
    ctx = compose_board(conn, now=now)
    rows = {r["type"]: r for r in ctx["pollen"]["rows"]}
    assert rows["RAGWEED"]["label"] == "Ragweed"
    assert rows["RAGWEED"]["index"] == 5
    assert rows["RAGWEED"]["attention"] == "amber"
    assert rows["BIRCH"]["category"] == "No reading"    # honest, not vanished


def test_board_pollen_card_rows_and_attention(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 0)
    store_weather(conn, date(2026, 7, 16), fetched_at=now)
    _enable(conn)
    snapshots.save(conn, 1, "google_pollen", SAMPLE, now=now)
    ctx = compose_board(conn, now=now)
    p = ctx["pollen"]
    assert [r["type"] for r in p["rows"]] == ["GRASS", "TREE"]   # watched only
    assert p["attention"] == "amber"           # grass at 4 = plan-your-meds
    assert "pollen" in ctx["layout"]


def test_board_pollen_absent_without_key_or_types(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 16, 7, 0)
    store_weather(conn, date(2026, 7, 16), fetched_at=now)
    assert compose_board(conn, now=now)["pollen"] is None
    secrets.store(conn, "google_pollen", "k" * 30, label="test")
    assert compose_board(conn, now=now)["pollen"] is None   # key but no types


def test_out_of_season_rests_but_stale_zero_shows(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 1, 10, 7, 0)
    store_weather(conn, date(2026, 1, 10), fetched_at=now)
    _enable(conn)
    zero = {"dailyInfo": [{"pollenTypeInfo": []}]}
    snapshots.save(conn, 1, "google_pollen", zero, now=now)
    ctx = compose_board(conn, now=now)
    assert ctx["pollen"] is None               # fresh all-zero: winter rest
    assert "pollen" not in ctx["layout"]
    # same zeros but stale: show honestly rather than vanish (invariant 2)
    later = datetime(2026, 1, 12, 7, 0)
    store_weather(conn, date(2026, 1, 12), fetched_at=later)
    ctx = compose_board(conn, now=later)
    assert ctx["pollen"] is not None and ctx["pollen"]["state"] == "stale"


def test_admin_key_and_type_toggles(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        c.post("/admin/settings/pollen",
               data={"api_key": " kkkkkkkkkkkkkkkkkkkkkkkkkkkkkk ", "csrf": csrf})
        c.post("/admin/settings/pollen/types",
               data={"ptype": ["TREE", "WEED"], "csrf": csrf})
        conn = db.connect()
        assert secrets.exists(conn, "google_pollen")
        rows = {r["field"] for r in conn.execute(
            "SELECT field FROM monitor WHERE adapter='pollen'").fetchall()}
        assert rows == {"TREE", "WEED"}
        conn.close()
        # resubmit with one unchecked: replaces, not appends
        c.post("/admin/settings/pollen/types",
               data={"ptype": "GRASS", "csrf": csrf})
        conn = db.connect()
        rows = {r["field"] for r in conn.execute(
            "SELECT field FROM monitor WHERE adapter='pollen'").fetchall()}
        assert rows == {"GRASS"}
        conn.close()


def test_admin_species_toggle_validated_against_snapshot(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        c.post("/admin/settings/pollen",
               data={"api_key": "k" * 30, "csrf": csrf})
        conn = db.connect()
        snapshots.save(conn, 1, "google_pollen", SAMPLE)
        conn.close()
        # RAGWEED is reported at this location; MADEUP is not
        c.post("/admin/settings/pollen/types",
               data={"ptype": ["TREE", "RAGWEED", "MADEUP"], "csrf": csrf})
        conn = db.connect()
        rows = {r["field"] for r in conn.execute(
            "SELECT field FROM monitor WHERE adapter='pollen'").fetchall()}
        conn.close()
        assert rows == {"TREE", "RAGWEED"}


def test_scheduler_polls_only_with_key_and_types(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    secrets.store(conn, "google_pollen", "k" * 30, label="test")
    names = [a.manifest.name for a in _active_adapters(conn)]
    assert "google_pollen" not in names        # key alone isn't a request
    with conn:
        conn.execute("INSERT INTO monitor (adapter, field)"
                     " VALUES ('pollen', 'TREE')")
    names = [a.manifest.name for a in _active_adapters(conn)]
    assert "google_pollen" in names
