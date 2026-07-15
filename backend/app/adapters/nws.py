"""NWS adapter — alerts (authoritative, no substitute exists) + forecast text.

SAFETY: alert data feeds the display's safety surface; freshness targets are
the tightest in the product (1 min poll / 5 min stale) and the unknown state
must never render as "no alerts" — see plan §7.3.
"""

import httpx

from app.adapters.base import Adapter, AdapterManifest

USER_AGENT = "SignalShack/0.1 (local home appliance; github.com/signalshack)"


class NWSAdapter(Adapter):
    manifest = AdapterManifest(
        name="nws",
        version="1.0",
        entity_kind="location",
        fields=["warning_active", "watch_active", "advisory_active",
                "alert_count", "top_alert_event", "top_alert_headline",
                "top_alert_severity", "top_alert_ends"],
        poll_seconds_fresh=60,
        stale_after_seconds=300,
        api_key_required=False,
        card_templates=["nws_alerts"],
        registry_record={
            "source_name": "nws",
            "source_url": "https://api.weather.gov",
            "license_type": "US Government public domain",
            "commercial_use_allowed": 1,
            "redistribution_allowed": 1,
            "bulk_storage_allowed": 1,
            "cache_allowed": 1,
            "attribution_required": 1,
            "attribution_text": "National Weather Service",
            "api_key_required": 0,
            "rate_limit": "unpublished; honor Retry-After, identify via User-Agent",
            "terms_url": "https://www.weather.gov/documentation/services-web-api",
            "last_reviewed_date": "2026-07-13",
            "notes": "Alerts are NWS-only — no fallback source exists (plan §10).",
        },
    )

    async def fetch(self, entity) -> dict:
        url = f"https://api.weather.gov/alerts/active?point={entity['latitude']},{entity['longitude']}"
        async with httpx.AsyncClient(
                timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()

    def normalize(self, raw: dict) -> dict:
        alerts = []
        for f in raw.get("features", []):
            p = f.get("properties", {})
            alerts.append({"event": p.get("event"), "severity": p.get("severity"),
                           "headline": p.get("headline"), "ends": p.get("ends")})
        sev_rank = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
        alerts.sort(key=lambda a: sev_rank.get(a["severity"], 9))
        top = alerts[0] if alerts else {}

        def has(kind):
            return any(kind in (a.get("event") or "") for a in alerts)

        return {
            "warning_active": has("Warning") or any(
                a.get("severity") in ("Extreme", "Severe") for a in alerts),
            "watch_active": has("Watch"),
            "advisory_active": has("Advisory"),
            "alert_count": len(alerts),
            "top_alert_event": top.get("event"),
            "top_alert_headline": top.get("headline"),
            "top_alert_severity": top.get("severity"),
            "top_alert_ends": top.get("ends"),
            "_alerts": alerts,   # full list for the alerts card renderer
        }
