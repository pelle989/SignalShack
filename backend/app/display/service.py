"""Board composition — snapshots → derived fields → rules → card context.

AIDEV-NOTE: this is where invariant 2 gets rendered: every card resolves to a
labeled state. Alerts especially — SAFETY: a missing/stale NWS snapshot renders
as 'alert status unknown', never as 'no active alerts'.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from app.adapters.airnow import AirNowAdapter
from app.adapters.mta import STATUS_RANK, MTAAdapter, line_view
from app.adapters.nws import NWSAdapter
from app.adapters.open_meteo import OpenMeteoAdapter
from app.adapters.weather_derive import derive_fields, derive_tomorrow
from app.core import layout, secrets, snapshots
from app.rules import user as user_rules
from app.rules.engine import compose, evaluate

SEEDS = json.loads(
    (Path(__file__).parents[1] / "rules" / "seeds.json").read_text())["rules"]
OM, NWS = OpenMeteoAdapter.manifest, NWSAdapter.manifest
TRANSFER_BUFFER_MIN = 5     # scheduled chain estimate: buffer per transfer


def _rule_enabled(conn, seed_id: str) -> bool:
    row = conn.execute(
        "SELECT us.enabled FROM signal_rule r JOIN rule_user_state us ON us.rule_id=r.id"
        " WHERE r.seed_id=?", (seed_id,)).fetchone()
    return True if row is None else bool(row["enabled"])   # default: enabled


def _fire_caps(conn, fired_ids: list[str], today: str) -> dict:
    """7-day fire counts for capped rules; records today's fires."""
    hist = snapshots.kv_get(conn, "fires7d", {})
    week_ago = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    counts = {}
    for rid, dates in hist.items():
        counts[rid] = len([d for d in dates if d >= week_ago])
    for rid in fired_ids:
        dates = [d for d in hist.get(rid, []) if d >= week_ago]
        if today not in dates:
            dates.append(today)
        hist[rid] = dates
    snapshots.kv_set(conn, "fires7d", hist)
    return counts


