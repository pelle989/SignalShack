# SignalShack — Conventions & Invariants

Local-first home signal appliance. FastAPI modular monolith + Jinja/htmx + SQLite.
Full product authority: `../signalshack-full-plan.md`. Architecture rationale:
`../architecture-decisions.md`. Rule spec: `../seed-rules.md`. Voice: `../copy-voice.md`.

## Invariants (violating any of these is a bug, whatever the tests say)

1. **An update never silently changes what a household sees.** New cards, rules,
   and seed-rule revisions arrive dormant/badged; user state always wins.
2. **No blank cards, no silent staleness.** Every card renders one of:
   loading / empty / fresh / stale / unavailable / degraded — always labeled.
3. **The display works with no internet** — cached data + stale labels, forever.
4. **The LAN is not a trust boundary.** CSRF on every mutating admin route;
   Host-header validation; rendered user input is always escaped.
5. **Secrets never appear in logs, exports, or backups.** Log fields are
   allowlisted; tests submit fake secrets and assert absence.
6. **Rules only fire on data ≤48h out.** Outlook data is informational only.
7. **Nothing listens inbound from the internet. Ever.**
8. **Meaning before numbers; sentences, not charts** (server-drawn SVG strips
   are the sanctioned exception). Copy obeys `copy-voice.md` hard rules.

## Module map (backend/app/)

- `core/` — db (SQLite WAL + migrations), config, security (argon2, sessions, CSRF)
- `adapters/` — data sources; each implements the contract in `adapters/base.py`
  and MUST have a complete source-registry record (CI enforces)
- `rules/` — engine: evaluates (adapter, entity, field) triples; bands; dedupe
- `display/` — board composition + Jinja/htmx routes (`/display`, `/display/<slug>`)
- `admin/` — auth, wizard, announcements, rule toggles, layout, status, backup
- `jobs/` — in-process schedulers: fetch, freshness, cleanup, health

## Conventions

- **Anchor comments:** `AIDEV-NOTE:` (orientation), `AIDEV-CAUTION:` (invariant-
  bearing — read before touching), `SAFETY:` (safety-display logic).
- Python 3.12, `uv` for deps, `ruff` for lint/format. Type hints everywhere.
- Server renders HTML; htmx swaps fragments; client JS is ~zero (clock,
  visibility-pause). No frontend build step, no CDN, no icon fonts.
- Every acceptance-matrix row (plan §17) becomes a test where automatable.
- Migrations are forward-only, numbered, run once at boot; `schema_version`
  gates them. Config schema changes are additive-only.

## Commands

```bash
uv sync                                   # install
uv run uvicorn app.main:app --reload      # dev server → http://localhost:8000/display
uv run pytest                             # tests
uv run ruff check && uv run ruff format   # lint/format
docker compose up -d                      # containerized
```

## License

AGPL-3.0. Add the canonical LICENSE text when creating the GitHub repo
(GitHub's license picker) — before the first public push, not after.
