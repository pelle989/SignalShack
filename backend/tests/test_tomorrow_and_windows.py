"""Tomorrow strip + display-time window enforcement."""

from datetime import date, datetime

from app.adapters.weather_derive import derive_fields, derive_tomorrow
from app.display.service import compose_board
from app.rules.engine import evaluate
from tests.test_display_service import setup_conn, store_weather
from tests.test_weather_adapters import SEEDS, synthetic_payload


def seed(seed_id):
    return next(r for r in SEEDS if r["id"] == seed_id)


# ---------------- window enforcement

def fields_for(day, now_h=7):
    hourly, daily = synthetic_payload(day)
    # make the commute wet so T2's conditions pass
    idx = [i for i, t in enumerate(hourly["time"]) if t.startswith(day.isoformat())]
    for i in idx[7:9]:
        hourly["precipitation_probability"][i] = 80
    return derive_fields(hourly, daily, day.isoformat(), now_h=now_h,
                         season_state={})


def test_commute_rule_gated_to_mornings():
    day = date(2026, 7, 14)                              # a Tuesday
    fields = fields_for(day)
    assert evaluate(seed("T2"), fields, month=7, hour=7) is not None
    assert evaluate(seed("T2"), fields, month=7, hour=20) is None   # 8 PM board
    # Phase M digests pass hour=None: windows deliberately skipped
    assert evaluate(seed("T2"), fields, month=7, hour=None) is not None


def test_windowed_rules_have_window_hours():
    for rid in ("S1", "S2", "T2", "P9", "P10", "A1", "A2", "A3"):
        wh = seed(rid).get("window_hours")
        assert wh and len(wh) == 2, rid


# ---------------- tomorrow strip

def test_derive_tomorrow_phrases():
    day = date(2026, 7, 14)
    hourly, daily = synthetic_payload(day)
    daily["temperature_2m_max"][8] = 82                  # tomorrow cooler than 90
    out = derive_tomorrow(hourly, daily, day.isoformat())
    assert out["text"].startswith("Cooler.")
    assert "Dry." in out["text"]
    assert "high confidence" in out["confidence"]


def test_derive_tomorrow_none_without_data():
    day = date(2026, 7, 14)
    hourly, daily = synthetic_payload(day)
    daily["time"] = daily["time"][:8]                    # strip tomorrow
    for k in list(daily):
        if k != "time":
            daily[k] = daily[k][:8]
    assert derive_tomorrow(hourly, daily, day.isoformat()) is None


def test_board_includes_tomorrow_strip(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    now = datetime(2026, 7, 14, 7, 0)
    store_weather(conn, date(2026, 7, 14), fetched_at=now)
    ctx = compose_board(conn, now=now)
    assert ctx["tomorrow"] is not None
    assert ctx["layout"][-1] == "tomorrow"               # appended last by default
