-- SignalShack schema v1 — entities per signalshack-full-plan.md §9.
-- AIDEV-CAUTION: additive-only from here; user state (toggles/overrides)
-- lives apart from seed definitions so updates never clobber it.

CREATE TABLE device (
    id INTEGER PRIMARY KEY CHECK (id = 1),          -- singleton
    serial_number TEXT,
    hardware_model TEXT,
    first_boot_at TEXT,
    setup_completed_at TEXT,
    timezone TEXT,
    admin_password_hash TEXT,
    dek_wrapped BLOB,                               -- envelope encryption (V1.1)
    schema_note TEXT
);

CREATE TABLE location (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    zip TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    nws_grid_id TEXT, nws_grid_x INTEGER, nws_grid_y INTEGER,
    is_primary INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE board (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE,                               -- NULL slug = default board
    is_default INTEGER NOT NULL DEFAULT 0,
    layout_json TEXT NOT NULL,                      -- ordered card instances, sizes, sort modes
    daypart_json TEXT,                              -- morning/evening variants (V1.1)
    last_viewed_at TEXT                             -- demand-driven polling presence
);

CREATE TABLE config (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'applied',         -- applied | rolled_back
    applied_at TEXT,
    settings_json TEXT NOT NULL,
    health_check_result TEXT
);

CREATE TABLE signal_rule (
    id INTEGER PRIMARY KEY,
    seed_id TEXT UNIQUE,                            -- immutable seed identity (e.g. 'T4'); NULL = user rule
    seed_version INTEGER,
    name TEXT NOT NULL,
    adapter TEXT NOT NULL,                          -- (adapter, entity, field) triples
    entity_id INTEGER,
    condition_json TEXT NOT NULL,
    output_text TEXT NOT NULL,
    priority INTEGER NOT NULL,
    topic TEXT,
    window_json TEXT,
    notes TEXT,                                     -- private admin self-documentation
    is_seed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT, updated_at TEXT
);

-- user state kept apart from definitions (update-survival invariant)
CREATE TABLE rule_user_state (
    rule_id INTEGER PRIMARY KEY REFERENCES signal_rule(id),
    enabled INTEGER NOT NULL DEFAULT 1,
    priority_override INTEGER,
    acknowledged_new INTEGER NOT NULL DEFAULT 0     -- "new" badge cleared?
);

CREATE TABLE announcement (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    person_label TEXT,                              -- per-person tagging (pending approval)
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE weather_snapshot (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES location(id),
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    observed_at TEXT,
    payload_json TEXT NOT NULL,
    freshness_state TEXT NOT NULL DEFAULT 'fresh',
    error_state TEXT
);
CREATE INDEX idx_wsnap ON weather_snapshot (location_id, source, fetched_at DESC);

CREATE TABLE alert_snapshot (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES location(id),
    alert_id TEXT,
    event TEXT, severity TEXT, urgency TEXT, headline TEXT,
    starts_at TEXT, ends_at TEXT,
    fetched_at TEXT NOT NULL,
    raw_json TEXT,
    freshness_state TEXT NOT NULL DEFAULT 'fresh'
);

CREATE TABLE commute_profile (                      -- schema reserved; built V1.2
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('train','bus','drive')),
    legs_json TEXT,                                 -- transit: ordered legs
    route_json TEXT,                                -- drive: waypoints + prefs
    target_arrival TEXT,                            -- leave-by countdown
    actively_monitored INTEGER NOT NULL DEFAULT 0   -- drive-route free-tier cap
);

CREATE TABLE monitor (                              -- trend meaning / allergy profile
    id INTEGER PRIMARY KEY,
    adapter TEXT NOT NULL,
    field TEXT NOT NULL,                            -- e.g. pollen species
    location_id INTEGER REFERENCES location(id),
    person_label TEXT,
    created_at TEXT
);

CREATE TABLE daily_aggregate (                      -- streaks / trend sentences
    id INTEGER PRIMARY KEY,
    location_id INTEGER REFERENCES location(id),
    date TEXT NOT NULL,
    field TEXT NOT NULL,
    value REAL,
    UNIQUE (location_id, date, field)
);

CREATE TABLE secret (
    id INTEGER PRIMARY KEY,
    service_name TEXT NOT NULL,
    label TEXT,
    ciphertext BLOB NOT NULL,
    nonce BLOB NOT NULL,
    created_at TEXT, last_used_at TEXT
);

CREATE TABLE source_registry (
    source_name TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    license_type TEXT NOT NULL,
    commercial_use_allowed INTEGER,
    redistribution_allowed INTEGER,
    bulk_storage_allowed INTEGER,
    cache_allowed INTEGER,
    attribution_required INTEGER,
    attribution_text TEXT,
    api_key_required INTEGER NOT NULL DEFAULT 0,
    rate_limit TEXT,
    terms_url TEXT,
    last_reviewed_date TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE event_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    service TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT                               -- allowlisted fields only
);

CREATE TABLE usage_stat (                           -- local-only, never leaves the box
    id INTEGER PRIMARY KEY,
    board_id INTEGER REFERENCES board(id),
    hour TEXT NOT NULL,                             -- YYYY-MM-DDTHH
    request_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (board_id, hour)
);

CREATE TABLE self_report (                          -- gate evidence
    id INTEGER PRIMARY KEY,
    date TEXT UNIQUE NOT NULL,
    useful INTEGER,
    note TEXT
);

CREATE TABLE backup_bundle (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    bytes_size INTEGER,
    file_path TEXT,
    manifest_json TEXT
);
