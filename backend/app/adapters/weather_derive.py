"""Weather derived-field computation — the intelligence behind the rule engine.

AIDEV-CAUTION: this module IS the field contract that seeds.json references.
Renaming an output key silently kills every rule that uses it — the
end-to-end test in test_weather_adapters.py guards the full contract.

Ported from the Phase M validation kit (backtest-tuned 2026-07-13); rules that
survive Gate M graduate here with their derivations intact.
"""

import math
from datetime import date, timedelta


def _f(x):
    return None if x is None else float(x)


def heat_index(t, dew):
    if t is None or dew is None or t < 80:
        return t
    rh = 100 * (math.exp((17.625 * (dew - 32) * 5 / 9) / (243.04 + (dew - 32) * 5 / 9))
                / math.exp((17.625 * (t - 32) * 5 / 9) / (243.04 + (t - 32) * 5 / 9)))
    return round(-42.379 + 2.04901523 * t + 10.14333127 * rh - .22475541 * t * rh
                 - .00683783 * t * t - .05481717 * rh * rh + .00122874 * t * t * rh
                 + .00085282 * t * rh * rh - .00000199 * t * t * rh * rh)


def wind_chill(t, v):
    if t is None or v is None or t > 50 or v <= 3:
        return t
    return round(35.74 + 0.6215 * t - 35.75 * v ** 0.16 + 0.4275 * t * v ** 0.16)


def hour_label(h):
    h = h % 24
    return {0: "midnight", 12: "noon"}.get(h, f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}")


def season_year(t):
    return t.year if t.month >= 7 else t.year - 1


_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(deg):
    return None if deg is None else _COMPASS[round(deg / 45) % 8]


def derive_fields(hourly: dict, daily: dict, day_iso: str, now_h: int,
                  season_state: dict) -> dict:
    """hourly/daily: Open-Meteo-shaped arrays (must include >=7 past days and
    tomorrow). season_state: mutable dict persisting one-shot memory
    (first_freeze / first_warm season-year strings). Returns the flat field
    dict consumed by rules/seeds.json."""
    times = hourly["time"]
    idx = [i for i, t in enumerate(times) if t.startswith(day_iso)]
    if not idx:
        raise ValueError(f"no hourly data for {day_iso}")

    def series(key):
        return [_f(hourly[key][i]) for i in idx]

    temp, pop = series("temperature_2m"), series("precipitation_probability")
    precip = series("precipitation")
    code = [int(hourly["weather_code"][i]) for i in idx]
    cloud, dew = series("cloud_cover"), series("dew_point_2m")
    wind, gust = series("wind_speed_10m"), series("wind_gusts_10m")
    uv = series("uv_index") if "uv_index" in hourly else [None] * 24
    wdir = (series("wind_direction_10m") if "wind_direction_10m" in hourly
            else [None] * 24)      # older snapshots predate this variable

    today_d = date.fromisoformat(day_iso)
    D = daily
    dt = D["time"].index(day_iso)
    high = _f(D["temperature_2m_max"][dt])
    low_tonight = _f(D["temperature_2m_min"][dt + 1]) if dt + 1 < len(D["time"]) \
        else _f(D["temperature_2m_min"][dt])
    high_yday = _f(D["temperature_2m_max"][dt - 1]) if dt > 0 else None
    prior_lows = [_f(x) for x in D["temperature_2m_min"][max(0, dt - 3):dt]]

    # streaks
    hot_streak = 0
    for h in reversed([_f(x) for x in D["temperature_2m_max"][:dt]]):
        if h is not None and h >= 85:
            hot_streak += 1
        else:
            break
    rain_streak = 0
    for p in reversed([_f(x) for x in D["precipitation_sum"][:dt]]):
        if p is not None and p > 0.05:
            rain_streak += 1
        else:
            break

    # yesterday's dew (P6 novelty gate, P9 change rule)
    yday = (today_d - timedelta(days=1)).isoformat()
    idx_y = [i for i, t in enumerate(times) if t.startswith(yday)]
    dew_max_yday = max((_f(hourly["dew_point_2m"][i]) or -99 for i in idx_y), default=None) \
        if idx_y else None

    fut = range(now_h, 24)
    rain_start = next((i for i in fut if (pop[i] or 0) >= 60), None)
    storm_start = next((i for i in fut if code[i] >= 95), None)
    raining_now = (precip[now_h] or 0) > 0.02
    rain_end = next((i for i in range(now_h + 1, 24) if (pop[i] or 0) < 30), None)

    ev = range(17, 21)
    ov = list(range(21, 24)) + [0]
    ev_temp = [temp[i] for i in ev if temp[i] is not None]
    ev_dew_max = max(((dew[i] or 0) for i in ev), default=None)

    # one-shots (season-scoped)
    sy = str(season_year(today_d))
    first_freeze = low_tonight is not None and low_tonight <= 32 \
        and season_state.get("first_freeze") != sy
    if first_freeze:
        season_state["first_freeze"] = sy
    first_warm = high is not None and high >= 70 \
        and season_state.get("first_warm") != sy and today_d.month in (1, 2, 3, 4, 5)
    if first_warm:
        season_state["first_warm"] = sy

    fields = {
        # --- safety inputs
        "wind_chill_min_morning": min((wind_chill(temp[i], wind[i]) or 99
                                       for i in range(5, 10)), default=None),
        "heat_index_max": max((heat_index(temp[i], dew[i]) or 0
                               for i in range(10, 20)), default=None),
        "freezing_precip_expected": any(c in (56, 57, 66, 67) for c in code),
        "temp_min_today": min((t for t in temp if t is not None), default=None),
        "snow_24h_in": _f(D["snowfall_sum"][dt]),
        # --- precip timing
        "rain_within_4h": rain_start is not None and (rain_start - now_h) <= 4,
        "rain_start_label": hour_label(rain_start) if rain_start is not None else None,
        "raining_now": raining_now,
        "rain_ends_within_6h": rain_end is not None and (rain_end - now_h) <= 6,
        "rain_end_label": hour_label(rain_end) if rain_end is not None else None,
        "storms_expected": storm_start is not None,
        "storm_start_label": hour_label(storm_start) if storm_start is not None
                             else "this afternoon",
        "pop_commute_max": max(((pop[i] or 0) for i in (7, 8)), default=None),
        "pop_today_max": max(((p or 0) for p in pop[6:22]), default=None),
        "all_day_soaker": max((sum(1 for j in range(i, min(i + 6, 22))
                                   if (pop[j] or 0) >= 60)
                               for i in range(6, 17)), default=0) >= 6,
        "is_weekday": today_d.weekday() < 5,
        # --- temperature
        "high_today": high,
        "low_tonight": low_tonight,
        "temp_swing_today": (high - min(t for t in temp if t is not None))
                            if high is not None else None,
        "high_delta_vs_yesterday": (high - high_yday)
                                   if high is not None and high_yday is not None else None,
        "temp_7am": temp[7],
        "temp_evening": ev_temp[0] if ev_temp else None,
        "temp_evening_min": min(ev_temp) if ev_temp else None,
        "temp_evening_max": max(ev_temp) if ev_temp else None,
        # --- humidity
        "dew_max_today": max((x for x in dew if x is not None), default=None),
        "dew_max_yesterday": dew_max_yday,
        "dew_evening_max": ev_dew_max,
        "dew_drop_vs_yesterday": (dew_max_yday - ev_dew_max)
                                 if dew_max_yday is not None and ev_dew_max is not None else None,
        # --- wind / sky / sun
        "max_gust_today": max((g for g in gust if g is not None), default=None),
        "wind_dir_now": compass(wdir[now_h]) if now_h < len(wdir) else None,
        "wind_overnight_max": max(((wind[i] or 0) for i in ov), default=None),
        "wind_evening_max": max(((wind[i] or 0) for i in ev), default=None),
        "cloud_overnight_max": max(((cloud[i] or 0) for i in ov), default=None),
        "cloud_midday_min": min(((cloud[i] or 100) for i in range(11, 15)), default=None),
        "uv_index_max": max((u for i, u in enumerate(uv)
                             if u is not None and 9 <= i <= 15), default=None),
        "pop_evening_max": max(((pop[i] or 0) for i in ev), default=None),
        # --- streaks & one-shots
        "freeze_snap_onset": len(prior_lows) >= 3
                             and all(low > 32 for low in prior_lows if low is not None),
        "hot_streak_days": hot_streak,
        "consecutive_precip_days": rain_streak,
        "first_freeze_of_season": first_freeze,
        "first_warm_day_of_season": first_warm,
    }
    return fields


