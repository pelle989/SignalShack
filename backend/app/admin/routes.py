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
from app.core import backup, config_mgr, db, diagnostics, security, snapshots
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


# ---------------------------------------------------------------- setup

@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    conn = db.connect()
    try:
        if _device(conn):
            return RedirectResponse("/admin", status_code=303)
    finally:
        conn.close()
    return _render(request, "setup.html")


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, password: str = Form(...),
                       password2: str = Form(...), zipcode: str = Form(""),
                       latitude: str = Form(""), longitude: str = Form(""),
                       label: str = Form("Home"),
                       timezone: str = Form("America/New_York")):
    conn = db.connect()
    try:
        if _device(conn):
            return RedirectResponse("/admin", status_code=303)
        if password != password2 or len(password) < 8:
            return _render(request, "setup.html",
                           error="Passwords must match and be at least 8 characters.")
        if latitude and longitude:
            loc = {"lat": float(latitude), "lon": float(longitude),
                   "label": label, "tz": timezone}
        else:
            loc = await _geocode_zip(zipcode)
            if loc is None:
                return _render(request, "setup.html", zip_failed=True,
                               error="ZIP lookup failed — enter coordinates below.")
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "INSERT INTO device (id, first_boot_at, setup_completed_at, timezone,"
                " admin_password_hash) VALUES (1, ?, ?, ?, ?)",
                (now, now, loc["tz"], security.hash_password(password)))
            conn.execute(
                "INSERT INTO location (label, zip, latitude, longitude, is_primary)"
                " VALUES (?, ?, ?, ?, 1)",
                (loc["label"], zipcode or None, loc["lat"], loc["lon"]))
            conn.execute(
                "INSERT INTO board (name, is_default, layout_json) VALUES"
                " ('Default', 1, '{\"preset\": \"default\"}')")
        cookie, _ = security.make_session()
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(security.SESSION_COOKIE, cookie, httponly=True,
                        samesite="lax", max_age=security.SESSION_MAX_AGE)
        return resp
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
                       settings=config_mgr.get_settings(conn))
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
        bands = {"Safety": [], "Time-sensitive": [], "Planning": [], "Ambient": []}
        for r in rows:
            band = ("Safety" if r["priority"] >= 90 else
                    "Time-sensitive" if r["priority"] >= 60 else
                    "Planning" if r["priority"] >= 30 else "Ambient")
            bands[band].append(r)
        return _render(request, "rules.html", guard, bands=bands)
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
