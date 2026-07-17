"""Admin — plain, boring, form-based (by design). All mutating routes: CSRF."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.adapters.nws import NWSAdapter
from app.adapters.open_meteo import OpenMeteoAdapter
from app.core import backup, config_mgr, db, diagnostics, layout, secrets, security, snapshots
from app.jobs import scheduler

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
ADAPTERS = {"open_meteo": OpenMeteoAdapter(), "nws": NWSAdapter()}


# ---------------------------------------------------------------- helpers

def _device(conn):
    return conn.execute("SELECT * FROM device WHERE id=1").fetchone()


def _guard(request: Request, conn) -> RedirectResponse | dict:
    """-> session dict, or a redirect to setup/login."""
    if _device(conn) is None:
        return RedirectResponse("/admin/setup", status_code=303)
    session = security.read_session(request)
    if not session:
        return RedirectResponse("/admin/login", status_code=303)
    return session


def _render(request, name, session=None, **ctx):
    ctx["csrf"] = session["csrf"] if session else None
    return templates.TemplateResponse(request, f"admin/{name}", ctx)


async def _geocode_zip(zipcode: str) -> dict | None:
    """Interim: Open-Meteo geocoding until the bundled ZCTA dataset lands."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                 params={"name": zipcode, "count": 1, "country": "US"})
            hit = r.json()["results"][0]
            return {"lat": hit["latitude"], "lon": hit["longitude"],
                    "label": hit.get("name", "Home"),
                    "tz": hit.get("timezone", "America/New_York")}
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- setup wizard
# Multi-step per spec (plan §6): password → ZIP w/ resolved-location confirm →
# done screen (URLs + QR + first-announcement teaser). Resumable at every step.

def _setup_stage(conn) -> str:
    """'password' | 'location' | 'complete'"""
    if _device(conn) is None:
        return "password"
    if not conn.execute("SELECT 1 FROM location WHERE is_primary=1").fetchone():
        return "location"
    return "complete"


def _wizard_redirect(stage: str) -> RedirectResponse:
    target = {"password": "/admin/setup", "location": "/admin/setup/location",
              "complete": "/admin"}[stage]
    return RedirectResponse(target, status_code=303)


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    conn = db.connect()
    try:
        stage = _setup_stage(conn)
    finally:
        conn.close()
    if stage != "password":
        return _wizard_redirect(stage)
    return _render(request, "setup.html")


@router.post("/setup", response_class=HTMLResponse)
async def setup_password(request: Request, password: str = Form(...),
                         password2: str = Form(...)):
    conn = db.connect()
    try:
        if _setup_stage(conn) != "password":
            return _wizard_redirect(_setup_stage(conn))
        if password != password2 or len(password) < 8:
            return _render(request, "setup.html",
                           error="Passwords must match and be at least 8 characters.")
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "INSERT INTO device (id, first_boot_at, timezone, admin_password_hash)"
                " VALUES (1, ?, 'America/New_York', ?)",
                (now, security.hash_password(password)))
    finally:
        conn.close()
    cookie, _ = security.make_session()
    resp = RedirectResponse("/admin/setup/location", status_code=303)
    resp.set_cookie(security.SESSION_COOKIE, cookie, httponly=True,
                    samesite="lax", max_age=security.SESSION_MAX_AGE)
    return resp


@router.get("/setup/location", response_class=HTMLResponse)
async def setup_location_form(request: Request):
    conn = db.connect()
    try:
        stage = _setup_stage(conn)
        if stage != "location":
            return _wizard_redirect(stage)
        session = security.read_session(request)
        if not session:
            return RedirectResponse("/admin/login", status_code=303)
        return _render(request, "setup_location.html", session)
    finally:
        conn.close()


