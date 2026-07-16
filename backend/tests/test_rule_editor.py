"""No-code rule editor: validation, save, preview, board evaluation."""

from datetime import date, datetime

from app.core import db
from app.display.service import compose_board
from app.rules import user as user_rules
from tests.test_admin import client, do_setup, get_csrf
from tests.test_display_service import setup_conn, store_weather

GOOD_FORM = {
    "name": "Pool evening", "output": "Warm evening ahead — pool after dinner.",
    "band": "plan", "season": "warm", "notes": "",
    "window_start": "", "window_end": "",
    "cfield": ["temp_evening"], "cop": [">="], "cvalue": ["78"],
}


# ---------------- validation

def test_validation_enforces_voice_and_fields():
    bad = dict(GOOD_FORM, output="Pool time!!!")
    _, problems = user_rules.validate_and_build(bad)
    assert any("exclamation" in p for p in problems)

    bad = dict(GOOD_FORM, output="x" * 95)
    _, problems = user_rules.validate_and_build(bad)
    assert any("90" in p for p in problems)

    bad = dict(GOOD_FORM, cfield=["not_a_field"])
    _, problems = user_rules.validate_and_build(bad)
    assert any("Unknown field" in p for p in problems)

    bad = dict(GOOD_FORM, cfield=[""])
    _, problems = user_rules.validate_and_build(bad)
    assert any("At least one condition" in p for p in problems)

    rule, problems = user_rules.validate_and_build(GOOD_FORM)
    assert problems == []
    assert rule["priority"] == 45                        # plan band
    assert rule["priority"] < 90                         # never safety band
    assert rule["months"] == [5, 6, 7, 8, 9]
    assert rule["conditions"] == [{"field": "temp_evening", "op": ">=", "value": 78}]


def test_value_parsing_types():
    form = dict(GOOD_FORM, cfield=["raining_now", "low_tonight"],
                cop=["==", "between"], cvalue=["true", "[55, 70]"])
    rule, problems = user_rules.validate_and_build(form)
    assert problems == []
    assert rule["conditions"][0]["value"] is True
    assert rule["conditions"][1]["value"] == [55, 70]


# ---------------- save + board evaluation

def test_saved_rule_fires_on_board(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 18, 0)                   # evening: temp_evening=90
    store_weather(conn, date(2026, 7, 14), fetched_at=now)
    rule, _ = user_rules.validate_and_build(GOOD_FORM)
    user_rules.save(conn, rule)
    ctx = compose_board(conn, now=now)
    everything = " ".join([ctx["primary_msg"] or ""] + ctx["secondary_msgs"])
    assert "pool after dinner" in everything
    # disabled -> silent
    rid = conn.execute("SELECT id FROM signal_rule WHERE is_seed=0").fetchone()["id"]
    with conn:
        conn.execute("UPDATE rule_user_state SET enabled=0 WHERE rule_id=?", (rid,))
    ctx = compose_board(conn, now=now)
    everything = " ".join([ctx["primary_msg"] or ""] + ctx["secondary_msgs"])
    assert "pool after dinner" not in everything


def test_user_rule_window_respected(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    store_weather(conn, date(2026, 7, 14),
                  fetched_at=datetime(2026, 7, 14, 8, 0))
    form = dict(GOOD_FORM, window_start="16", window_end="21",
                cfield=["high_today"], cop=[">="], cvalue=["80"])
    rule, _ = user_rules.validate_and_build(form)
    user_rules.save(conn, rule)
    morning = compose_board(conn, now=datetime(2026, 7, 14, 8, 0))
    assert "pool" not in " ".join([morning["primary_msg"] or ""]
                                  + morning["secondary_msgs"])


# ---------------- routes end to end

def test_editor_routes_full_cycle(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        assert "pick a field" in c.get("/admin/rules/new").text

        # save via route
        r = c.post("/admin/rules/save", data={**{k: v for k, v in GOOD_FORM.items()
                                                 if not isinstance(v, list)},
                                              "cfield": "temp_evening",
                                              "cop": ">=", "cvalue": "78",
                                              "csrf": csrf}, follow_redirects=False)
        assert r.headers["location"] == "/admin/rules"
        page = c.get("/admin/rules")
        assert "Pool evening" in page.text

        # bad save re-renders with problems, nothing stored
        r = c.post("/admin/rules/save", data={"name": "Bad", "output": "No!!!",
                                              "band": "plan", "season": "all",
                                              "cfield": "temp_evening",
                                              "cop": ">=", "cvalue": "78",
                                              "csrf": csrf})
        assert "exclamation" in r.text
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM signal_rule"
                            " WHERE is_seed=0").fetchone()["c"] == 1

        # delete removes it
        rid = conn.execute("SELECT id FROM signal_rule WHERE is_seed=0").fetchone()["id"]
        conn.close()
        c.post(f"/admin/rules/user/{rid}/delete", data={"csrf": csrf})
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM signal_rule"
                            " WHERE is_seed=0").fetchone()["c"] == 0
        conn.close()


def test_preview_reports_fire_state(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        # no weather data yet
        r = c.post("/admin/rules/preview", data={**{k: v for k, v in GOOD_FORM.items()
                                                    if not isinstance(v, list)},
                                                 "cfield": "temp_evening",
                                                 "cop": ">=", "cvalue": "78",
                                                 "csrf": csrf})
        assert "needs one fetch" in r.text
        # with data: fires (synthetic evening temp 90 >= 78)
        conn = db.connect()
        store_weather(conn, date.today(), fetched_at=datetime.now())
        conn.close()
        r = c.post("/admin/rules/preview", data={**{k: v for k, v in GOOD_FORM.items()
                                                    if not isinstance(v, list)},
                                                 "cfield": "temp_evening",
                                                 "cop": ">=", "cvalue": "78",
                                                 "csrf": csrf})
        assert "Would fire right now" in r.text
        assert "temp_evening" in r.text
