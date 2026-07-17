"""Board density presets: comfortable / compact / focus."""

from datetime import date, datetime

from app.core import db, layout, snapshots
from app.display.service import compose_board
from tests.test_admin import client, do_setup, get_csrf
from tests.test_display_service import setup_conn, store_weather


def _board_row(conn):
    with conn:
        conn.execute("INSERT INTO board (name, is_default, layout_json)"
                     " VALUES ('Default', 1, '{}')")


def test_density_default_set_and_invalid(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    _board_row(conn)
    assert layout.get_density(conn) == "comfortable"
    layout.set_density(conn, "focus")
    assert layout.get_density(conn) == "focus"
    layout.set_density(conn, "gigantic")            # unknown: ignored
    assert layout.get_density(conn) == "focus"


def test_reorder_preserves_density(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    _board_row(conn)
    layout.set_density(conn, "compact")
    layout.move(conn, "alerts", "up")               # _save must keep density
    assert layout.get_density(conn) == "compact"


def test_focus_caps_visible_cards(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    _board_row(conn)
    now = datetime(2026, 7, 16, 7, 0)
    store_weather(conn, date(2026, 7, 16), fetched_at=now)
    snapshots.save(conn, 1, "nws", {"features": []}, now=now)
    full = compose_board(conn, now=now)["layout"]
    assert len(full) > 3
    layout.set_density(conn, "focus")
    ctx = compose_board(conn, now=now)
    assert len(ctx["layout"]) == 3
    assert ctx["layout"] == full[:3]                # order respected
    assert ctx["density"] == "focus"


def test_admin_density_roundtrip(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        page = c.get("/admin/layout")
        assert "Board density" in page.text
        c.post("/admin/layout/density", data={"density": "compact",
                                              "csrf": csrf})
        conn = db.connect()
        assert layout.get_density(conn) == "compact"
        conn.close()
        # density class rides the board fragment
        frag = c.get("/fragments/board")
        assert 'className = "compact"' in frag.text