@router.post("/setup/location", response_class=HTMLResponse)
async def setup_location_submit(request: Request, zipcode: str = Form(""),
                                latitude: str = Form(""), longitude: str = Form(""),
                                label: str = Form("Home"),
                                timezone: str = Form("America/New_York")):
    conn = db.connect()
    try:
        stage = _setup_stage(conn)
        if stage != "location":
            return _wizard_redirect(stage)
        session = security.read_session(request)
        if not session:
            return RedirectResponse("/admin/login", status_code=303)
        if latitude and longitude:   # manual path: user typed it, no confirm needed
            _create_location(conn, label, zipcode, float(latitude),
                             float(longitude), timezone)
            return RedirectResponse("/admin/setup/done", status_code=303)
        found = await _geocode_zip(zipcode)
        if found is None:
            return _render(request, "setup_location.html", session, zip_failed=True,
                           error="ZIP lookup failed — check it, or enter"
                                 " coordinates below.")
        # confirm screen: show the resolved MEANING, not coordinates (spec)
        return _render(request, "setup_confirm.html", session,
                       zipcode=zipcode, found=found)
    finally:
        conn.close()


@router.post("/setup/confirm", response_class=HTMLResponse)
async def setup_confirm(request: Request, zipcode: str = Form(""),
                        latitude: float = Form(...), longitude: float = Form(...),
                        label: str = Form("Home"),
                        timezone: str = Form("America/New_York"),
                        csrf: str = Form(None)):
    conn = db.connect()
    try:
        stage = _setup_stage(conn)
        if stage != "location":
            return _wizard_redirect(stage)
        session = security.read_session(request)
        if not session or not security.check_csrf(session, csrf):
            return RedirectResponse("/admin/setup/location", status_code=303)
        _create_location(conn, label, zipcode, latitude, longitude, timezone)
        return RedirectResponse("/admin/setup/done", status_code=303)
    finally:
        conn.close()


def _create_location(conn, label, zipcode, lat, lon, tz) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute("INSERT INTO location (label, zip, latitude, longitude,"
                     " is_primary) VALUES (?, ?, ?, ?, 1)",
                     (label or "Home", zipcode or None, lat, lon))
        conn.execute("UPDATE device SET timezone=?, setup_completed_at=? WHERE id=1",
                     (tz, now))
        conn.execute("INSERT INTO board (name, is_default, layout_json)"
                     " VALUES ('Default', 1, '{\"preset\": \"default\"}')")


@router.get("/setup/done", response_class=HTMLResponse)
async def setup_done(request: Request):
    import qrcode
    import qrcode.image.svg
    conn = db.connect()
    try:
        if _setup_stage(conn) != "complete":
            return _wizard_redirect(_setup_stage(conn))
        session = security.read_session(request)
        if not session:
            return RedirectResponse("/admin/login", status_code=303)
        host = request.url.hostname or "signalshack.local"
        display_url = f"http://{host}/display"
        img = qrcode.make(display_url,
                          image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=12)
        return _render(request, "setup_done.html", session,
                       display_url=display_url,
                       mdns_url="http://signalshack.local/display",
                       qr_svg=img.to_string(encoding="unicode"))
    finally:
        conn.close()


