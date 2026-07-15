"""SQLite (WAL) + forward-only migrations.

AIDEV-CAUTION: migrations run once at boot, in order, inside a transaction
each; the config backup/rollback invariant depends on schema_version being
accurate. Never edit a shipped migration — add a new one.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "signalshack.db"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def migrate() -> int:
    conn = connect()
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()
    current = row["v"] or 0
    applied = 0
    for path in sorted(MIGRATIONS.glob("*.sql")):
        num = int(path.name.split("_")[0])
        if num <= current:
            continue
        with conn:  # one transaction per migration
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (num,))
        applied += 1
    conn.close()
    return applied