def compose_board(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    today = now.date().isoformat()
    loc = conn.execute("SELECT * FROM location WHERE is_primary=1").fetchone()
    ctx = {"stamp_weather": "unavailable", "stamp_alerts": "unavailable",
           "primary_msg": None, "secondary_msgs": [], "weather_state": "unavailable",
           "alerts_state": "unknown", "alerts": [], "alerts_attention": None,
           "announcements": [], "transit": [], "transit_routes": [],
           "stamp_transit": None, "air": None, "tomorrow": None,
           "forecast": None, "outlook": None,
           "location_label": loc["label"] if loc else "Home"}

    if loc is None:
        ctx["primary_msg"] = "Setup not complete — visit /admin/setup."
        ctx["pip"] = {"state": "amber", "label": "setup needed"}
        ctx["layout"] = ["weather", "alerts", "announcements"]
        return ctx

    # ---- weather meaning
    om_snap = snapshots.latest(conn, loc["id"], "open_meteo")
    w_state = snapshots.freshness(om_snap and om_snap["fetched_at"],
                                  OM.poll_seconds_fresh, OM.stale_after_seconds, now)
    ctx["weather_state"] = w_state
    ctx["stamp_weather"] = snapshots.stamp(om_snap and om_snap["fetched_at"], w_state)

    warning_active = False
    nws_snap = snapshots.latest(conn, loc["id"], "nws")
    a_state = snapshots.freshness(nws_snap and nws_snap["fetched_at"],
                                  NWS.poll_seconds_fresh, NWS.stale_after_seconds, now)
    # SAFETY: stale/missing alerts => unknown, never "none"
    ctx["alerts_state"] = a_state if a_state != "unavailable" else "unknown"
    ctx["stamp_alerts"] = snapshots.stamp(nws_snap and nws_snap["fetched_at"], a_state)
    if nws_snap:
        norm = NWSAdapter().normalize(nws_snap["payload"])
        warning_active = norm["warning_active"]
        # severity tier per alert: WARNING (red) > WATCH > ADVISORY (plan §7.3)
        for a in norm["_alerts"]:
            ev = a.get("event") or ""
            a["tier"] = ("warning" if "Warning" in ev
                         or a.get("severity") in ("Extreme", "Severe")
                         else "watch" if "Watch" in ev else "advisory")
        ctx["alerts"] = norm["_alerts"]
        ctx["alerts_attention"] = "red" if warning_active else \
            ("amber" if norm["_alerts"] else None)

    if om_snap:
        season_state = snapshots.kv_get(conn, "season_state", {})
        try:
            fields = derive_fields(om_snap["payload"]["hourly"],
                                   om_snap["payload"]["daily"],
                                   today, now.hour, season_state)
            snapshots.kv_set(conn, "season_state", season_state)
            caps = snapshots.kv_get(conn, "fires7d", {})
            week_ago = (now.date() - timedelta(days=7)).isoformat()
            fired = []
            for r in SEEDS:
                if not _rule_enabled(conn, r["id"]):
                    continue
                recent = len([d for d in caps.get(r["id"], []) if d >= week_ago])
                f = evaluate(r, fields, month=now.month, recent_fires_7d=recent,
                             hour=now.hour)          # display-time windows
                if f:
                    fired.append(f)
            # user-created rules run through the same engine (plan §7.1a)
            for r in user_rules.load_enabled(conn):
                f = evaluate(r, fields, month=now.month, hour=now.hour)
                if f:
                    fired.append(f)
            try:
                ctx["tomorrow"] = derive_tomorrow(om_snap["payload"]["hourly"],
                                                  om_snap["payload"]["daily"], today)
            except (KeyError, ValueError):
                ctx["tomorrow"] = None
            ctx["forecast"] = _forecast_grid(om_snap["payload"]["hourly"],
                                             today, now.hour,
                                             layout.get_forecast_horizon(conn))
            ctx["outlook"] = _outlook(om_snap["payload"]["daily"], today,
                                      om_snap["payload"]["hourly"])
            _fire_caps(conn, [f.rule_id for f in fired
                              if any(s.get("max_fires_per_7d") and s["id"] == f.rule_id
                                     for s in SEEDS)], today)
            primary, secondary = compose(fired, warning_active)
            ctx["primary_msg"] = primary[0].message if primary else "No notable signals."
            ctx["secondary_msgs"] = [f.message for f in secondary]
        except (KeyError, ValueError):
            ctx["weather_state"] = "degraded"
            ctx["primary_msg"] = "Weather data incomplete — retrying."
    else:
        ctx["primary_msg"] = "Weather unavailable — waiting for first fetch."

    # ---- transit line cards (C1 + segment scoping): one card per monitor
    monitors = conn.execute(
        "SELECT field, config_json FROM monitor WHERE adapter='mta'"
        " ORDER BY id").fetchall()
    ctx["transit"] = []
    ctx["stamp_transit"] = None
    # live trip updates, shared by line rows and chains. FRESH only — a
    # 5-minute-old "next train" is a lie. Missing/stale => scheduled fallback.
    from app.adapters.mta_rt import MTARealtimeAdapter, live_chain, next_departures
    rt_snap = snapshots.latest(conn, loc["id"], "mta_rt")
    # two tiers of trust by AGE: LIVE badge + chain totals demand ≤60s; next-
    # train times get a grace window (≤5 min) because they self-prune — past
    # departures drop out — but the card admits the age. Beyond 5 min: times
    # gone and the failure is named, never silent (invariant 2).
    rt_m = MTARealtimeAdapter.manifest
    rt_age = ((now - datetime.fromisoformat(rt_snap["fetched_at"]))
              .total_seconds() if rt_snap else None)
    trips_all = rt_snap["payload"].get("trips", []) if rt_snap else []
    trips_live = trips_all if rt_age is not None \
        and rt_age <= rt_m.poll_seconds_fresh else []
    trips_view = trips_all if rt_age is not None \
        and rt_age <= rt_m.stale_after_seconds else []
    ctx["transit_rt_note"] = None
    if rt_age is not None and rt_age > rt_m.stale_after_seconds:
        ctx["transit_rt_note"] = "Live times unavailable — retrying."
    elif rt_age is not None and rt_age > rt_m.poll_seconds_fresh:
        ctx["transit_rt_note"] = (
            f"Live times updated {max(1, round(rt_age / 60))} min ago")

    def _dep_labels(line, from_id, to_id):
        return [datetime.fromtimestamp(d).strftime("%-I:%M")
                for d in next_departures(trips_view, line, from_id, to_id,
                                         int(now.timestamp()))]

    if monitors:
        from app.transit import gtfs
        mta_snap = snapshots.latest(conn, loc["id"], "mta")
        t_state = snapshots.freshness(mta_snap and mta_snap["fetched_at"],
                                      MTAAdapter.manifest.poll_seconds_fresh,
                                      MTAAdapter.manifest.stale_after_seconds, now)
        ctx["stamp_transit"] = snapshots.stamp(mta_snap and mta_snap["fetched_at"],
                                               t_state)
        normalized = MTAAdapter().normalize(mta_snap["payload"]) if mta_snap else {}
        for m in monitors:
            seg_cfg = json.loads(m["config_json"] or "{}")
            segment = None
            if seg_cfg.get("from_id") and seg_cfg.get("to_id"):
                segment = gtfs.segment_ids(m["field"], seg_cfg["from_id"],
                                           seg_cfg["to_id"])
            view = line_view(m["field"], normalized, segment=segment,
                             now_epoch=int(now.timestamp()))
            view["state"] = t_state
            view["segment_label"] = (
                f"{seg_cfg.get('from_name', '')} → {seg_cfg.get('to_name', '')}"
                if segment else "")
            view["next_trains"] = (
                _dep_labels(m["field"], seg_cfg["from_id"], seg_cfg["to_id"])
                if trips_view and segment else [])
            if t_state == "unavailable":     # honest state: never claim good service
                view["label"] = "Status unknown"
                view["attention"] = None
            ctx["transit"].append(view)

    # ---- chained routes (C2 legs): one combined card per route
    ctx["transit_routes"] = []
    route_rows = conn.execute(
        "SELECT name, legs_json FROM commute_profile WHERE mode='train'"
        " AND actively_monitored=1 ORDER BY id").fetchall()
    if route_rows:
        from app.transit import gtfs
        mta_snap = snapshots.latest(conn, loc["id"], "mta")
        t_state = snapshots.freshness(mta_snap and mta_snap["fetched_at"],
                                      MTAAdapter.manifest.poll_seconds_fresh,
                                      MTAAdapter.manifest.stale_after_seconds, now)
        ctx["stamp_transit"] = ctx["stamp_transit"] or snapshots.stamp(
            mta_snap and mta_snap["fetched_at"], t_state)
        normalized = MTAAdapter().normalize(mta_snap["payload"]) if mta_snap else {}
        for r in route_rows:
            legs = []
            ride_s, timed = 0, True
            cfg_legs = json.loads(r["legs_json"] or "[]")
            for leg in cfg_legs:
                segment = gtfs.segment_ids(leg["line"], leg["from_id"], leg["to_id"])
                v = line_view(leg["line"], normalized, segment=segment,
                              now_epoch=int(now.timestamp()))
                v["from_name"], v["to_name"] = leg["from_name"], leg["to_name"]
                s = gtfs.travel_seconds(leg["line"], leg["from_id"], leg["to_id"])
                if s is None:               # dataset predates time capture
                    timed = False
                else:
                    ride_s += s
                v["ride_min"] = round(s / 60) if s else None   # scheduled default
                v["next_trains"] = (_dep_labels(leg["line"], leg["from_id"],
                                                leg["to_id"])
                                    if trips_view else [])
                legs.append(v)
            # scheduled total: ride time + transfer buffer per change.
            # HONEST LABEL: timetable estimate, not live — GTFS-RT is C2 slice 3.
            est_min = (round(ride_s / 60) + TRANSFER_BUFFER_MIN * (len(legs) - 1)
                       if timed and legs else None)
            worst = min((leg["status"] for leg in legs),
                        key=STATUS_RANK.index, default="good")
            live = (live_chain(cfg_legs, trips_live, int(now.timestamp()))
                    if trips_live else None)
            if live:
                # per-leg live ride times replace the scheduled defaults
                for v, lt in zip(legs, live["legs"], strict=True):
                    v["ride_min"], v["ride_live"] = lt["ride_min"], True
                live = {"total_min": live["total_min"],
                        "depart_label": datetime.fromtimestamp(
                            live["depart_epoch"]).strftime("%-I:%M")}
            route = {"name": r["name"], "legs": legs, "state": t_state,
                     "est_min": est_min, "live": live, "status": worst,
                     "label": {"good": "✓ All clear", "delays": "Delays en route",
                               "service_change": "Service change en route",
                               "planned_work": "Planned work en route",
                               "suspended": "Leg suspended"}[worst],
                     "attention": {"good": None, "planned_work": None,
                                   "delays": "amber", "service_change": "amber",
                                   "suspended": "red"}[worst]}
            if t_state == "unavailable":
                route["label"], route["attention"] = "Status unknown", None
            ctx["transit_routes"].append(route)

    # ---- combined Transit card summary: one card, worst status wins the
    # headline and the border (glanceability survives any number of rows)
    ctx["transit_summary"] = None
    entries = ([("line", v) for v in ctx["transit"]]
               + [("route", r) for r in ctx["transit_routes"]])
    if entries:
        kind, worst = min(entries,
                          key=lambda e: STATUS_RANK.index(e[1]["status"]))
        stale = any(e[1].get("state") == "stale" for e in entries)
        if any(e[1].get("state") == "unavailable" for e in entries):
            summary = {"status": "unknown", "attention": None, "stale": stale,
                       "headline": "Status unknown", "sub": ""}
        elif worst["status"] == "good":
            summary = {"status": "good", "attention": None, "stale": stale,
                       "headline": "✓ Good service on all lines", "sub": ""}
        elif kind == "line":
            disp = ("LIRR" if worst["line"] == "LIRR"
                    else "Metro-North" if worst["line"] == "MNR"
                    else f"{worst['line']} train")
            where = (f" · {worst['segment_label']}"
                     if worst.get("segment_label") else "")
            summary = {"status": worst["status"],
                       "attention": worst["attention"], "stale": stale,
                       "headline": f"{disp}{where} — {worst['label']}",
                       "sub": worst.get("headline", "")}
        else:
            # name the culprit LEG — the route row below already shows the
            # combined verdict, so repeating it in big type adds nothing
            bad = min(worst["legs"],
                      key=lambda leg: STATUS_RANK.index(leg["status"]))
            summary = {"status": worst["status"],
                       "attention": worst["attention"], "stale": stale,
                       "headline": f"{worst['name']} — {bad['line']} leg"
                                   f" {bad['label'].lower()}",
                       "sub": bad.get("headline", "")}
        ctx["transit_summary"] = summary

    # ---- air quality card (V1.1): appears once an AirNow key is stored
    ctx["air"] = None
    if secrets.exists(conn, "airnow"):
        aq_snap = snapshots.latest(conn, loc["id"], "airnow")
        a_state = snapshots.freshness(aq_snap and aq_snap["fetched_at"],
                                      AirNowAdapter.manifest.poll_seconds_fresh,
                                      AirNowAdapter.manifest.stale_after_seconds, now)
        air = {"state": a_state,
               "stamp": snapshots.stamp(aq_snap and aq_snap["fetched_at"], a_state)}
        if aq_snap:
            air.update(AirNowAdapter().normalize(aq_snap["payload"]))
        else:
            air.update({"aqi": None, "meaning": None})
        ctx["air"] = air

    # ---- pollen card (V1.2 slice 1): key + at least one watched type.
    # Fresh all-zero (out of season) => card rests; stale zeros still show.
    ctx["pollen"] = None
    ptypes = [r["field"] for r in conn.execute(
        "SELECT field FROM monitor WHERE adapter='pollen'"
        " ORDER BY field").fetchall()]
    if ptypes and secrets.exists(conn, "google_pollen"):
        from app.adapters.google_pollen import TYPE_LABELS, GooglePollenAdapter
        gp = GooglePollenAdapter.manifest
        p_snap = snapshots.latest(conn, loc["id"], "google_pollen")
        p_state = snapshots.freshness(p_snap and p_snap["fetched_at"],
                                      gp.poll_seconds_fresh,
                                      gp.stale_after_seconds, now)
        rows = []
        if p_snap:
            norm = GooglePollenAdapter().normalize(p_snap["payload"])
            for t in ptypes:
                # a watched field is a type bucket OR a species code
                info = norm["types"].get(t) or norm["plants"].get(t)
                if info is None:             # species not reported today
                    info = {"index": 0, "category": "No reading",
                            "meaning": "not reported at your location today"}
                label = TYPE_LABELS.get(t) or info.get("display") or t.title()
                rows.append({"type": t, "label": label,
                             "index": info["index"],
                             "category": info["category"],
                             "meaning": info["meaning"],
                             "attention": "amber" if info["index"] >= 4 else None})
        pollen = {"state": p_state, "rows": rows,
                  "attention": ("amber" if any(r["index"] >= 4 for r in rows)
                                else None),
                  "stamp": snapshots.stamp(p_snap and p_snap["fetched_at"],
                                           p_state)}
        if p_state == "fresh" and rows and all(r["index"] == 0 for r in rows):
            pollen = None                    # out of season — card rests
        ctx["pollen"] = pollen

    # ---- announcements
    rows = conn.execute(
        "SELECT text, priority FROM announcement WHERE expires_at IS NULL OR expires_at > ?"
        " ORDER BY priority DESC, created_at DESC LIMIT 5",
        (now.isoformat(timespec='seconds'),)).fetchall()
    ctx["announcements"] = [{"text": r["text"], "priority": r["priority"]} for r in rows]

    ctx["layout"] = layout.visible_order(conn, ctx)
    ctx["density"] = layout.get_density(conn)
    ctx["forecast_style"] = layout.get_forecast_style(conn)
    ctx["forecast_horizon"] = layout.get_forecast_horizon(conn)
    ctx["pip"] = _pip(ctx)
    return ctx


_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

# WMO weather codes -> one glanceable word (outlook strip)
_WMO_WORDS = {0: "Clear", 1: "Mostly sun", 2: "Part cloudy", 3: "Cloudy",
              45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
              56: "Icy drizzle", 57: "Icy drizzle", 61: "Rain", 63: "Rain",
              65: "Rain", 66: "Icy rain", 67: "Icy rain", 71: "Snow",
              73: "Snow", 75: "Snow", 77: "Snow", 80: "Showers", 81: "Showers",
              82: "Showers", 85: "Snow showers", 86: "Snow showers",
              95: "Storms", 96: "Storms", 99: "Storms"}


# horizon (total hours) -> hours visible in the window at once. 72h shows a
# 48h window and pans across the extra day; 12/48 fit their window statically.
_HORIZON_WINDOW = {12: 12, 48: 48, 72: 48}


def _forecast_grid(hourly: dict, today_iso: str, now_h: int,
                   hours_n: int = 12) -> dict | None:
    """Next N hours as grid columns (trailsnh-style, plan approval).
    hours_n is the admin-chosen horizon (12/48/72)."""
    try:
        start = hourly["time"].index(f"{today_iso}T{now_h:02d}:00")
    except (KeyError, ValueError):
        return None

    def g(key, i):
        series = hourly.get(key)
        return series[i] if series and i < len(series) else None

    hours = []
    for i in range(start, min(start + hours_n, len(hourly["time"]))):
        h = int(hourly["time"][i][11:13])
        wd = g("wind_direction_10m", i)
        hours.append({
            "label": ("12A" if h == 0 else f"{h}A" if h < 12
                      else "12P" if h == 12 else f"{h - 12}P"),
            "date": hourly["time"][i][:10],
            "temp": g("temperature_2m", i),
            "feels": g("apparent_temperature", i),
            "pop": g("precipitation_probability", i),
            "cloud": g("cloud_cover", i),
            "wind": g("wind_speed_10m", i),
            "gust": g("wind_gusts_10m", i),
            "dir": None if wd is None else _COMPASS[round(wd / 45) % 8],
        })
    if not hours:
        return None
    hpw = _HORIZON_WINDOW.get(hours_n, len(hours))
    return {"hours": hours, "horizon": hours_n,
            "chart": _forecast_chart(hours, hpw)}


# arrow = direction the wind blows TOWARD (mockup convention: "W →")
_DIR_ARROWS = {"N": "↓", "NE": "↙", "E": "←", "SE": "↖",
               "S": "↑", "SW": "↗", "W": "→", "NW": "↘"}


def _smooth_path(pts: list[tuple], close_to: float | None = None) -> str:
    """Catmull-Rom -> cubic bezier, Chartist's smooth-line look. close_to:
    drop to that baseline y and close (area series like cloud/pop)."""
    if not pts:
        return ""
    d = f"M{pts[0][0]},{pts[0][1]}"
    for i in range(1, len(pts)):
        p0 = pts[i - 2] if i >= 2 else pts[i - 1]
        p1, p2 = pts[i - 1], pts[i]
        p3 = pts[i + 1] if i + 1 < len(pts) else p2
        d += (f" C{p1[0] + (p2[0] - p0[0]) / 6:.1f},"
              f"{p1[1] + (p2[1] - p0[1]) / 6:.1f}"
              f" {p2[0] - (p3[0] - p1[0]) / 6:.1f},"
              f"{p2[1] - (p3[1] - p1[1]) / 6:.1f} {p2[0]},{p2[1]}")
    if close_to is not None:
        d += f" L{pts[-1][0]},{close_to} L{pts[0][0]},{close_to} Z"
    return d


# chart frame (Chartist-style: grid area, baseline, top). The svg viewBox is
# 480 wide = the visible WINDOW; _CH_LEFT is the y-axis gutter, _CH_RIGHT a
# breathing margin. Point spacing (step) is derived per-horizon so a window's
# worth of hours fills the plot; wider horizons (72h) overflow and pan.
_CH_TOP, _CH_BASE = 12, 88
_CH_LEFT, _CH_RIGHT, _CH_W = 18, 22, 480


def _extreme_labels(pts: list[tuple], vals: list) -> list[dict]:
    """Temp labels the trailsnh way: endpoints + local highs/lows only
    (their 55° → 72° → 51° pattern), minima labeled below the line."""
    n = len(vals)
    if n == 0:
        return []
    keep = {0, n - 1}
    for i in range(1, n - 1):
        if (vals[i] >= vals[i - 1] and vals[i] >= vals[i + 1]) or \
                (vals[i] <= vals[i - 1] and vals[i] <= vals[i + 1]):
            keep.add(i)
    out, last_val = [], None
    for i in sorted(keep):
        if last_val is not None and vals[i] == last_val:
            continue                     # plateau: one label is enough
        last_val = vals[i]
        is_min = vals[i] <= min(vals[max(0, i - 1)], vals[min(n - 1, i + 1)])
        # endpoints anchor inward so they clear the y-axis gutter (left) and
        # the window edge (right); interior labels stay centred on the point
        anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
        out.append({"x": pts[i][0],
                    "y": pts[i][1] + 13 if is_min else pts[i][1] - 6,
                    "text": f"{vals[i]:.0f}°", "anchor": anchor})
    return out


def _forecast_chart(hours: list[dict], hpw: int | None = None) -> dict | None:
    """Chartist-faithful geometry (verified against trailsnh's #wxsvg):
    horizontal gridlines, smooth AREA series from the baseline (cloud gray,
    pop blue), smooth temp LINE with point dots — all on one 0-100 y-axis
    (°F shares the percent scale, the trailsnh trick) — day-separator lines
    where the date changes, hour labels below.

    hpw = hours visible per window. Point spacing fills the plot with hpw
    hours; when len(hours) > hpw the content overflows the 480-wide window
    and `scroll` is the pan distance (in user units) the display animates."""
    n = len(hours)
    hpw = hpw or n
    plot_w = _CH_W - _CH_LEFT - _CH_RIGHT
    step = plot_w / max(hpw - 1, 1)
    xs = [round(_CH_LEFT + i * step, 1) for i in range(n)]
    dense = hpw > 12                      # 48/72h: thin labels, drop dots/rows
    lab_every = 6 if dense else 2

    def vy(v):        # value 0-100 -> chart y
        v = max(0, min(100, v))
        return round(_CH_BASE - v / 100 * (_CH_BASE - _CH_TOP), 1)

    temps = [(x, h["temp"]) for x, h in zip(xs, hours, strict=True)
             if h["temp"] is not None]
    if not temps:
        return None
    temp_pts = [(x, vy(t)) for x, t in temps]
    chart = {
        "grid_y": [_CH_BASE - i * (_CH_BASE - _CH_TOP) / 4 for i in range(5)],
        "cloud_path": _smooth_path(
            [(x, vy(h["cloud"] or 0)) for x, h in zip(xs, hours, strict=True)],
            close_to=_CH_BASE),
        "pop_path": _smooth_path(
            [(x, vy(h["pop"] or 0)) for x, h in zip(xs, hours, strict=True)],
            close_to=_CH_BASE),
        "temp_path": _smooth_path(temp_pts),
        # hourly dots only when spacing is generous (12h); they'd smear at 48/72
        "dots": [] if dense else [{"x": x, "y": y} for x, y in temp_pts],
        "temp_labels": _extreme_labels(temp_pts, [t for _, t in temps]),
        "wind_path": _smooth_path(
            [(x, vy(h["wind"])) for x, h in zip(xs, hours, strict=True)
             if h["wind"] is not None]),
        "gust_path": _smooth_path(
            [(x, vy(h["gust"])) for x, h in zip(xs, hours, strict=True)
             if h["gust"] is not None]),
        "hours": [{"x": xs[i], "text": hours[i]["label"]}
                  for i in range(0, n, lab_every)],
        "day_seps": [], "winds": [], "dirs": [],
        # pan distance: extra hours beyond the window, in user units
        "scroll": round(max(0, n - hpw) * step, 1),
        "content_w": round(_CH_LEFT + (n - 1) * step + _CH_RIGHT, 1),
    }
    for i in range(1, n):
        if hours[i].get("date") and hours[i]["date"] != hours[i - 1].get("date"):
            chart["day_seps"].append({
                "x": round(xs[i] - step / 2, 1),
                "text": date.fromisoformat(hours[i]["date"]).strftime("%A")})
    # numeric wind/dir row only in detail (12h) mode — too dense to read at 48/72
    if not dense:
        for i in range(1, n, lab_every):
            h = hours[i]
            if h["wind"] is None:
                continue
            g = h["gust"]
            gusty = g is not None and (g - h["wind"] >= 8 or g >= 20)
            chart["winds"].append({
                "x": xs[i] - 12, "hot": g is not None and g >= 30,
                "text": f"{h['wind']:.0f}" + (f" g {g:.0f}" if gusty else "")})
            if h["dir"]:
                chart["dirs"].append(
                    {"x": xs[i] - 12,
                     "text": f"{h['dir']} {_DIR_ARROWS[h['dir']]}"})
    return chart


def _wmo_icon(code) -> str | None:
    if code is None:
        return None
    if code <= 1:
        return "sun"
    if code == 2:
        return "part"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "rain"        # drizzle / rain / showers


def _rain_when(hourly: dict, day_iso: str) -> str | None:
    """AM / PM / all day, from the day's hourly rain chances."""
    times = hourly.get("time") or []
    pops = hourly.get("precipitation_probability") or []
    am, pm = 0, 0
    for i, t in enumerate(times):
        if not t.startswith(day_iso) or i >= len(pops) or pops[i] is None:
            continue
        h = int(t[11:13])
        if 6 <= h < 12:
            am = max(am, pops[i])
        elif 12 <= h <= 21:
            pm = max(pm, pops[i])
    if am >= 30 and pm >= 30:
        return "all day"
    if am >= 30:
        return "AM"
    if pm >= 30:
        return "PM"
    return None


def _outlook(daily: dict, today_iso: str, hourly: dict | None = None) -> dict | None:
    """Tomorrow through day 7, honest about forecast decay: days 1-4 full,
    day 5 medium, 6-7 low confidence (accuracy note in the plan)."""
    try:
        start = daily["time"].index(today_iso)
    except (KeyError, ValueError):
        return None

    def g(key, i):
        series = daily.get(key)
        return series[i] if series and i < len(series) else None

    days = []
    for i in range(start + 1, min(start + 8, len(daily["time"]))):
        ahead = i - start
        code = g("weather_code", i)
        day_iso = daily["time"][i]
        days.append({
            "label": date.fromisoformat(day_iso).strftime("%a"),
            "high": g("temperature_2m_max", i),
            "low": g("temperature_2m_min", i),
            "pop": g("precipitation_probability_max", i),
            "word": _WMO_WORDS.get(code) if code is not None else None,
            "icon": _wmo_icon(code),
            "rain_when": _rain_when(hourly, day_iso) if hourly else None,
            "conf": "high" if ahead <= 4 else "medium" if ahead == 5 else "low",
        })
    if len(days) < 2:                   # 1 day = tomorrow strip's job
        return None
    return {"days": days, "accuracy": _accuracy_band(days)}


# how confident the outlook is, per tier — replaces the old prose caveat with
# an honest number (typical multi-day forecast skill, matches the plan's fade)
_CONF_PCT = {"high": 90, "medium": 70, "low": 50}


def _accuracy_band(days: list[dict]) -> list[dict]:
    """Group consecutive days of equal confidence into labelled segments so the
    board can draw one accuracy bar aligned under the day columns. span = how
    many day-columns the segment covers; pct = that tier's accuracy."""
    band = []
    for d in days:
        pct = _CONF_PCT[d["conf"]]
        if band and band[-1]["conf"] == d["conf"]:
            band[-1]["span"] += 1
        else:
            band.append({"conf": d["conf"], "pct": pct, "span": 1})
    return band


def get_live_fields(conn: sqlite3.Connection,
                    now: datetime | None = None) -> dict | None:
    """Current derived fields for the rule editor's preview.
    AIDEV-CAUTION: passes a COPY of season_state — a preview must never
    consume a one-shot (first-freeze etc.)."""
    now = now or datetime.now()
    loc = conn.execute("SELECT * FROM location WHERE is_primary=1").fetchone()
    if loc is None:
        return None
    snap = snapshots.latest(conn, loc["id"], "open_meteo")
    if snap is None:
        return None
    try:
        state_copy = dict(snapshots.kv_get(conn, "season_state", {}))
        return derive_fields(snap["payload"]["hourly"], snap["payload"]["daily"],
                             now.date().isoformat(), now.hour, state_copy)
    except (KeyError, ValueError):
        return None


def _pip(ctx: dict) -> dict:
    """Status pip per plan §7.5 — green: healthy; amber: stale/retrying;
    red: no usable weather AND alert data."""
    w, a = ctx["weather_state"], ctx["alerts_state"]
    if w in ("unavailable", "degraded") and a == "unknown":
        return {"state": "red", "label": "no data — check admin"}
    if w in ("stale", "unavailable", "degraded") or a in ("stale", "unknown"):
        worst = "retrying" if w in ("unavailable", "degraded") else "some data stale"
        return {"state": "amber", "label": worst}
    return {"state": "green", "label": "all signals fresh"}