# ---------------------------------------------------------------- auth

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _render(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    if security.rate_limited(ip):
        return _render(request, "login.html",
                       error="Too many attempts — try again in 15 minutes.")
    conn = db.connect()
    try:
        device = _device(conn)
        if device is None:
            return RedirectResponse("/admin/setup", status_code=303)
        if not security.verify_password(device["admin_password_hash"], password):
            security.record_attempt(ip)
            return _render(request, "login.html", error="Wrong password.")
    finally:
        conn.close()
    cookie, _ = security.make_session()
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(security.SESSION_COOKIE, cookie, httponly=True,
                    samesite="lax", max_age=security.SESSION_MAX_AGE)
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(security.SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------- status home

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        loc = conn.execute("SELECT * FROM location WHERE is_primary=1").fetchone()
        sources = []
        for name, adapter in ADAPTERS.items():
            snap = loc and snapshots.latest(conn, loc["id"], name)
            state = snapshots.freshness(snap and snap["fetched_at"],
                                        adapter.manifest.poll_seconds_fresh,
                                        adapter.manifest.stale_after_seconds)
            sources.append({"name": name, "state": state,
                            "stamp": snapshots.stamp(snap and snap["fetched_at"], state)})
        events = conn.execute(
            "SELECT ts, service, event_type FROM event_log"
            " ORDER BY ts DESC LIMIT 10").fetchall()
        return _render(request, "status.html", guard, sources=sources,
                       location=loc, events=events)
    finally:
        conn.close()


@router.post("/refresh/{source}")
async def manual_refresh(request: Request, source: str, csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if not security.check_csrf(guard, csrf) or source not in ADAPTERS:
            return RedirectResponse("/admin", status_code=303)
        # debounce: one manual trigger per source per 60s
        last = snapshots.kv_get(conn, f"manual_refresh_{source}", None)
        now = datetime.now()
        if last and (now - datetime.fromisoformat(last)).total_seconds() < 60:
            return RedirectResponse("/admin", status_code=303)
        snapshots.kv_set(conn, f"manual_refresh_{source}",
                         now.isoformat(timespec="seconds"))
        loc = conn.execute("SELECT * FROM location WHERE is_primary=1").fetchone()
        if loc:
            await scheduler._fetch_and_store(conn, ADAPTERS[source], loc)
        return RedirectResponse("/admin", status_code=303)
    finally:
        conn.close()


# ---------------------------------------------------------------- announcements

@router.get("/announcements", response_class=HTMLResponse)
async def announcements(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        rows = conn.execute("SELECT * FROM announcement ORDER BY created_at DESC"
                            " LIMIT 20").fetchall()
        return _render(request, "announcements.html", guard, items=rows,
                       now=datetime.now().isoformat(timespec="seconds"))
    finally:
        conn.close()


@router.post("/announcements", response_class=HTMLResponse)
async def create_announcement(request: Request, text: str = Form(...),
                              priority: str = Form(""), hours: int = Form(12),
                              csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if not security.check_csrf(guard, csrf):
            return RedirectResponse("/admin/announcements", status_code=303)
        text = text.strip()[:200]                       # length-validated
        if text:
            now = datetime.now()
            expires = (now + timedelta(hours=max(1, min(hours, 168)))) \
                .isoformat(timespec="seconds")
            with conn:
                conn.execute(
                    "INSERT INTO announcement (text, priority, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    (text, 1 if priority else 0,
                     now.isoformat(timespec="seconds"), expires))
        return RedirectResponse("/admin/announcements", status_code=303)
    finally:
        conn.close()


@router.post("/announcements/{item_id}/delete")
async def delete_announcement(request: Request, item_id: int, csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            with conn:
                conn.execute("DELETE FROM announcement WHERE id=?", (item_id,))
        return RedirectResponse("/admin/announcements", status_code=303)
    finally:
        conn.close()


# ---------------------------------------------------------------- support & data

@router.get("/support", response_class=HTMLResponse)
async def support_page(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        checks = diagnostics.self_check(conn)
        return _render(request, "support.html", guard, checks=checks)
    finally:
        conn.close()


@router.post("/diagnostics/bundle")
async def diagnostics_bundle(request: Request, csrf: str = Form(None)):
    from fastapi.responses import Response
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if not security.check_csrf(guard, csrf):
            return RedirectResponse("/admin/support", status_code=303)
        data = diagnostics.build_bundle(conn)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition":
                f'attachment; filename="signalshack-diagnostics-{stamp}.zip"'})
    finally:
        conn.close()


@router.get("/backup/export")
async def backup_export(request: Request):
    from fastapi.responses import Response
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        bundle = backup.export_bundle(conn)
        stamp = datetime.now().strftime("%Y%m%d")
        return Response(json.dumps(bundle, indent=1), media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="signalshack-backup-{stamp}.json"'})
    finally:
        conn.close()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        return _render(request, "settings.html", guard,
                       settings=config_mgr.get_settings(conn),
                       airnow_configured=secrets.exists(conn, "airnow"),
                       pollen_configured=secrets.exists(conn, "google_pollen"),
                       pollen_types=_pollen_types(conn),
                       pollen_species=_pollen_species(conn))
    finally:
        conn.close()


def _pollen_types(conn) -> set[str]:
    return {r["field"] for r in conn.execute(
        "SELECT field FROM monitor WHERE adapter='pollen'").fetchall()}


def _pollen_species(conn) -> list[dict]:
    """Species Google reports at THIS location (from the latest snapshot) —
    the picker only offers plants that actually grow near the household."""
    from app.adapters.google_pollen import GooglePollenAdapter
    from app.core import snapshots
    loc = conn.execute("SELECT id FROM location WHERE is_primary=1").fetchone()
    if not loc:
        return []
    snap = snapshots.latest(conn, loc["id"], "google_pollen")
    if not snap:
        return []
    plants = GooglePollenAdapter().normalize(snap["payload"])["plants"]
    return sorted(({"code": c, "display": p["display"]}
                   for c, p in plants.items()), key=lambda s: s["display"])


@router.post("/settings/pollen")
async def pollen_key(request: Request, api_key: str = Form(""),
                     csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            key = secrets.clean_pasted_key(api_key)
            if key:
                secrets.store(conn, "google_pollen", key,
                              label="Google Pollen API key")
            else:
                secrets.delete(conn, "google_pollen")   # blank submit = remove
        return RedirectResponse("/admin/settings", status_code=303)
    finally:
        conn.close()


@router.post("/settings/pollen/types")
async def pollen_types(request: Request, csrf: str = Form(None)):
    from app.adapters.google_pollen import TYPES
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            form = await request.form()
            allowed = set(TYPES) | {s["code"] for s in _pollen_species(conn)}
            wanted = set(form.getlist("ptype")) & allowed
            with conn:
                conn.execute("DELETE FROM monitor WHERE adapter='pollen'")
                for t in sorted(wanted):
                    conn.execute("INSERT INTO monitor (adapter, field)"
                                 " VALUES ('pollen', ?)", (t,))
        return RedirectResponse("/admin/settings", status_code=303)
    finally:
        conn.close()


@router.post("/settings/pins")
async def settings_pins(request: Request, announce_pin: str = Form(""),
                        display_pin: str = Form(""), csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            current = config_mgr.get_settings(conn)
            current.update({"announce_pin": announce_pin.strip(),
                            "display_pin": display_pin.strip()})
            ok, problems = config_mgr.apply_settings(conn, current)
            if not ok:
                return _render(request, "settings.html", guard,
                               settings=config_mgr.get_settings(conn),
                               problems=problems,
                               airnow_configured=secrets.exists(conn, "airnow"))
        return RedirectResponse("/admin/settings", status_code=303)
    finally:
        conn.close()


@router.post("/settings/airnow")
async def airnow_key(request: Request, api_key: str = Form(""),
                     csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            key = secrets.clean_pasted_key(api_key)
            if key:
                secrets.store(conn, "airnow", key, label="AirNow API key")
            else:
                secrets.delete(conn, "airnow")   # blank submit = remove
        return RedirectResponse("/admin/settings", status_code=303)
    finally:
        conn.close()


@router.post("/settings", response_class=HTMLResponse)
async def settings_submit(request: Request, timezone: str = Form(...),
                          night_start: str = Form(...), night_end: str = Form(...),
                          display_poll_seconds: int = Form(20),
                          csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if not security.check_csrf(guard, csrf):
            return RedirectResponse("/admin/settings", status_code=303)
        ok, problems = config_mgr.apply_settings(conn, {
            "timezone": timezone, "night_start": night_start,
            "night_end": night_end, "display_poll_seconds": display_poll_seconds})
        return _render(request, "settings.html", guard,
                       settings=config_mgr.get_settings(conn),
                       saved=ok, problems=problems)
    finally:
        conn.close()


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        return _render(request, "privacy.html", guard)
    finally:
        conn.close()


# ---------------------------------------------------------------- layout

CARD_LABELS = {"weather": "Weather Meaning", "alerts": "NWS Alerts",
               "transit": "Transit lines", "air": "Air Quality",
               "pollen": "Pollen",
               "announcements": "Household announcements",
               "tomorrow": "Tomorrow strip"}


@router.get("/layout", response_class=HTMLResponse)
async def layout_page(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        cards = [{"type": c["type"], "enabled": c["enabled"],
                  "label": CARD_LABELS[c["type"]]}
                 for c in layout.get_layout(conn)]
        return _render(request, "layout.html", guard, cards=cards,
                       density=layout.get_density(conn),
                       densities=layout.DENSITIES)
    finally:
        conn.close()


@router.post("/layout/density")
async def layout_density(request: Request, density: str = Form(""),
                         csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            layout.set_density(conn, density)
        return RedirectResponse("/admin/layout", status_code=303)
    finally:
        conn.close()


@router.post("/layout/{card_type}/{action}")
async def layout_action(request: Request, card_type: str, action: str,
                        csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf) and card_type in layout.CARD_TYPES:
            if action in ("up", "down"):
                layout.move(conn, card_type, action)
            elif action == "toggle":
                layout.toggle(conn, card_type)
        return RedirectResponse("/admin/layout", status_code=303)
    finally:
        conn.close()


# ---------------------------------------------------------------- transit (C1)

SUBWAY_LINES = ["1", "2", "3", "4", "5", "6", "7", "A", "C", "E", "B", "D", "F",
                "M", "G", "J", "Z", "L", "N", "Q", "R", "W", "S", "SI"]
RAIL_LINES = ["LIRR", "MNR"]


@router.get("/transit", response_class=HTMLResponse)
async def transit_page(request: Request, msg: str = ""):
    from app.transit import gtfs
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        rows = conn.execute(
            "SELECT id, field, config_json FROM monitor WHERE adapter='mta'"
            " ORDER BY id").fetchall()
        dataset = gtfs.load()
        monitors = []
        for r in rows:
            cfg = json.loads(r["config_json"] or "{}")
            monitors.append({
                "id": r["id"], "field": r["field"], "cfg": cfg,
                "stops": gtfs.stops_for(r["field"]) if dataset else [],
                "is_subway": r["field"] in SUBWAY_LINES})
        routes_rows = conn.execute(
            "SELECT id, name, legs_json FROM commute_profile WHERE mode='train'"
            " ORDER BY id").fetchall()
        routes = [{"id": r["id"], "name": r["name"],
                   "legs": json.loads(r["legs_json"] or "[]")} for r in routes_rows]
        return _render(request, "transit.html", guard, monitors=monitors,
                       subway=SUBWAY_LINES, rail=RAIL_LINES, routes=routes,
                       has_dataset=dataset is not None, msg=msg)
    finally:
        conn.close()


@router.post("/transit/gtfs")
async def transit_gtfs_download(request: Request, csrf: str = Form(None)):
    """Download + process MTA stop data (one-time, ~1–2 min on the box)."""
    import asyncio

    from app.transit import gtfs
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if not security.check_csrf(guard, csrf):
            return RedirectResponse("/admin/transit", status_code=303)
    finally:
        conn.close()
    try:
        summary = await asyncio.to_thread(gtfs.build_dataset)
        msg = f"Stop data ready — {summary['routes']} routes, {summary['stops']} stops."
    except Exception as exc:  # noqa: BLE001 — report, don't crash admin
        msg = f"Download failed ({type(exc).__name__}) — try again."
    return RedirectResponse(f"/admin/transit?msg={msg}", status_code=303)


@router.get("/transit/leg-row", response_class=HTMLResponse)
async def transit_leg_row(request: Request):
    return _render(request, "_leg_row.html", None, subway=SUBWAY_LINES)


@router.get("/transit/leg-stops", response_class=HTMLResponse)
async def transit_leg_stops(request: Request, leg_line: str = ""):
    """htmx: from/to selects for the chosen line (dependent dropdowns)."""
    from app.transit import gtfs
    stops = gtfs.stops_for(leg_line) if leg_line in SUBWAY_LINES else []
    return _render(request, "_leg_stops.html", None, stops=stops)


@router.post("/transit/route")
async def transit_route_save(request: Request):
    """Chained route: ordered legs, each line + from + to."""
    from app.transit import gtfs
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        form = await request.form()
        if not security.check_csrf(guard, form.get("csrf")):
            return RedirectResponse("/admin/transit", status_code=303)
        name = (form.get("route_name") or "").strip()[:40] or "My route"
        legs = []
        for line, frm, to in zip(form.getlist("leg_line"), form.getlist("leg_from"),
                                 form.getlist("leg_to"), strict=False):
            if not (line in SUBWAY_LINES and frm and to and frm != to):
                continue                                  # skip incomplete rows
            names = {s["id"]: s["name"] for s in gtfs.stops_for(line)}
            if frm in names and to in names:
                legs.append({"line": line, "from_id": frm, "to_id": to,
                             "from_name": names[frm], "to_name": names[to]})
        if len(legs) >= 2:                                # a chain needs 2+ legs
            with conn:
                conn.execute(
                    "INSERT INTO commute_profile (name, mode, legs_json,"
                    " actively_monitored) VALUES (?, 'train', ?, 1)",
                    (name, json.dumps(legs)))
        return RedirectResponse("/admin/transit", status_code=303)
    finally:
        conn.close()


@router.post("/transit/route/{route_id}/delete")
async def transit_route_delete(request: Request, route_id: int,
                               csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            with conn:
                conn.execute("DELETE FROM commute_profile WHERE id=? AND mode='train'",
                             (route_id,))
        return RedirectResponse("/admin/transit", status_code=303)
    finally:
        conn.close()


@router.post("/transit/{monitor_id}/segment")
async def transit_segment(request: Request, monitor_id: int,
                          from_stop: str = Form(""), to_stop: str = Form(""),
                          csrf: str = Form(None)):
    from app.transit import gtfs
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        row = conn.execute("SELECT field FROM monitor WHERE id=? AND adapter='mta'",
                           (monitor_id,)).fetchone()
        if security.check_csrf(guard, csrf) and row:
            if not from_stop or not to_stop:            # clear the segment
                with conn:
                    conn.execute("UPDATE monitor SET config_json=NULL WHERE id=?",
                                 (monitor_id,))
            else:
                names = {s["id"]: s["name"] for s in gtfs.stops_for(row["field"])}
                if from_stop in names and to_stop in names and from_stop != to_stop:
                    cfg = json.dumps({
                        "from_id": from_stop, "to_id": to_stop,
                        "from_name": names[from_stop], "to_name": names[to_stop]})
                    with conn:
                        conn.execute("UPDATE monitor SET config_json=? WHERE id=?",
                                     (cfg, monitor_id))
        return RedirectResponse("/admin/transit", status_code=303)
    finally:
        conn.close()


@router.post("/transit")
async def transit_add(request: Request, line: str = Form(...),
                      csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf) and line in SUBWAY_LINES + RAIL_LINES:
            # duplicates allowed by design: two R monitors with different
            # segments are two different commutes
            with conn:
                conn.execute("INSERT INTO monitor (adapter, field, created_at)"
                             " VALUES ('mta', ?, ?)",
                             (line, datetime.now().isoformat(timespec="seconds")))
        return RedirectResponse("/admin/transit", status_code=303)
    finally:
        conn.close()


@router.post("/transit/{monitor_id}/delete")
async def transit_delete(request: Request, monitor_id: int, csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            with conn:
                conn.execute("DELETE FROM monitor WHERE id=? AND adapter='mta'",
                             (monitor_id,))
        return RedirectResponse("/admin/transit", status_code=303)
    finally:
        conn.close()


# ---------------------------------------------------------------- rule editor

def _editor_ctx(conn, rule_row=None, draft=None, problems=None):
    from app.rules import user as user_rules
    ctx = {"fields": user_rules.picker_fields(), "ops": user_rules.VALID_OPS,
           "bands": user_rules.BANDS, "problems": problems or [],
           "rule_id": rule_row["id"] if rule_row else None}
    if draft:                                   # failed save: re-show input
        ctx["draft"] = draft
    elif rule_row:                              # editing existing
        meta = json.loads(rule_row["window_json"] or "{}")
        wh = meta.get("window_hours") or ["", ""]
        band = next((k for k, (_, p) in user_rules.BANDS.items()
                     if p == rule_row["priority"]), "plan")
        season = next((k for k, m in user_rules.SEASONS.items()
                       if m == meta.get("months")), "all")
        ctx["draft"] = {"name": rule_row["name"], "output": rule_row["output_text"],
                        "band": band, "season": season, "notes": rule_row["notes"] or "",
                        "window_start": wh[0], "window_end": wh[1],
                        "conditions": json.loads(rule_row["condition_json"])}
    else:
        ctx["draft"] = {"name": "", "output": "", "band": "plan", "season": "all",
                        "notes": "", "window_start": "", "window_end": "",
                        "conditions": [{"field": "", "op": ">=", "value": ""}]}
    return ctx


async def _editor_form(request) -> dict:
    form = await request.form()
    return {"name": form.get("name"), "output": form.get("output"),
            "band": form.get("band"), "season": form.get("season"),
            "topic": form.get("topic"), "notes": form.get("notes") or "",
            "window_start": form.get("window_start"),
            "window_end": form.get("window_end"),
            "cfield": form.getlist("cfield"), "cop": form.getlist("cop"),
            "cvalue": form.getlist("cvalue"), "csrf": form.get("csrf"),
            "rule_id": form.get("rule_id")}


@router.get("/rules/new", response_class=HTMLResponse)
async def rule_new(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        return _render(request, "rule_editor.html", guard, **_editor_ctx(conn))
    finally:
        conn.close()


@router.get("/rules/user/{rule_id}/edit", response_class=HTMLResponse)
async def rule_edit(request: Request, rule_id: int):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        row = conn.execute("SELECT * FROM signal_rule WHERE id=? AND is_seed=0",
                           (rule_id,)).fetchone()
        if row is None:
            return RedirectResponse("/admin/rules", status_code=303)
        return _render(request, "rule_editor.html", guard,
                       **_editor_ctx(conn, rule_row=row))
    finally:
        conn.close()


@router.get("/rules/row", response_class=HTMLResponse)
async def rule_condition_row(request: Request):
    """htmx: one more empty condition row."""
    from app.rules import user as user_rules
    return _render(request, "_rule_row.html", None,
                   cond={"field": "", "op": ">=", "value": ""},
                   fields=user_rules.picker_fields(), ops=user_rules.VALID_OPS)


@router.post("/rules/preview", response_class=HTMLResponse)
async def rule_preview(request: Request):
    """htmx: evaluate the draft against the CURRENT snapshot. No side effects."""
    from app.display.service import get_live_fields
    from app.rules import user as user_rules
    from app.rules.engine import evaluate
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        form = await _editor_form(request)
        rule, problems = user_rules.validate_and_build(form)
        if problems:
            return _render(request, "_preview.html", guard, problems=problems)
        fields = get_live_fields(conn)
        if fields is None:
            return _render(request, "_preview.html", guard,
                           problems=["No weather data yet — preview needs one fetch."])
        rule["id"] = "PREVIEW"
        now = datetime.now()
        fired = evaluate(rule, fields, month=now.month, hour=now.hour)
        used = {c["field"]: fields.get(c["field"]) for c in rule["conditions"]}
        return _render(request, "_preview.html", guard, fired=fired, used=used,
                       windowed=bool(rule.get("window_hours")))
    finally:
        conn.close()


@router.post("/rules/save", response_class=HTMLResponse)
async def rule_save(request: Request):
    from app.rules import user as user_rules
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        form = await _editor_form(request)
        if not security.check_csrf(guard, form["csrf"]):
            return RedirectResponse("/admin/rules", status_code=303)
        rule, problems = user_rules.validate_and_build(form)
        if problems:                       # re-render with input preserved
            draft = {**form, "conditions": [
                {"field": f, "op": o, "value": v} for f, o, v in
                zip(form["cfield"], form["cop"], form["cvalue"], strict=False)]}
            return _render(request, "rule_editor.html", guard,
                           **_editor_ctx(conn, draft=draft, problems=problems))
        rid = int(form["rule_id"]) if form.get("rule_id") else None
        user_rules.save(conn, rule, notes=form["notes"], rule_id=rid)
        return RedirectResponse("/admin/rules", status_code=303)
    finally:
        conn.close()


@router.post("/rules/user/{rule_id}/toggle")
async def rule_user_toggle(request: Request, rule_id: int, csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            row = conn.execute("SELECT id FROM signal_rule WHERE id=? AND is_seed=0",
                               (rule_id,)).fetchone()
            if row:
                with conn:
                    conn.execute(
                        "INSERT INTO rule_user_state (rule_id, enabled,"
                        " acknowledged_new) VALUES (?, 0, 1)"
                        " ON CONFLICT(rule_id) DO UPDATE SET enabled = 1 - enabled",
                        (rule_id,))
        return RedirectResponse("/admin/rules", status_code=303)
    finally:
        conn.close()


@router.post("/rules/user/{rule_id}/delete")
async def rule_user_delete(request: Request, rule_id: int, csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            with conn:                     # user rules only — seeds are immortal
                conn.execute("DELETE FROM rule_user_state WHERE rule_id IN"
                             " (SELECT id FROM signal_rule WHERE id=? AND is_seed=0)",
                             (rule_id,))
                conn.execute("DELETE FROM signal_rule WHERE id=? AND is_seed=0",
                             (rule_id,))
        return RedirectResponse("/admin/rules", status_code=303)
    finally:
        conn.close()


# ---------------------------------------------------------------- rule toggles

@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        rows = conn.execute(
            "SELECT r.seed_id, r.output_text, r.priority, r.notes,"
            " COALESCE(us.enabled, 1) enabled, COALESCE(us.acknowledged_new, 1) acked"
            " FROM signal_rule r LEFT JOIN rule_user_state us ON us.rule_id = r.id"
            " WHERE r.is_seed=1 ORDER BY r.priority DESC").fetchall()
        user_rows = conn.execute(
            "SELECT r.id, r.name, r.output_text, COALESCE(us.enabled, 1) enabled"
            " FROM signal_rule r LEFT JOIN rule_user_state us ON us.rule_id = r.id"
            " WHERE r.is_seed=0 ORDER BY r.created_at DESC").fetchall()
        bands = {"Safety": [], "Time-sensitive": [], "Planning": [], "Ambient": []}
        for r in rows:
            band = ("Safety" if r["priority"] >= 90 else
                    "Time-sensitive" if r["priority"] >= 60 else
                    "Planning" if r["priority"] >= 30 else "Ambient")
            bands[band].append(r)
        return _render(request, "rules.html", guard, bands=bands,
                       user_rules=user_rows)
    finally:
        conn.close()


@router.post("/rules/{seed_id}/toggle")
async def toggle_rule(request: Request, seed_id: str, csrf: str = Form(None)):
    conn = db.connect()
    try:
        guard = _guard(request, conn)
        if isinstance(guard, RedirectResponse):
            return guard
        if security.check_csrf(guard, csrf):
            row = conn.execute("SELECT id FROM signal_rule WHERE seed_id=? AND is_seed=1",
                               (seed_id,)).fetchone()
            if row:
                with conn:
                    conn.execute(
                        "INSERT INTO rule_user_state (rule_id, enabled, acknowledged_new)"
                        " VALUES (?, 0, 1) ON CONFLICT(rule_id) DO UPDATE SET"
                        " enabled = 1 - enabled, acknowledged_new = 1", (row["id"],))
        return RedirectResponse("/admin/rules", status_code=303)
    finally:
        conn.close()
