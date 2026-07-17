"""User-created rules — the no-code editor's backend (plan §7.1a).

AIDEV-NOTE: user rules live in signal_rule (is_seed=0) and evaluate through
the SAME engine as seeds. Priority is capped below the safety band — a user
rule must never outrank a safety message.
"""

import json
import re
import sqlite3
from datetime import datetime

from app.adapters.open_meteo import OpenMeteoAdapter

# friendly labels for the manifest-driven field picker
FRIENDLY_FIELDS = {
    "high_today": "Today's high (°F)",
    "low_tonight": "Tonight's low (°F)",
    "temp_7am": "Temperature at 7 AM (°F)",
    "temp_evening": "Evening temperature (°F)",
    "heat_index_max": "Max heat index (°F)",
    "wind_chill_min_morning": "Morning wind chill (°F)",
    "temp_swing_today": "Day's temperature swing (°F)",
    "high_delta_vs_yesterday": "High vs yesterday (±°F)",
    "dew_max_today": "Max dewpoint today (°F)",
    "dew_evening_max": "Evening dewpoint (°F)",
    "dew_drop_vs_yesterday": "Dewpoint drop vs yesterday (°F)",
    "max_gust_today": "Max wind gust (mph)",
    "wind_dir_now": "Wind direction now (N, NE, E, SE, S, SW, W, NW)",
    "wind_evening_max": "Evening wind (mph)",
    "pop_today_max": "Rain chance today (%)",
    "pop_commute_max": "Rain chance 7–9 AM (%)",
    "pop_evening_max": "Rain chance evening (%)",
    "cloud_overnight_max": "Overnight cloud cover (%)",
    "uv_index_max": "Max UV index",
    "snow_24h_in": "Snow expected (inches)",
    "hot_streak_days": "Days in a row ≥85°",
    "consecutive_precip_days": "Rainy days in a row",
    "rain_within_4h": "Rain within 4 hours (true/false)",
    "raining_now": "Raining right now (true/false)",
    "storms_expected": "Thunderstorms expected (true/false)",
    "is_weekday": "Is a weekday (true/false)",
    "freeze_snap_onset": "First freeze after mild nights (true/false)",
    "all_day_soaker": "Rain most of the day (true/false)",
}

BANDS = {"time": ("Time-sensitive", 75), "plan": ("Planning", 45),
         "ambient": ("Ambient", 20)}
SEASONS = {"all": None, "warm": [5, 6, 7, 8, 9], "cold": [10, 11, 12, 1, 2, 3, 4]}
VALID_OPS = [">=", "<=", ">", "<", "==", "!=", "between"]
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-⛿]")


def picker_fields() -> list[dict]:
    published = set(OpenMeteoAdapter.manifest.fields)
    return [{"name": k, "label": v} for k, v in FRIENDLY_FIELDS.items()
            if k in published]


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return json.loads(raw)          # numbers, [a, b] for between
    except (ValueError, TypeError):
        return raw


def validate_and_build(form: dict) -> tuple[dict | None, list[str]]:
    """form: name, output, band, season, window_start, window_end,
    cfield[], cop[], cvalue[]  ->  (engine-ready rule dict, problems)."""
    problems = []
    name = (form.get("name") or "").strip()
    output = (form.get("output") or "").strip()
    if not 1 <= len(name) <= 60:
        problems.append("Name: 1–60 characters.")
    rendered = re.sub(r"\{[^}]+\}", "00", output)
    if not output or len(rendered) > 90:
        problems.append("Message: required, at most 90 characters as shown.")
    if "!" in output:
        problems.append("Message: no exclamation points (voice guide).")
    if _EMOJI.search(output):
        problems.append("Message: no emoji (voice guide).")
    if output and output[0].islower():
        output = output[0].upper() + output[1:]   # sentence case: fix, don't nag

    band = form.get("band") or "plan"
    if band not in BANDS:
        problems.append("Pick a priority band.")
    season = form.get("season") or "all"
    if season not in SEASONS:
        problems.append("Pick a season.")

    published = set(OpenMeteoAdapter.manifest.fields)
    conditions = []
    for field, op, raw in zip(form.get("cfield", []), form.get("cop", []),
                              form.get("cvalue", []), strict=False):
        if not field:
            continue
        if field not in published:
            problems.append(f"Unknown field: {field}")
            continue
        if op not in VALID_OPS:
            problems.append(f"Unknown comparison: {op}")
            continue
        value = _parse_value(raw)
        if op == "between" and not (isinstance(value, list) and len(value) == 2):
            problems.append("'between' needs two numbers like: [55, 70]")
            continue
        if field == "wind_dir_now" and isinstance(value, str):
            value = value.strip().upper()     # nw -> NW: fix, don't nag
        conditions.append({"field": field, "op": op, "value": value})
    if not conditions:
        problems.append("At least one condition is required.")

    window = None
    ws, we = form.get("window_start") or "", form.get("window_end") or ""
    if ws and we:
        window = [int(ws), int(we)]
        if not (0 <= window[0] < window[1] <= 24):
            problems.append("Window: start must be before end (0–24).")

    if problems:
        return None, problems
    rule = {"name": name, "output": output, "priority": BANDS[band][1],
            "topic": form.get("topic") or "custom",
            "conditions": conditions, "months": SEASONS[season],
            "window_hours": window}
    return rule, []


def save(conn: sqlite3.Connection, rule: dict, notes: str = "",
         rule_id: int | None = None) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    meta = json.dumps({"months": rule["months"], "window_hours": rule["window_hours"]})
    with conn:
        if rule_id:
            conn.execute(
                "UPDATE signal_rule SET name=?, condition_json=?, output_text=?,"
                " priority=?, topic=?, window_json=?, notes=?, updated_at=?"
                " WHERE id=? AND is_seed=0",
                (rule["name"], json.dumps(rule["conditions"]), rule["output"],
                 rule["priority"], rule["topic"], meta, notes, now, rule_id))
            return rule_id
        conn.execute(
            "INSERT INTO signal_rule (name, adapter, condition_json, output_text,"
            " priority, topic, window_json, notes, is_seed, created_at, updated_at)"
            " VALUES (?, 'open_meteo', ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (rule["name"], json.dumps(rule["conditions"]), rule["output"],
             rule["priority"], rule["topic"], meta, notes, now, now))
        rid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute("INSERT INTO rule_user_state (rule_id, enabled,"
                     " acknowledged_new) VALUES (?, 1, 1)", (rid,))
        return rid


def load_enabled(conn: sqlite3.Connection) -> list[dict]:
    """Engine-ready dicts for all enabled user rules."""
    rows = conn.execute(
        "SELECT r.id, r.name, r.priority, r.topic, r.condition_json,"
        " r.output_text, r.window_json FROM signal_rule r"
        " LEFT JOIN rule_user_state us ON us.rule_id = r.id"
        " WHERE r.is_seed=0 AND COALESCE(us.enabled, 1)=1").fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["window_json"] or "{}")
        out.append({"id": f"U{r['id']}", "priority": r["priority"],
                    "topic": r["topic"] or "custom",
                    "conditions": json.loads(r["condition_json"]),
                    "output": r["output_text"],
                    "months": meta.get("months"),
                    "window_hours": meta.get("window_hours")})
    return out
