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
        from app.adapters.mta_rt import MTARealtimeAdapter, live_chain
        from app.transit import gtfs
        mta_snap = snapshots.latest(conn, loc["id"], "mta")
        t_state = snapshots.freshness(mta_snap and mta_snap["fetched_at"],
                                      MTAAdapter.manifest.poll_seconds_fresh,
                                      MTAAdapter.manifest.stale_after_seconds, now)
        ctx["stamp_transit"] = ctx["stamp_transit"] or snapshots.stamp(
            mta_snap and mta_snap["fetched_at"], t_state)
        normalized = MTAAdapter().normalize(mta_snap["payload"]) if mta_snap else {}
        # live trip updates: FRESH only — a 5-minute-old "next train" is a lie.
        # Missing/stale => scheduled fallback, never a blank (invariant 2).
        rt_snap = snapshots.latest(conn, loc["id"], "mta_rt")
        rt_state = snapshots.freshness(
            rt_snap and rt_snap["fetched_at"],
            MTARealtimeAdapter.manifest.poll_seconds_fresh,
            MTARealtimeAdapter.manifest.stale_after_seconds, now)
        trips = (rt_snap["payload"].get("trips", [])
                 if rt_snap and rt_state == "fresh" else [])
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
                legs.append(v)
            # scheduled total: ride time + transfer buffer per change.
            # HONEST LABEL: timetable estimate, not live — GTFS-RT is C2 slice 3.
            est_min = (round(ride_s / 60) + TRANSFER_BUFFER_MIN * (len(legs) - 1)
                       if timed and legs else None)
            worst = min((leg["status"] for leg in legs),
                        key=STATUS_RANK.index, default="good")
            live = live_chain(cfg_legs, trips, int(now.timestamp())) if trips else None
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
    ctx["pip"] = _pip(ctx)
    return ctx


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
