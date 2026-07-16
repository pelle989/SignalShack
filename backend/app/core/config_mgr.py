"""Config apply/rollback — the safety rails ship in MVP, the approval UI
waits for V1.3 (architecture decision #8).

Pipeline: validate -> snapshot current as backup -> apply -> health check ->
rollback on failure. Boot always uses the latest 'applied' row.
"""

import json
import sqlite3

DEFAULTS = {
    "timezone": "America/New_York",
    "night_start": "22:00",
    "night_end": "05:30",
    "display_poll_seconds": 20,
    "announce_pin": "",      # empty = /announce page disabled
    "display_pin": "",       # empty = display open to the LAN (default)
}


def get_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT settings_json FROM config WHERE status='applied'"
                       " ORDER BY version DESC LIMIT 1").fetchone()
    merged = dict(DEFAULTS)
    if row:
        merged.update(json.loads(row["settings_json"]))
    return merged


def _validate(settings: dict) -> list[str]:
    problems = []
    for key in settings:
        if key not in DEFAULTS:
            problems.append(f"unknown setting: {key}")
    for key in ("night_start", "night_end"):
        v = settings.get(key, DEFAULTS[key])
        parts = str(v).split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts) \
                or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
            problems.append(f"{key}: not a valid HH:MM time")
    poll = settings.get("display_poll_seconds", 20)
    if not isinstance(poll, int) or not 10 <= poll <= 120:
        problems.append("display_poll_seconds: must be 10–120")
    for key in ("announce_pin", "display_pin"):
        pin = str(settings.get(key, "") or "")
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 8):
            problems.append(f"{key}: 4–8 digits, or blank to disable")
    return problems


def apply_settings(conn: sqlite3.Connection, new: dict,
                   health_check=None) -> tuple[bool, list[str]]:
    """-> (applied, problems). health_check: callable(conn) raising on failure
    (defaults to composing the board — display must still render)."""
    problems = _validate(new)
    if problems:
        return False, problems

    prev_version = (conn.execute("SELECT MAX(version) v FROM config").fetchone()["v"] or 0)
    with conn:
        conn.execute("INSERT INTO config (version, status, applied_at, settings_json)"
                     " VALUES (?, 'applied', datetime('now'), ?)",
                     (prev_version + 1, json.dumps(new)))
    try:
        if health_check is None:
            from app.display.service import compose_board
            compose_board(conn)          # must not raise; must render something
        else:
            health_check(conn)
    except Exception as exc:  # noqa: BLE001 — any failure means rollback
        with conn:            # AIDEV-CAUTION: rollback = mark bad, previous wins
            conn.execute("UPDATE config SET status='rolled_back',"
                         " health_check_result=? WHERE version=?",
                         (type(exc).__name__, prev_version + 1))
        return False, [f"health check failed ({type(exc).__name__}) — rolled back"]
    with conn:
        conn.execute("UPDATE config SET health_check_result='ok' WHERE version=?",
                     (prev_version + 1,))
    return True, []