def derive_tomorrow(hourly: dict, daily: dict, day_iso: str) -> dict | None:
    """Three-phrase forward look for the Tomorrow strip (plan V1.1).
    Informational only — rules never fire on tomorrow's data."""
    D = daily
    dt = D["time"].index(day_iso)
    if dt + 1 >= len(D["time"]):
        return None
    high_today = _f(D["temperature_2m_max"][dt])
    high_tmrw = _f(D["temperature_2m_max"][dt + 1])
    if high_today is None or high_tmrw is None:
        return None
    delta = high_tmrw - high_today

    tomorrow_iso = D["time"][dt + 1]
    idx_t = [i for i, t in enumerate(hourly["time"]) if t.startswith(tomorrow_iso)]
    pop_max = max(((_f(hourly["precipitation_probability"][i]) or 0)
                   for i in idx_t), default=0)
    idx_now = [i for i, t in enumerate(hourly["time"]) if t.startswith(day_iso)]
    dew_today = max(((_f(hourly["dew_point_2m"][i]) or 0) for i in idx_now), default=0)
    dew_tmrw = max(((_f(hourly["dew_point_2m"][i]) or 0) for i in idx_t), default=0)

    phrases = []
    phrases.append("Warmer." if delta >= 5 else "Cooler." if delta <= -5
                   else "Similar temps.")
    phrases.append("Rain likely." if pop_max >= 60 else "Rain possible."
                   if pop_max >= 40 else "Dry.")
    dew_delta = dew_tmrw - dew_today
    if dew_delta <= -4:
        phrases.append("Less humid.")
    elif dew_delta >= 4:
        phrases.append("Muggier.")
    return {"text": " ".join(phrases), "confidence": "1-day outlook · high confidence"}
