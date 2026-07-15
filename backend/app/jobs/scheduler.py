"""Polling loop — plain asyncio, no queue, no broker (architecture decision:
in-process jobs).

Demand-driven polling (plan §8): forecast sources poll on cycle only while a
board has a recent viewer; NWS alerts NEVER sleep (safety surface — relaxed
cadence when idle, never stopped). Wake refresh: the display route calls
refresh_if_stale() on request after idle.
"""

import asyncio
from datetime import datetime

from app.adapters.airnow import AirNowAdapter
from app.adapters.mta import MTAAdapter
from app.adapters.nws import NWSAdapter
from app.adapters.open_meteo import OpenMeteoAdapter
from app.core import db, secrets, snapshots

VIEWER_WINDOW_S = 15 * 60
IDLE_ALERT_POLL_S = 300           # alerts when nobody watching: 5 min, never off
_last_fetch: dict[tuple, datetime] = {}


def _viewer_recent(conn) -> bool:
    row = conn.execute("SELECT MAX(last_viewed_at) v FROM board").fetchone()
    if not row or not row["v"]:
        return False
    return (datetime.now() - datetime.fromisoformat(row["v"])).total_seconds() < VIEWER_WINDOW_S


async def _fetch_and_store(conn, adapter, loc) -> None:
    key = (adapter.manifest.name, loc["id"])
    try:
        raw = await adapter.fetch({"latitude": loc["latitude"],
                                   "longitude": loc["longitude"]})
        snapshots.save(conn, loc["id"], adapter.manifest.name, raw)
        _last_fetch[key] = datetime.now()
    except Exception as exc:  # noqa: BLE001 — provider failures must never crash the loop
        conn.execute(
            "INSERT INTO event_log (ts, level, service, event_type, payload_json)"
            " VALUES (?, 'warn', ?, 'fetch_failed', ?)",
            (datetime.now().isoformat(timespec="seconds"),
             adapter.manifest.name, f'{{"error": "{type(exc).__name__}"}}'))
        conn.commit()


def _due(adapter, loc, engaged: bool) -> bool:
    key = (adapter.manifest.name, loc["id"])
    last = _last_fetch.get(key)
    if adapter.manifest.name == "nws":          # SAFETY: never sleeps
        interval = adapter.manifest.poll_seconds_fresh if engaged else IDLE_ALERT_POLL_S
    else:
        if not engaged:
            return False                        # dormant until wake refresh
        interval = adapter.manifest.poll_seconds_fresh
    return last is None or (datetime.now() - last).total_seconds() >= interval


async def refresh_if_stale(loc_id: int) -> None:
    """Wake refresh — fired by the display route after idle. Best-effort."""
    conn = db.connect()
    try:
        loc = conn.execute("SELECT * FROM location WHERE id=?", (loc_id,)).fetchone()
        if not loc:
            return
        for adapter in _active_adapters(conn):
            snap = snapshots.latest(conn, loc_id, adapter.manifest.name)
            state = snapshots.freshness(snap and snap["fetched_at"],
                                        adapter.manifest.poll_seconds_fresh,
                                        adapter.manifest.stale_after_seconds)
            if state != "fresh":
                await _fetch_and_store(conn, adapter, loc)
    finally:
        conn.close()


def _active_adapters(conn) -> tuple:
    """Keyed/optional adapters poll only when the household enabled them."""
    adapters = [OpenMeteoAdapter(), NWSAdapter()]
    if conn.execute("SELECT 1 FROM monitor WHERE adapter='mta' LIMIT 1").fetchone():
        adapters.append(MTAAdapter())
    key = secrets.retrieve(conn, "airnow") if secrets.exists(conn, "airnow") else None
    if key:
        adapters.append(AirNowAdapter(api_key=key))
    return tuple(adapters)


async def poll_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        conn = db.connect()
        try:
            engaged = _viewer_recent(conn)
            for loc in conn.execute("SELECT * FROM location"
                                    " WHERE is_primary=1").fetchall():
                for adapter in _active_adapters(conn):
                    if _due(adapter, loc, engaged):
                        await _fetch_and_store(conn, adapter, loc)
        finally:
            conn.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except TimeoutError:
            pass
