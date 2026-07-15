"""Source adapter contract.

Every data source — prebuilt or future — implements this and ships a complete
source-registry record. CI fails any adapter whose manifest or registry record
is incomplete (tests/test_source_registry.py).

AIDEV-NOTE: the manifest's `fields` list is what auto-populates the rule
editor's field picker; adding an adapter automatically extends the rule engine
with zero engine changes (plan §7.1a).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdapterManifest:
    name: str                       # e.g. "nws"
    version: str
    fields: list[str]               # exposed to rule engine, e.g. ["temp", "wind_gust"]
    entity_kind: str                # "location" | "commute_profile" | ...
    registry_record: dict           # complete source_registry row (validated in CI)
    api_key_required: bool = False
    poll_seconds_fresh: int = 1800
    stale_after_seconds: int = 7200
    card_templates: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        problems = []
        required = ["source_name", "source_url", "license_type", "terms_url",
                    "last_reviewed_date", "cache_allowed", "commercial_use_allowed",
                    "attribution_required"]
        for key in required:
            if self.registry_record.get(key) is None:
                problems.append(f"{self.name}: registry_record missing '{key}'")
        if not self.fields:
            problems.append(f"{self.name}: no fields exposed")
        return problems


class Adapter:
    """Subclass and implement. fetch() returns raw payload; normalize() maps it
    to {field: series} for snapshots and the rule engine."""

    manifest: AdapterManifest

    async def fetch(self, entity) -> dict: ...
    def normalize(self, raw: dict) -> dict: ...
