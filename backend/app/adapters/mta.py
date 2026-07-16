"""MTA adapter — C1 Line Status (service alerts, JSON feeds, no API key).

Covers subway, LIRR, and Metro-North service alerts. Arrival times (C2
leg-based routes) need the protobuf GTFS-RT feeds and come later; line
*status* is fully served by the alerts feeds.

AIDEV-NOTE: feed structure is GTFS-RT ServiceAlert JSON with MTA's Mercury
extension carrying a human alert_type ("Delays", "Planned Work", ...). Parse
defensively — verify live before beta (sandbox cannot reach the endpoint).
"""

import httpx

from app.adapters.base import Adapter, AdapterManifest

FEEDS = {
    "subway": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json",
    "lirr": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Flirr-alerts.json",
    "mnr": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fmnr-alerts.json",
}

# official line colors (bullets); yellow lines need dark text
LINE_COLORS = {
    "1": "#ee352e", "2": "#ee352e", "3": "#ee352e",
    "4": "#00933c", "5": "#00933c", "6": "#00933c", "7": "#b933ad",
    "A": "#0039a6", "C": "#0039a6", "E": "#0039a6",
    "B": "#ff6319", "D": "#ff6319", "F": "#ff6319", "M": "#ff6319",
    "G": "#6cbe45", "J": "#996633", "Z": "#996633", "L": "#a7a9ac",
    "N": "#fccc0a", "Q": "#fccc0a", "R": "#fccc0a", "W": "#fccc0a",
    "S": "#808183", "SI": "#2850ad",
    "LIRR": "#0f61a9", "MNR": "#0f61a9",
}
DARK_TEXT = {"N", "Q", "R", "W"}

# statuses ranked worst-first for card attention
STATUS_RANK = ["suspended", "service_change", "delays", "planned_work", "good"]


def _classify(alert_type: str) -> str:
    t = (alert_type or "").lower()
    if "suspend" in t or "no service" in t:
        return "suspended"
    if "delay" in t:
        return "delays"
    if "planned" in t or "maintenance" in t:
        return "planned_work"
    if t:
        return "service_change"
    return "service_change"


class MTAAdapter(Adapter):
    manifest = AdapterManifest(
        name="mta",
        version="1.0",
        entity_kind="transit_line",
        fields=["line_status", "line_delayed", "line_headline"],
        poll_seconds_fresh=60,
        stale_after_seconds=300,
        api_key_required=False,
        card_templates=["line_status"],
        registry_record={
            "source_name": "mta",
            "source_url": "https://api.mta.info",
            "license_type": "MTA developer data terms (free, no key since 2023)",
            "commercial_use_allowed": 1,
            "redistribution_allowed": 1,
            "bulk_storage_allowed": 1,
            "cache_allowed": 1,
            "attribution_required": 1,
            "attribution_text": "MTA",
            "api_key_required": 0,
            "rate_limit": "unpublished; poll politely (60s engaged / dormant idle)",
            "terms_url": "https://api.mta.info/#/DataFeedAgreement",
            "last_reviewed_date": "2026-07-15",
            "notes": "Alerts JSON feeds for C1 line status; protobuf GTFS-RT"
                     " trip updates deferred to C2.",
        },
    )

    async def fetch(self, entity=None) -> dict:
        """Fetch all three alert feeds; tolerate individual failures."""
        out = {"feeds": {}, "errors": []}
        async with httpx.AsyncClient(timeout=20) as client:
            for name, url in FEEDS.items():
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    out["feeds"][name] = r.json()
                except Exception as exc:  # noqa: BLE001 — per-feed isolation
                    out["errors"].append(f"{name}: {type(exc).__name__}")
        return out

    def normalize(self, raw: dict) -> dict:
        """-> {"alerts": {line_id: [{status, headline, stops}]}}. Each entry
        keeps its informed stop ids (parent form) so segment scoping can
        filter; stops == [] means line-wide."""
        from app.transit.gtfs import parent
        per_line: dict[str, list[dict]] = {}

        def add(key, entry):
            per_line.setdefault(key, []).append(entry)

        for feed_name, feed in raw.get("feeds", {}).items():
            for entity in feed.get("entity", []):
                alert = entity.get("alert") or {}
                mercury = alert.get("transit_realtime.mercury_alert") or {}
                status = _classify(mercury.get("alert_type", ""))
                headline = ""
                translations = (alert.get("header_text") or {}).get("translation", [])
                if translations:
                    headline = translations[0].get("text", "")
                routes, stops = set(), []
                for informed in alert.get("informed_entity", []):
                    if informed.get("route_id"):
                        routes.add(informed["route_id"])
                    if informed.get("stop_id"):
                        stops.append(parent(informed["stop_id"]))
                entry = {"status": status, "headline": headline[:140],
                         "stops": sorted(set(stops))}
                for route in routes:
                    add(route, entry)
                # rail branches roll up to a feed-level pseudo-line for C1
                if feed_name == "lirr":
                    add("LIRR", entry)
                elif feed_name == "mnr":
                    add("MNR", entry)
        return {"alerts": per_line, "errors": raw.get("errors", [])}


def line_view(line_id: str, normalized: dict,
              segment: set[str] | None = None) -> dict:
    """Card-ready view for one monitored line. segment: parent stop ids the
    household rides — alerts scoped entirely OUTSIDE it are suppressed;
    line-wide alerts (no stop ids) always count."""
    entries = normalized.get("alerts", {}).get(line_id, [])
    relevant = [e for e in entries
                if not e["stops"] or segment is None
                or set(e["stops"]) & segment]
    worst = min(relevant, key=lambda e: STATUS_RANK.index(e["status"]),
                default=None)
    status = worst["status"] if worst else "good"
    return {
        "line": line_id,
        "status": status,
        "headline": worst["headline"] if worst else "",
        "color": LINE_COLORS.get(line_id, "#5c6878"),
        "dark_text": line_id in DARK_TEXT,
        "label": {"good": "✓ Good service", "delays": "Delays",
                  "service_change": "Service change", "planned_work": "Planned work",
                  "suspended": "Suspended"}[status],
        "attention": {"good": None, "planned_work": None, "delays": "amber",
                      "service_change": "amber", "suspended": "red"}[status],
    }
