"""Snapshot store + freshness computation.

AIDEV-CAUTION: freshness is computed at READ time from fetched_at vs the
adapter manifest's TTLs — never stored stale, never silently fresh
(invariant 2). The unknown/unavailable distinction matters most for alerts:
"no snapshot" must never render as "no alerts".
"""

import json
import sqlite3
from datetime import datetime


def save(conn: sqlite3.Connection, location_id: int, source: str, payload: dict,
         now: datetime | None = None) -> None:
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO weather_snapshot (location_id, source, fetched_at, payload_json)"
            " VALUES (?, ?, ?, ?)",
            (location_id, source, ts, json.dumps(payload)))
        # bounded retention: keep last 30 per (location, source)
        conn.execute(
            "DELETE FROM weather_snapshot WHERE location_id=? AND source=? AND id NOT IN"
            " (SELECT id FROM weather_snapshot WHERE location_id=? AND source=?"
            "  ORDER BY fetched_at DESC LIMIT 30)",
            (location_id, source, location_id, source))


def latest(conn: sqlite3.Connection, location_id: int, source: str) -> dict | None:
    row = conn.execute(
        "SELECT fetched_at, payload_json FROM weather_snapshot"
        " WHERE location_id=? AND source=? ORDER BY fetched_at DESC LIMIT 1",
        (location_id, source)).fetchone()
    if not row:
        return None
    return {"fetched_at": row["fetched_at"], "payload": json.loads(row["payload_json"])}


def freshness(fetched_at: str | None, fresh_s: int, stale_s: int,
              now: datetime | None = None) -> str:
    """-> 'fresh' | 'stale' | 'unavailable' (no data at all)."""
    if fetched_at is None:
        return "unavailable"
    age = ((now or datetime.now()) - datetime.fromisoformat(fetched_at)).total_seconds()
    return "fresh" if age <= stale_s else "stale"


def stamp(fetched_at: str | None, state: str) -> str:
    if fetched_at is None:
        return "unavailable"
    t = datetime.fromisoformat(fetched_at).strftime("%-I:%M %p")
    return f"as of {t}" + ("" if state == "fresh" else f" · {state}")


# ---- kv helpers (season one-shots, fire caps)

def kv_get(conn: sqlite3.Connection, key: str, default):
    row = conn.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def kv_set(conn: sqlite3.Connection, key: str, value) -> None:
    with conn:
        conn.execute(
            "INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
            " updated_at=excluded.updated_at",
            (key, json.dumps(value), datetime.now().isoformat(timespec="seconds")))
