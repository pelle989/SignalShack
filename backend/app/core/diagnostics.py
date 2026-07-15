"""Self-diagnose + diagnostic bundle (support-without-telemetry design).

Bundle contents are an ALLOWLIST (invariant 5): versions, migrations, source
freshness, scrubbed recent events, self-check results. Never: secrets,
announcements text, credentials, raw payloads.
"""

import io
import json
import platform
import shutil
import sqlite3
import zipfile
from datetime import datetime

from app.adapters.nws import NWSAdapter
from app.adapters.open_meteo import OpenMeteoAdapter
from app.core import db, snapshots

APP_VERSION = "0.1.0"
ADAPTERS = (OpenMeteoAdapter(), NWSAdapter())


def self_check(conn: sqlite3.Connection) -> list[dict]:
    """Plain-language checks shown to the user BEFORE any support contact."""
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    loc = conn.execute("SELECT * FROM location WHERE is_primary=1").fetchone()
    add("Location configured", loc is not None,
        "Set up complete." if loc else "Run the setup wizard at /admin/setup.")

    for adapter in ADAPTERS:
        m = adapter.manifest
        snap = loc and snapshots.latest(conn, loc["id"], m.name)
        state = snapshots.freshness(snap and snap["fetched_at"],
                                    m.poll_seconds_fresh, m.stale_after_seconds)
        add(f"{m.name} data", state == "fresh",
            f"{state} — {snapshots.stamp(snap and snap['fetched_at'], state)}"
            + ("" if state == "fresh" else ". If this persists, check your internet"
               " connection, then try Refresh Now."))

    try:
        free_gb = shutil.disk_usage(db.DB_PATH.parent).free / 1e9
        add("Disk space", free_gb > 1, f"{free_gb:.1f} GB free")
    except OSError:
        add("Disk space", False, "could not check")

    try:
        with conn:
            conn.execute("INSERT INTO event_log (ts, level, service, event_type)"
                         " VALUES (?, 'info', 'diagnostics', 'self_check')",
                         (datetime.now().isoformat(timespec="seconds"),))
        add("Database writable", True, "ok")
    except sqlite3.Error as e:
        add("Database writable", False, type(e).__name__)

    fails = conn.execute(
        "SELECT COUNT(*) c FROM event_log WHERE event_type='fetch_failed'"
        " AND ts > datetime('now', '-1 day')").fetchone()["c"]
    add("Fetch errors (24h)", fails < 10,
        f"{fails} failed fetches" + (" — providers may be having trouble;"
                                     " cached data stays on display." if fails else ""))
    return checks


def build_bundle(conn: sqlite3.Connection) -> bytes:
    """Scrubbed zip the user downloads and emails. Allowlisted contents only."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about.json", json.dumps({
            "app_version": APP_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": conn.execute(
                "SELECT MAX(version) v FROM schema_version").fetchone()["v"],
        }, indent=1))
        z.writestr("self_check.json", json.dumps(self_check(conn), indent=1))
        events = [dict(r) for r in conn.execute(
            "SELECT ts, level, service, event_type FROM event_log"
            " ORDER BY ts DESC LIMIT 200").fetchall()]      # payload_json excluded
        z.writestr("events.json", json.dumps(events, indent=1))
        loc = conn.execute("SELECT id FROM location WHERE is_primary=1").fetchone()
        freshness = []
        for adapter in ADAPTERS:
            snap = loc and snapshots.latest(conn, loc["id"], adapter.manifest.name)
            freshness.append({"source": adapter.manifest.name,
                              "fetched_at": snap and snap["fetched_at"]})
        z.writestr("sources.json", json.dumps(freshness, indent=1))
        z.writestr("README.txt",
                   "SignalShack diagnostic bundle.\n"
                   "Contains: app/platform versions, self-check results, source\n"
                   "freshness, and event types (no payloads).\n"
                   "Does NOT contain: secrets, announcements, location coordinates\n"
                   "beyond what you email us, or any personal data.\n")
    return buf.getvalue()
