"""Seed rule sync — implements the update-survival invariant.

AIDEV-CAUTION (invariant 1): updates replace seed DEFINITIONS, never user
state. First install: seeds arrive enabled (they ARE the product). Later
updates: brand-new seed ids arrive DISABLED with the 'new' badge; version
bumps update the definition while rule_user_state is untouched.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SEEDS_PATH = Path(__file__).parent / "seeds.json"


def sync_seeds(conn: sqlite3.Connection) -> dict:
    data = json.loads(SEEDS_PATH.read_text())
    version, added, updated = data["seed_version"], 0, 0
    first_install = conn.execute(
        "SELECT COUNT(*) c FROM signal_rule WHERE is_seed=1").fetchone()["c"] == 0
    now = datetime.now().isoformat(timespec="seconds")

    for r in data["rules"]:
        row = conn.execute("SELECT id, seed_version FROM signal_rule WHERE seed_id=?",
                           (r["id"],)).fetchone()
        payload = (r["id"], version, r["id"], "open_meteo",
                   json.dumps(r["conditions"]), r["output"], r["priority"],
                   r.get("topic"), json.dumps(r.get("months")), now)
        if row is None:
            with conn:
                conn.execute(
                    "INSERT INTO signal_rule (seed_id, seed_version, name, adapter,"
                    " condition_json, output_text, priority, topic, window_json,"
                    " updated_at, is_seed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                    payload + (now,))
                rid = conn.execute("SELECT id FROM signal_rule WHERE seed_id=?",
                                   (r["id"],)).fetchone()["id"]
                conn.execute(
                    "INSERT INTO rule_user_state (rule_id, enabled, acknowledged_new)"
                    " VALUES (?, ?, ?)",
                    (rid, 1 if first_install else 0, 1 if first_install else 0))
            added += 1
        elif row["seed_version"] != version:
            with conn:   # definition refresh only — user state untouched
                conn.execute(
                    "UPDATE signal_rule SET seed_version=?, condition_json=?,"
                    " output_text=?, priority=?, topic=?, window_json=?, updated_at=?"
                    " WHERE seed_id=?",
                    (version, json.dumps(r["conditions"]), r["output"], r["priority"],
                     r.get("topic"), json.dumps(r.get("months")), now, r["id"]))
            updated += 1
    return {"added": added, "updated": updated, "first_install": first_install}
