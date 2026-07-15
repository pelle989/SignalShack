-- kv: small JSON state (season one-shots, weekly fire caps). Additive-only.
CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT
);
