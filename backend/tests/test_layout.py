"""Layout editor: ordering, toggles, availability filtering, primary slot."""

from datetime import date, datetime

from app.core import layout, secrets, snapshots
from app.display.service import compose_board
from tests.test_admin import client, do_setup, get_csrf
from tests.test_display_service import setup_conn, store_weather
from tests.test_mta import FIXTURE as MTA_FIXTURE


def test_default_layout_and_availability_filter(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 15, 7, 0)
    store_weather(conn, date(2026, 7, 15), fetched_at=now)
    ctx = compose_board(conn, now=now)
    # no monitors, no AirNow key: transit and air filtered out of the order
    assert ctx["layout"] == ["weather", "alerts", "announcements", "tomorrow",
                             "forecast"]


def test_configured_cards_join_the_order(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 15, 7, 0)
    store_weather(conn, date(2026, 7, 15), fetched_at=now)
    with conn:
        conn.execute("INSERT INTO board (name, is_default, layout_json)"
                     " VALUES ('Default', 1, '{}')")
        conn.execute("INSERT INTO monitor (adapter, field, created_at)"
                     " VALUES ('mta', 'F', '2026-07-15T06:00:00')")
    snapshots.save(conn, 1, "mta", MTA_FIXTURE, now=now)
    secrets.store(conn, "airnow", "k")
    ctx = compose_board(conn, now=now)
    assert ctx["layout"] == ["weather", "alerts", "transit", "air",
                             "announcements", "tomorrow", "forecast"]


def test_move_and_toggle_change_composition(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 15, 7, 0)
    store_weather(conn, date(2026, 7, 15), fetched_at=now)
    with conn:
        conn.execute("INSERT INTO board (name, is_default, layout_json)"
                     " VALUES ('Default', 1, '{}')")
    layout.move(conn, "alerts", "up")            # alerts to the top -> primary
    layout.toggle(conn, "announcements")         # off
    ctx = compose_board(conn, now=now)
    assert ctx["layout"][0] == "alerts"
    assert "announcements" not in ctx["layout"]
    layout.toggle(conn, "announcements")         # back on, position preserved
    ctx = compose_board(conn, now=now)
    assert "announcements" in ctx["layout"]
    assert ctx["layout"].index("announcements") > ctx["layout"].index("weather")


def test_layout_survives_legacy_preset_json(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    with conn:
        conn.execute("INSERT INTO board (name, is_default, layout_json)"
                     " VALUES ('Default', 1, '{\"preset\": \"default\"}')")
    cards = layout.get_layout(conn)              # legacy board upgraded in place
    assert [c["type"] for c in cards] == layout.CARD_TYPES


def test_layout_routes_end_to_end(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        page = c.get("/admin/layout")
        assert "Weather Meaning" in page.text and "primary" in page.text
        c.post("/admin/layout/alerts/up", data={"csrf": csrf})
        page = c.get("/admin/layout")
        assert page.text.index("NWS Alerts") < page.text.index("Weather Meaning")
        # board fragment renders alerts first (primary)
        frag = c.get("/fragments/board")
        assert frag.text.index("Alerts") < frag.text.index("Weather ·")
