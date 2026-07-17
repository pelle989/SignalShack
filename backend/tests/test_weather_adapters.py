"""End-to-end: synthetic payload → derive_fields → seeds.json → engine → board.

Guards the field contract: every field a seed rule references must exist in
derive_fields() output (or be another adapter's published field).
"""

import json
from datetime import date, timedelta
from pathlib import Path

from app.adapters.nws import NWSAdapter
from app.adapters.open_meteo import OpenMeteoAdapter
from app.adapters.weather_derive import derive_fields
from app.rules.engine import compose, evaluate

SEEDS = json.loads(
    (Path(__file__).parents[1] / "app" / "rules" / "seeds.json").read_text())["rules"]


def synthetic_payload(day: date, storm_day=True):
    """9 days (7 past + today + tomorrow) of a hot July stretch, storms at 3 PM."""
    days = [(day - timedelta(days=7 - i)).isoformat() for i in range(9)]
    hours = [f"{d}T{h:02d}:00" for d in days for h in range(24)]
    today = day.isoformat()

    def temp(d, h):
        return 74 + (16 if 12 <= h <= 17 else 8 if 9 <= h < 12 or 17 < h <= 19 else 0)

    hourly = {
        "time": hours,
        "temperature_2m": [temp(d, h) for d in days for h in range(24)],
        "apparent_temperature": [temp(d, h) + 8 for d in days for h in range(24)],
        "precipitation_probability": [70 if 15 <= h <= 19 and d == today else 10
                                      for d in days for h in range(24)],
        "precipitation": [0.0] * len(hours),
        "snowfall": [0.0] * len(hours),
        "weather_code": [95 if h == 15 and d == today and storm_day else 2
                         for d in days for h in range(24)],
        "cloud_cover": [35] * len(hours),
        "dew_point_2m": [73] * len(hours),
        "wind_speed_10m": [12] * len(hours),
        "wind_gusts_10m": [44 if 13 <= h <= 17 and d == today else 18
                           for d in days for h in range(24)],
        "uv_index": [9 if 10 <= h <= 14 else 3 for d in days for h in range(24)],
    }
    daily = {
        "time": days,
        "temperature_2m_max": [88, 89, 87, 90, 86, 88, 91, 90, 84],
        "temperature_2m_min": [70] * 9,
        "snowfall_sum": [0] * 9,
        "precipitation_sum": [0.0] * 9,
    }
    return hourly, daily


def test_field_contract_covers_all_seed_references():
    day = date(2026, 7, 14)
    hourly, daily = synthetic_payload(day)
    fields = derive_fields(hourly, daily, day.isoformat(), now_h=6, season_state={})
    published = set(fields) | set(NWSAdapter.manifest.fields)
    missing = {c["field"] for r in SEEDS for c in r["conditions"]} - published
    assert not missing, f"seed rules reference unpublished fields: {missing}"


def test_end_to_end_storm_day_composition():
    day = date(2026, 7, 14)  # a Tuesday
    hourly, daily = synthetic_payload(day)
    fields = derive_fields(hourly, daily, day.isoformat(), now_h=6, season_state={})
    fired = [f for r in SEEDS
             if (f := evaluate(r, fields, month=day.month)) is not None]
    ids = {f.rule_id for f in fired}
    assert "T7" in ids       # storms at 3 PM
    assert "T6" in ids       # 44 mph gusts (tuned 40 threshold)
    assert "P6" not in ids or fields["dew_max_yesterday"] < 72  # novelty gate
    primary, secondary = compose(fired)
    assert primary and primary[0].rule_id == "T7"
    assert "3 PM" in primary[0].message
    assert len(secondary) <= 3


def test_topic_split_p6_survives_s2(monkeypatch):
    """v4 scorelog fix: humidity is its own topic — S2 heat-safety firing must
    no longer dedupe P6 off the board (they shared 'temp' in v3)."""
    day = date(2026, 7, 14)
    hourly, daily = synthetic_payload(day, storm_day=False)
    hourly["apparent_temperature"] = [t + 14 for t in hourly["temperature_2m"]]
    fields = derive_fields(hourly, daily, day.isoformat(), now_h=6,
                           season_state={})
    fields["heat_index_max"] = 101          # force S2
    fields["dew_max_today"], fields["dew_max_yesterday"] = 74, 70   # force P6
    fired = [f for r in SEEDS
             if (f := evaluate(r, fields, month=7)) is not None]
    ids = {f.rule_id for f in fired}
    assert {"S2", "P6"} <= ids
    primary, secondary = compose(fired)
    kept = {f.rule_id for f in primary + secondary}
    assert "S2" in kept and "P6" in kept    # both surfaces, different topics


def test_p1_cool_start_gate():
    """v4 scorelog fix: 'dress in layers' needs an actually-cool morning."""
    p1 = next(r for r in SEEDS if r["id"] == "P1")
    fields = {"temp_swing_today": 30, "temp_min_today": 75, "high_today": 102}
    assert evaluate(p1, fields, month=7) is None        # hot morning: absurd
    fields["temp_min_today"] = 48
    assert evaluate(p1, fields, month=10) is not None   # cool morning: fires


def test_warning_suppression_end_to_end():
    day = date(2026, 7, 14)
    hourly, daily = synthetic_payload(day)
    fields = derive_fields(hourly, daily, day.isoformat(), now_h=6, season_state={})
    fired = [f for r in SEEDS
             if (f := evaluate(r, fields, month=day.month)) is not None]
    primary, secondary = compose(fired, warning_active=True)
    assert all(f.priority >= 60 for f in primary + secondary)


def test_one_shot_state_fires_once():
    day = date(2026, 1, 10)
    hourly, daily = synthetic_payload(day, storm_day=False)
    daily["temperature_2m_min"] = [40, 40, 40, 40, 40, 40, 40, 30, 30]  # freeze tonight
    hourly["temperature_2m"] = [38] * len(hourly["time"])
    state = {}
    f1 = derive_fields(hourly, daily, day.isoformat(), now_h=6, season_state=state)
    f2 = derive_fields(hourly, daily, day.isoformat(), now_h=6, season_state=state)
    assert f1["first_freeze_of_season"] is True
    assert f2["first_freeze_of_season"] is False  # state consumed
    assert f1["freeze_snap_onset"] is True        # prior 3 nights were 40°


def test_nws_normalize_severity_and_flags():
    raw = {"features": [
        {"properties": {"event": "Coastal Flood Advisory", "severity": "Minor",
                        "headline": "Advisory until 4 PM", "ends": "2026-07-14T16:00"}},
        {"properties": {"event": "Tornado Warning", "severity": "Extreme",
                        "headline": "Take shelter now", "ends": "2026-07-14T15:30"}},
    ]}
    out = NWSAdapter().normalize(raw)
    assert out["warning_active"] is True
    assert out["advisory_active"] is True
    assert out["top_alert_event"] == "Tornado Warning"   # severity-sorted
    assert out["alert_count"] == 2


def test_adapters_pass_registry_gate():
    for cls in (OpenMeteoAdapter, NWSAdapter):
        assert cls.manifest.validate() == [], cls.__name__
