-- monitor gains per-monitor config (segment scoping for transit lines, and
-- future per-monitor options). Additive-only, per policy.
ALTER TABLE monitor ADD COLUMN config_json TEXT;
