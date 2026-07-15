"""Board composition — snapshots → derived fields → seed rules → card context.

AIDEV-NOTE: this is where invariant 2 gets rendered: every card resolves to a
labeled state. Alerts especially — SAFETY: a missing/stale NWS snapshot renders
as 'alert status unknown', never as 'no active alerts'.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from app.adapters.airnow import AirNowAdapter
from app.adapters.mta import MTAAdapter, line_view
from app.adapters.nws import NWSAdapter
from app.adapters.open_meteo import OpenMeteoAdapter
from app.adapters.weather_derive import derive_fields
from app.core import secrets, snapshots
from app.rules.engine import compose, evaluate

SEEDS = json.loads(
    (Path(__file__).parents[1] / "rules" / "seeds.json").read_text())["rules"]
OM, NWS = OpenMeteoAdapter.manifest, NWSAdapter.manifest


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
           "announcements": [], "transit": [], "stamp_transit": None,
           "air": None, "location_label": loc["label"] if loc else "Home"}

    if loc is None:
        ctx["primary_msg"] = "Setup not complete — visit /admin/setup."
        ctx["pip"] = {"state": "amber", "label": "setup needed"}
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
                f = evaluate(r, fields, month=now.month, recent_fires_7d=recent)
                if f:
                    fired.append(f)
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

    # ---- transit line cards (C1): one card instance per monitored line
    monitors = conn.execute(
        "SELECT field FROM monitor WHERE adapter='mta' ORDER BY id").fetchall()
    ctx["transit"] = []
    ctx["stamp_transit"] = None
    if monitors:
        mta_snap = snapshots.latest(conn, loc["id"], "mta")
        t_state = snapshots.freshness(mta_snap and mta_snap["fetched_at"],
                                      MTAAdapter.manifest.poll_seconds_fresh,
                                      MTAAdapter.manifest.stale_after_seconds, now)
        ctx["stamp_transit"] = snapshots.stamp(mta_snap and mta_snap["fetched_at"],
                                               t_state)
        normalized = MTAAdapter().normalize(mta_snap["payload"]) if mta_snap else {}
        for m in monitors:
            view = line_view(m["field"], normalized)
            view["state"] = t_state
            if t_state == "unavailable":     # honest state: never claim good service
                view["label"] = "Status unknown"
                view["attention"] = None
            ctx["transit"].append(view)

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

    # ---- announcements
    rows = conn.execute(
        "SELECT text, priority FROM announcement WHERE expires_at IS NULL OR expires_at > ?"
        " ORDER BY priority DESC, created_at DESC LIMIT 5",
        (now.isoformat(timespec='seconds'),)).fetchall()
    ctx["announcements"] = [{"text": r["text"], "priority": r["priority"]} for r in rows]

    ctx["pip"] = _pip(ctx)
    return ctx


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
