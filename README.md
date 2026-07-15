# SignalShack

**The local-first home display that turns open data into household meaning.**

A small box on your network serves a calm, glanceable board to any TV or
browser: not "Precip 40%, wind W 13 mph" but *"Rain likely around 8:30 —
bring an umbrella."* Weather meaning, NWS alerts, family announcements —
no cloud, no account, no subscription, no microphone, no ads.

If SignalShack-the-project disappeared tomorrow, your box would keep working
exactly as it does today. That's the point.

## Status

Pre-release (V1.0). Message quality is being validated in a live 14-day run;
the appliance software is complete with 49 passing tests. Not yet accepting
external users — watch this repo.

## Run it (development)

```bash
cd backend
uv sync
SIGNALSHACK_LAT=40.68 SIGNALSHACK_LON=-73.47 uv run uvicorn app.main:app
# → http://localhost:8000/display   admin at /admin (wizard on first visit)
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). One vendored file:
`curl -L -o backend/app/static/htmx.min.js https://unpkg.com/htmx.org@2/dist/htmx.min.js`

## Run it (appliance)

Fresh Ubuntu Server 24.04 box on your LAN:

```bash
cd ansible
cp inventory.example.yml inventory.yml   # set your box's IP + user
ansible-playbook -i inventory.yml playbook.yml
# → http://signalshack.local/display  (IP fallback always works)
```

Set BIOS **"Restore on AC Power Loss → Power On"** — it's how the display
survives outages unattended.

## Design commitments

- **Local-first, forever.** Display works with no internet (cached, labeled).
  Nothing listens inbound. No telemetry, no analytics.
- **Meaning over data.** Rules turn feeds into ≤90-character actionable
  sentences with a tested voice. Silence is a feature.
- **Honest states.** Every card is loading/empty/fresh/stale/unavailable/
  degraded — labeled, never blank, never silently stale.
- **Updates never silently change what a household sees.** New rules and
  cards arrive dormant; your toggles always win (machine-enforced).
- **The LAN is not a trust boundary.** Argon2id, CSRF, host-guard,
  rate-limited login, secrets encrypted and never logged (tested).

## Project layout

- `backend/` — FastAPI modular monolith, Jinja+htmx UI, SQLite. See `CLAUDE.md`
  for module map, invariants, and conventions.
- `backend/app/rules/seeds.json` — the seed rule library (backtest-tuned).
- `ansible/` — appliance provisioning.

## License

AGPL-3.0. The core is open — sell hardware and convenience, never lock-in.
