"""SignalShack — FastAPI modular monolith entry point.

AIDEV-NOTE: server renders HTML (Jinja), htmx polls fragments. The display
must always render *something* labeled — see CLAUDE.md invariant 2.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.admin.routes import router as admin_router
from app.core import db, security
from app.display.service import compose_board
from app.jobs import scheduler
from app.rules.sync import sync_seeds


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.migrate()
    conn = db.connect()
    try:
        sync_seeds(conn)
    finally:
        conn.close()
    _bootstrap_dev_location()
    stop = asyncio.Event()
    task = None
    if os.environ.get("SIGNALSHACK_NO_JOBS") != "1":      # tests set this
        task = asyncio.create_task(scheduler.poll_loop(stop))
    yield
    stop.set()
    if task:
        await task


def _bootstrap_dev_location() -> None:
    """Until the setup wizard exists: seed a primary location from env.
    SIGNALSHACK_LAT / SIGNALSHACK_LON / SIGNALSHACK_LABEL."""
    lat, lon = os.environ.get("SIGNALSHACK_LAT"), os.environ.get("SIGNALSHACK_LON")
    if not (lat and lon):
        return
    conn = db.connect()
    try:
        if not conn.execute("SELECT 1 FROM location WHERE is_primary=1").fetchone():
            with conn:
                conn.execute(
                    "INSERT INTO location (label, latitude, longitude, is_primary)"
                    " VALUES (?, ?, ?, 1)",
                    (os.environ.get("SIGNALSHACK_LABEL", "Home"), float(lat), float(lon)))
        if not conn.execute("SELECT 1 FROM board WHERE is_default=1").fetchone():
            with conn:   # board row powers viewer-presence polling + usage stats
                conn.execute("INSERT INTO board (name, is_default, layout_json)"
                             " VALUES ('Default', 1, '{\"preset\": \"default\"}')")
    finally:
        conn.close()


app = FastAPI(title="SignalShack", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def host_guard(request: Request, call_next):
    """DNS-rebinding defense (invariant 4) + baseline security headers."""
    if request.url.path.startswith("/admin") and not security.host_allowed(
            request, os.environ.get("SIGNALSHACK_HOST")):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Forbidden host", status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/admin"):
        response.headers["X-Frame-Options"] = "DENY"   # admin never framed
    return response


app.include_router(admin_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Jinja autoescape is on by default here — AIDEV-CAUTION: never disable it;
# announcement text is user input rendered on the display (invariant 4).


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ts": datetime.now().isoformat(timespec="seconds")}


@app.get("/display", response_class=HTMLResponse)
def display(request: Request):
    return templates.TemplateResponse(request, "display.html", {})


@app.get("/fragments/board", response_class=HTMLResponse)
async def board_fragment(request: Request):
    """htmx polls this every ~20s. The request itself is the presence signal
    (demand-driven polling) and the wake-refresh trigger.
    AIDEV-CAUTION: must stay `async def` — the wake-refresh create_task needs
    the running event loop; a sync route runs in a threadpool without one."""
    conn = db.connect()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "UPDATE board SET last_viewed_at=? WHERE is_default=1 OR slug IS NULL", (now,))
            hour = now[:13]
            conn.execute(
                "INSERT INTO usage_stat (board_id, hour, request_count) VALUES (NULL, ?, 1)"
                " ON CONFLICT(board_id, hour) DO UPDATE SET request_count=request_count+1",
                (hour,))
        ctx = compose_board(conn)
        loc = conn.execute("SELECT id FROM location WHERE is_primary=1").fetchone()
    finally:
        conn.close()
    if loc and os.environ.get("SIGNALSHACK_NO_JOBS") != "1":
        # wake refresh: fire-and-forget; page renders from cache immediately
        asyncio.create_task(scheduler.refresh_if_stale(loc["id"]))
    return templates.TemplateResponse(request, "_board.html", ctx)
