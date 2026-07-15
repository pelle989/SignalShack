"""AirNow adapter — EPA air quality (V1.1). First keyed adapter: the free key
is an email signup (no credit card — this is why the Monitors story leads
with AQ, not pollen; plan §5)."""

import httpx

from app.adapters.base import Adapter, AdapterManifest

CATEGORY_MEANING = {
    # AQI category number -> voice-compliant meaning line
    1: "Air quality good.",
    2: "Air quality moderate — fine for most.",
    3: "Air quality poor for sensitive groups — limit long outdoor efforts.",
    4: "Air quality unhealthy — keep windows closed.",
    5: "Air quality very unhealthy — stay inside if you can.",
    6: "Air quality hazardous — stay inside, seal windows.",
}


class AirNowAdapter(Adapter):
    manifest = AdapterManifest(
        name="airnow",
        version="1.0",
        entity_kind="location",
        fields=["aqi", "aqi_category", "aqi_pollutant"],
        poll_seconds_fresh=3600,          # hourly source data (plan refresh table)
        stale_after_seconds=10800,
        api_key_required=True,
        card_templates=["air_quality"],
        registry_record={
            "source_name": "airnow",
            "source_url": "https://www.airnowapi.org",
            "license_type": "US EPA public data; free API account",
            "commercial_use_allowed": 1,
            "redistribution_allowed": 1,
            "bulk_storage_allowed": 1,
            "cache_allowed": 1,
            "attribution_required": 1,
            "attribution_text": "AirNow (US EPA)",
            "api_key_required": 1,
            "rate_limit": "500 requests/hour per key",
            "terms_url": "https://docs.airnowapi.org/faq",
            "last_reviewed_date": "2026-07-15",
            "notes": "Per-household key, email signup only. Hourly poll uses"
                     " ~24 calls/day of the 500/hr allowance.",
        },
    )

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    async def fetch(self, entity) -> dict:
        params = {"format": "application/json",
                  "latitude": entity["latitude"], "longitude": entity["longitude"],
                  "distance": 25, "API_KEY": self.api_key}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://www.airnowapi.org/aq/observation/latLong/current/",
                params=params)
            r.raise_for_status()
            return {"observations": r.json()}

    def normalize(self, raw: dict) -> dict:
        """Worst pollutant wins the card (standard AQI presentation)."""
        worst = None
        for obs in raw.get("observations", []):
            aqi = obs.get("AQI")
            if aqi is None or aqi < 0:
                continue
            if worst is None or aqi > worst["aqi"]:
                worst = {"aqi": aqi,
                         "aqi_category": (obs.get("Category") or {}).get("Number", 1),
                         "aqi_category_name": (obs.get("Category") or {}).get("Name", ""),
                         "aqi_pollutant": obs.get("ParameterName", "")}
        if worst is None:
            return {"aqi": None, "aqi_category": None, "aqi_pollutant": None,
                    "meaning": None}
        worst["meaning"] = CATEGORY_MEANING.get(worst["aqi_category"],
                                                CATEGORY_MEANING[2])
        return worst
