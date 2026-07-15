"""Smoke tests: migrations apply, app boots, invariants have teeth."""

from fastapi.testclient import TestClient

from app.core import db
from app.main import app


def test_migrations_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    assert db.migrate() >= 1
    conn = db.connect()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for required in ("device", "location", "board", "signal_rule", "rule_user_state",
                     "weather_snapshot", "source_registry", "usage_stat", "monitor"):
        assert required in tables, f"missing table: {required}"


def test_display_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("SIGNALSHACK_NO_JOBS", "1")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        page = client.get("/display")
        assert page.status_code == 200
        frag = client.get("/fragments/board")
        # no location yet -> honest setup message, and alerts show unknown
        assert "Setup not complete" in frag.text
        assert "Alert status unknown" in frag.text


def test_fragment_with_jobs_enabled_schedules_wake_refresh(tmp_path, monkeypatch):
    """Regression: sync-route/no-event-loop crash (2026-07-13). The wake-refresh
    path must work when jobs are enabled and a location exists."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.delenv("SIGNALSHACK_NO_JOBS", raising=False)
    monkeypatch.setenv("SIGNALSHACK_LAT", "40.68")
    monkeypatch.setenv("SIGNALSHACK_LON", "-73.47")

    from app.jobs import scheduler as sched
    calls = []

    async def fake_refresh(loc_id):
        calls.append(loc_id)

    monkeypatch.setattr(sched, "refresh_if_stale", fake_refresh)
    # also stop the poll loop from doing network work in tests
    monkeypatch.setenv("SIGNALSHACK_NO_JOBS", "")  # jobs "enabled" for the route

    async def fake_loop(stop):
        await stop.wait()

    monkeypatch.setattr(sched, "poll_loop", fake_loop)

    with TestClient(app) as client:
        r = client.get("/fragments/board")
        assert r.status_code == 200
    assert calls == [1]


def test_announcement_text_is_escaped(tmp_path, monkeypatch):
    """Invariant 4: rendered user input is always escaped (XSS)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")

    from app.main import templates
    # render fragment with hostile announcement through the real template
    tpl = templates.get_template("_board.html")
    html = tpl.render(primary_msg="x", secondary_msgs=[], alerts=[],
                      alerts_state="fresh", alerts_attention=None,
                      announcements=[{"text": "<script>alert(1)</script>", "priority": 1}],
                      stamp_weather="test", stamp_alerts="test",
                      location_label="Home", layout=["weather", "announcements"],
                      pip={"state": "green", "label": "test"})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
