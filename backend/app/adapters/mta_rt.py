"""MTA real-time trip updates — C2 slice 3 (live chain times).

Protobuf GTFS-RT feeds, keyless since 2023. fetch() parses protobuf at the
edge and stores plain JSON (snapshots must stay JSON): per trip, the route and
{parent_stop_id: arrival_epoch}. Only routes the household actually chains are
kept — payloads stay small.

AIDEV-CAUTION: feed parsing coded to the documented format; verify live on the
box (sandbox cannot reach MTA endpoints). Failures degrade to the scheduled
estimate — never a blank card (invariant 2).
"""

import httpx

from app.adapters.base import Adapter, AdapterManifest
from app.transit.gtfs import parent

_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"
RT_FEEDS = {
    "": "123456S7",          # main feed: numbered lines + 42 St shuttle
    "-ace": "ACE",
    "-bdfm": "BDFM",
    "-g": "G",
    "-jz": "JZ",
    "-nqrw": "NQRW",
    "-l": "L",
    "-si": "SI",
}


def feeds_for(lines: set[str]) -> list[str]:
    """Feed URLs covering the given route ids."""
    urls = []
    for suffix, routes in RT_FEEDS.items():
        if any(ln in routes for ln in lines):
            urls.append(_BASE + suffix)
    return urls


class MTARealtimeAdapter(Adapter):
    manifest = AdapterManifest(
        name="mta_rt",
        version="1.0",
        entity_kind="commute_profile",
        fields=["route_live_minutes", "route_next_departure"],
        poll_seconds_fresh=60,
        stale_after_seconds=300,
        api_key_required=False,
        card_templates=["route_status"],
        registry_record={
            "source_name": "mta_rt",
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
            "last_reviewed_date": "2026-07-17",
            "notes": "GTFS-RT protobuf trip updates for live chain times"
                     " (C2 slice 3). Parsed to JSON at fetch time.",
        },
    )

    def __init__(self, lines: set[str] | None = None):
        self.lines = lines or set()

    async def fetch(self, entity=None) -> dict:
        """All feeds covering monitored chain lines; per-feed isolation."""
        from google.transit import gtfs_realtime_pb2  # heavy import stays local
        out = {"trips": [], "errors": []}
        async with httpx.AsyncClient(timeout=20) as client:
            for url in feeds_for(self.lines):
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    feed = gtfs_realtime_pb2.FeedMessage()
                    feed.ParseFromString(r.content)
                    for entity_msg in feed.entity:
                        if not entity_msg.HasField("trip_update"):
                            continue
                        tu = entity_msg.trip_update
                        route = tu.trip.route_id
                        if route not in self.lines:
                            continue
                        stops = {}
                        for stu in tu.stop_time_update:
                            when = (stu.arrival.time if stu.HasField("arrival")
                                    else stu.departure.time
                                    if stu.HasField("departure") else 0)
                            if when:
                                stops[parent(stu.stop_id)] = when
                        if stops:
                            out["trips"].append({"route": route, "stops": stops})
                except Exception as exc:  # noqa: BLE001 — per-feed isolation
                    out["errors"].append(f"{url[-8:]}: {type(exc).__name__}")
        return out

    def normalize(self, raw: dict) -> dict:
        return {"trips": raw.get("trips", []), "errors": raw.get("errors", [])}


TRANSFER_WALK_S = 120       # platform-to-platform walk at a transfer


def next_departures(trips: list[dict], line: str, from_id: str, to_id: str,
                    now_epoch: int, n: int = 3) -> list[int]:
    """Next n departures at from_id, direction-filtered: only trips that go on
    to reach to_id (so the platform across the street doesn't count)."""
    deps = sorted({t["stops"][from_id] for t in trips
                   if t["route"] == line
                   and t["stops"].get(from_id) and t["stops"].get(to_id)
                   and t["stops"][to_id] > t["stops"][from_id]
                   and t["stops"][from_id] >= now_epoch})
    return deps[:n]


def live_chain(legs: list[dict], trips: list[dict],
               now_epoch: int) -> dict | None:
    """Ride the next available train through each leg in order.
    legs: [{line, from_id, to_id}, ...] (commute_profile legs_json shape).
    Returns {"total_min", "depart_epoch", "arrive_epoch"} or None if any leg
    has no usable upcoming trip (caller falls back to scheduled estimate)."""
    cursor, depart_first, leg_times = now_epoch, None, []
    for leg in legs:
        best = None                                    # (dep, arr)
        for t in trips:
            if t["route"] != leg["line"]:
                continue
            dep = t["stops"].get(leg["from_id"])
            arr = t["stops"].get(leg["to_id"])
            if not dep or not arr or arr <= dep:       # wrong direction/partial
                continue
            if dep >= cursor and (best is None or dep < best[0]):
                best = (dep, arr)
        if best is None:
            return None
        if depart_first is None:
            depart_first = best[0]
        leg_times.append({"depart": best[0], "arrive": best[1],
                          "ride_min": round((best[1] - best[0]) / 60)})
        cursor = best[1] + TRANSFER_WALK_S
    arrive = cursor - TRANSFER_WALK_S
    return {"total_min": round((arrive - depart_first) / 60),
            "depart_epoch": depart_first, "arrive_epoch": arrive,
            "legs": leg_times}
