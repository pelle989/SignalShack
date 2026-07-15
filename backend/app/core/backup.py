"""Backup export — plan §17: config, boards, locations, rules + user state.

AIDEV-CAUTION (invariant 5): secrets and logs are NEVER in the bundle;
announcements are excluded by default (plan §11 of architecture). The test
suite plants a fake secret and asserts absence.
"""

import sqlite3
from datetime import datetime


def export_bundle(conn: sqlite3.Connection) -> dict:
    def rows(sql, params=()):
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    device = conn.execute(
        "SELECT timezone, hardware_model FROM device WHERE id=1").fetchone()
    return {
        "kind": "signalshack-backup",
        "schema_version": conn.execute(
            "SELECT MAX(version) v FROM schema_version").fetchone()["v"],
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "device": dict(device) if device else None,          # non-secret fields only
        "locations": rows("SELECT label, zip, latitude, longitude, is_primary,"
                          " nws_grid_id, nws_grid_x, nws_grid_y FROM location"),
        "boards": rows("SELECT name, slug, is_default, layout_json, daypart_json"
                       " FROM board"),
        "user_rules": rows("SELECT name, adapter, condition_json, output_text,"
                           " priority, topic, window_json, notes FROM signal_rule"
                           " WHERE is_seed=0"),
        "rule_state": rows("SELECT r.seed_id, us.enabled, us.priority_override"
                           " FROM rule_user_state us JOIN signal_rule r"
                           " ON r.id=us.rule_id WHERE r.seed_id IS NOT NULL"),
        "settings": rows("SELECT settings_json FROM config WHERE status='applied'"
                         " ORDER BY version DESC LIMIT 1"),
        # excluded by design: secret, event_log, announcement, usage_stat
    }
