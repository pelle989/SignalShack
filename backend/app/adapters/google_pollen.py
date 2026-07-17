"""Google Pollen adapter — V1.2 slice 1 (household type toggles, own card).

Keyed source (Google Maps Platform; billing account required, generous free
tier). UPI index 0-5 per type: TREE / GRASS / WEED.

AIDEV-CAUTION: the key rides a query param (Google's scheme) — never log URLs
here. Fetch errors log exception TYPE only, like every adapter.
"""

import httpx

from app.adapters.base import Adapter, AdapterManifest

API_URL = "https://pollen.googleapis.com/v1/forecast:lookup"
TYPES = ["TREE", "GRASS", "WEED"]
TYPE_LABELS = {"TREE": "Tree", "GRASS": "Grass", "WEED": "Weed"}

# UPI categories, index value -> plain meaning (copy-voice: meaning, not data)
MEANINGS = {
    0: "none in the air",
    1: "very low — fine for everyone",
    2: "low — fine for most",
    3: "moderate — sensitive folks may notice",
    4: "high — plan meds before going out",
    5: "very high — keep windows closed",
}


class GooglePollenAdapter(Adapter):
    manifest = AdapterManifest(
        name="google_pollen",
        version="1.0",
        entity_kind="location",
        fields=["pollen_tree_index", "pollen_grass_index", "pollen_weed_index"],
        poll_seconds_fresh=6 * 3600,        # daily-scale data; 4 polls/day max
        stale_after_seconds=24 * 3600,
        api_key_required=True,
        card_templates=["pollen"],
        registry_record={
            "source_name": "google_pollen",
            "source_url": "https://developers.google.com/maps/documentation/pollen",
            "license_type": "Google Maps Platform ToS (keyed, free tier)",
            "commercial_use_allowed": 1,
            "redistribution_allowed": 0,
            "bulk_storage_allowed": 0,
            "cache_allowed": 1,             # transient caching permitted
            "attribution_required": 1,
            "attribution_text": "Google",
            "api_key_required": 1,
            "rate_limit": "free tier ~limited QPM; we poll 4x/day",
            "terms_url": "https://cloud.google.com/maps-platform/terms",
            "last_reviewed_date": "2026-07-17",
            "notes": "UPI 0-5 per pollen type. Key is per-household"
                     " (Connect tier may proxy later, plan §6).",
        },
    )

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    async def fetch(self, entity) -> dict:
        params = {
            "key": self.api_key,
            "location.latitude": entity["latitude"],
            "location.longitude": entity["longitude"],
            "days": 1,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(API_URL, params=params)
            r.raise_for_status()
            return r.json()

    def normalize(self, raw: dict) -> dict:
        """-> {"types": {"TREE": {"index", "category", "meaning"}, ...}}.
        Types the API omits (out of season) come back index 0."""
        out = {t: {"index": 0, "category": "None",
                   "meaning": MEANINGS[0]} for t in TYPES}
        days = raw.get("dailyInfo") or []
        for info in (days[0].get("pollenTypeInfo", []) if days else []):
            code = info.get("code")
            if code not in out:
                continue
            idx = (info.get("indexInfo") or {}).get("value")
            if idx is None:                  # no index = not reported today
                continue
            out[code] = {"index": idx,
                         "category": (info.get("indexInfo") or {}).get(
                             "category", ""),
                         "meaning": MEANINGS.get(idx, "")}
        return {"types": out}
