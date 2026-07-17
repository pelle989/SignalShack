"""Forecast grid (next 12h) + 7-day outlook with confidence fade."""

from datetime import date, datetime, timedelta

from app.core import snapshots
from app.display.service import _forecast_grid, _outlook, compose_board
from tests.test_display_service import setup_conn
from tests.test_weather_adapters import synthetic_payload

DAY = date(2026, 7, 16)
NOW = datetime(2026, 7, 16, 7, 0)


def payload_with_outlook():
    """Synthetic hourly + a daily block extended to 8 forecast days."""
    hourly, _ = synthetic_payload(DAY)
    hourly["wind_direction_10m"] = [270] * len(hourly["time"])
    days = [(DAY + timedelta(days=i - 7)).isoformat() for i in range(15)]
    daily = {
        "time": days,
        "temperature_2m_max": [88] * 15,
        "temperature_2m_min": [70] * 15,
        "snowfall_sum": [0] * 15,
        "precipitation_sum": [0.0] * 15,
        "precipitation_probability_max": [10, 10, 10, 10, 10, 10, 10, 20,
                                          30, 40, 50, 60, 70, 80, 90],
        "weather_code": [2] * 8 + [61] * 7,
    }
    return {"hourly": hourly, "daily": daily}


def test_forecast_grid_next_12_hours():
    p = payload_with_outlook()
    grid = _forecast_grid(p["hourly"], DAY.isoformat(), 7)
    assert len(grid["hours"]) == 12
    assert grid["hours"][0]["label"] == "7A"
    assert grid["hours"][5]["label"] == "12P"
    assert grid["hours"][0]["dir"] == "W"                # 270°
    assert grid["hours"][0]["temp"] is not None


def test_forecast_grid_tolerates_old_snapshots():
    hourly, _ = synthetic_payload(DAY)                   # no wind_direction
    grid = _forecast_grid(hourly, DAY.isoformat(), 7)
    assert grid["hours"][0]["dir"] is None               # dash, not crash
    assert _forecast_grid(hourly, "1999-01-01", 7) is None


def test_outlook_seven_days_confidence_fade():
    out = _outlook(payload_with_outlook()["daily"], DAY.isoformat())
    days = out["days"]
    assert len(days) == 7
    assert days[0]["label"] == (DAY + timedelta(days=1)).strftime("%a")
    assert [d["conf"] for d in days] == ["high"] * 4 + ["medium", "low", "low"]
    assert days[0]["word"] == "Rain"                     # WMO 61
    assert days[0]["pop"] == 30                          # tomorrow = index 8


def test_outlook_none_on_short_daily():
    _, daily = synthetic_payload(DAY)                    # only tomorrow ahead
    assert _outlook(daily, DAY.isoformat()) is None


def test_board_integration_and_layout(tmp_path, monkeypatch):
    conn = setup_conn(tmp_path, monkeypatch)
    snapshots.save(conn, 1, "open_meteo", payload_with_outlook(), now=NOW)
    ctx = compose_board(conn, now=NOW)
    assert len(ctx["forecast"]["hours"]) == 12
    assert len(ctx["outlook"]["days"]) == 7
    assert "forecast" in ctx["layout"] and "outlook" in ctx["layout"]
    # cards render without error
    from app.main import templates
    tpl = templates.env.get_template("_board.html")
    html = tpl.render(**ctx, csrf="x")
    assert "Next 12 hours" in html and "Next 7 days" in html
    assert "12P" in html and "% rain" in html
