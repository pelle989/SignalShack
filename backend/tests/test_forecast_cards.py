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
    # Chartist-faithful geometry (verified against trailsnh's #wxsvg)
    c = grid["chart"]
    assert len(c["grid_y"]) == 5                         # horizontal gridlines
    assert c["cloud_path"].startswith("M") and "C" in c["cloud_path"]  # smooth
    assert c["cloud_path"].rstrip().endswith("Z")        # area closed to base
    assert c["pop_path"].rstrip().endswith("Z")
    assert "Z" not in c["temp_path"]                     # temp is a LINE
    assert len(c["dots"]) == 12
    assert len(c["hours"]) == 6                          # hour labels every 2h
    # temp labels the trailsnh way: endpoints + extremes, plateaus collapsed
    assert 2 <= len(c["temp_labels"]) <= 6
    assert all(lab["text"].endswith("°") for lab in c["temp_labels"])
    assert c["wind_path"] and c["gust_path"]
    assert c["dirs"][0]["text"] == "W →"                 # arrow = blowing toward
    assert all(w["text"] for w in c["winds"])
    # 7A start crosses no midnight: no day separator; 8P start would
    assert c["day_seps"] == []
    night = _forecast_grid(payload_with_outlook()["hourly"],
                           DAY.isoformat(), 20)
    assert night["chart"]["day_seps"][0]["text"] == (
        DAY + timedelta(days=1)).strftime("%A")


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
    assert 'class="series-temp"' in html and 'class="series-cloud"' in html
    assert 'class="ct-grids"' in html                    # Chartist framework
    assert "ol-ic" in html                               # outlook icons


def test_rain_when_am_pm_split():
    from app.display.service import _rain_when
    hours = [f"2026-07-18T{h:02d}:00" for h in range(24)]
    am_rain = {"time": hours,
               "precipitation_probability": [60 if 7 <= h < 11 else 10
                                             for h in range(24)]}
    pm_rain = {"time": hours,
               "precipitation_probability": [55 if 14 <= h <= 19 else 10
                                             for h in range(24)]}
    all_day = {"time": hours, "precipitation_probability": [70] * 24}
    dry = {"time": hours, "precipitation_probability": [15] * 24}
    assert _rain_when(am_rain, "2026-07-18") == "AM"
    assert _rain_when(pm_rain, "2026-07-18") == "PM"
    assert _rain_when(all_day, "2026-07-18") == "all day"
    assert _rain_when(dry, "2026-07-18") is None
    assert _rain_when({}, "2026-07-18") is None          # no hourly: no claim


def test_outlook_icons_mapped():
    from app.display.service import _wmo_icon
    assert _wmo_icon(0) == "sun" and _wmo_icon(2) == "part"
    assert _wmo_icon(3) == "cloud" and _wmo_icon(45) == "fog"
    assert _wmo_icon(61) == "rain" and _wmo_icon(75) == "snow"
    assert _wmo_icon(95) == "storm" and _wmo_icon(None) is None


def test_forecast_style_toggle(tmp_path, monkeypatch):
    from app.core import layout
    conn = setup_conn(tmp_path, monkeypatch)
    with conn:
        conn.execute("INSERT INTO board (name, is_default, layout_json)"
                     " VALUES ('Default', 1, '{}')")
    assert layout.get_forecast_style(conn) == "chart"    # default
    layout.set_forecast_style(conn, "table")
    assert layout.get_forecast_style(conn) == "table"
    layout.set_forecast_style(conn, "hologram")          # unknown: ignored
    assert layout.get_forecast_style(conn) == "table"
    layout.move(conn, "alerts", "up")                    # reorder preserves it
    layout.set_density(conn, "compact")                  # density too
    assert layout.get_forecast_style(conn) == "table"
    # table markup renders when selected
    snapshots.save(conn, 1, "open_meteo", payload_with_outlook(), now=NOW)
    ctx = compose_board(conn, now=NOW)
    assert ctx["forecast_style"] == "table"
    from app.main import templates
    html = templates.env.get_template("_board.html").render(**ctx, csrf="x")
    assert "Rain %" in html and "series-temp" not in html
