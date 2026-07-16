"""MTA subway GTFS static — route → ordered stop sequences.

Downloaded ON THE APPLIANCE (admin → Transit → "Download stop data"), processed
once into a small JSON, cached on disk. This is the dataset that powers
segment scoping now and C2 leg-based routes later.

AIDEV-NOTE: stop_times.txt is ~100 MB uncompressed — it is streamed line by
line, never loaded whole. The canonical stop order for a route is its longest
direction-0 trip.
"""

import csv
import io
import json
import zipfile
from pathlib import Path

import httpx

GTFS_URL = "http://web.mta.info/developers/data/nyct/subway/google_transit.zip"
DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "gtfs_subway.json"

_cache: dict = {"mtime": None, "data": None}


def parent(stop_id: str) -> str:
    """GTFS-RT stop ids carry a platform suffix: F20N/F20S -> F20."""
    return stop_id[:-1] if stop_id and stop_id[-1] in ("N", "S") else stop_id


def load() -> dict | None:
    """Cached dataset: {"routes": {route_id: [{"id", "name"}, ...]}}."""
    if not DATA_FILE.exists():
        return None
    mtime = DATA_FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        _cache["data"] = json.loads(DATA_FILE.read_text())
        _cache["mtime"] = mtime
    return _cache["data"]


def stops_for(route_id: str) -> list[dict]:
    data = load()
    return (data or {}).get("routes", {}).get(route_id, [])


def segment_ids(route_id: str, from_id: str, to_id: str) -> set[str] | None:
    """Parent stop ids between from/to inclusive, direction-agnostic.
    None if the dataset or stops are unknown (caller falls back to line-wide)."""
    stops = stops_for(route_id)
    order = [s["id"] for s in stops]
    try:
        a, b = order.index(from_id), order.index(to_id)
    except ValueError:
        return None
    lo, hi = min(a, b), max(a, b)
    return set(order[lo:hi + 1])


def build_dataset(timeout: int = 120) -> dict:
    """Download + process GTFS static. Returns summary. Network required —
    runs on the appliance, triggered from admin."""
    r = httpx.get(GTFS_URL, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))

    # stops.txt: parent stations (ids without platform suffix)
    stop_names: dict[str, str] = {}
    with zf.open("stops.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
            sid = row["stop_id"]
            if sid == parent(sid):                      # parent rows only
                stop_names[sid] = row["stop_name"]

    # trips.txt: trip -> (route, direction)
    trip_route: dict[str, str] = {}
    with zf.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
            if row.get("direction_id", "0") == "0":
                trip_route[row["trip_id"]] = row["route_id"]

    # stop_times pass 1 (streamed): count stops per direction-0 trip
    counts: dict[str, int] = {}
    with zf.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))
        for row in reader:
            tid = row["trip_id"]
            if tid in trip_route:
                counts[tid] = counts.get(tid, 0) + 1

    # longest trip per route = canonical sequence
    best: dict[str, str] = {}
    for tid, n in counts.items():
        route = trip_route[tid]
        if best.get(route) is None or n > counts[best[route]]:
            best[route] = tid
    chosen = set(best.values())

    # pass 2: collect sequences for chosen trips
    seqs: dict[str, list[tuple[int, str]]] = {t: [] for t in chosen}
    with zf.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))
        for row in reader:
            tid = row["trip_id"]
            if tid in chosen:
                seqs[tid].append((int(row["stop_sequence"]),
                                  parent(row["stop_id"])))

    routes: dict[str, list[dict]] = {}
    for route, tid in best.items():
        ordered = [sid for _, sid in sorted(seqs[tid])]
        seen: set[str] = set()
        stops = []
        for sid in ordered:
            if sid not in seen:
                seen.add(sid)
                stops.append({"id": sid, "name": stop_names.get(sid, sid)})
        routes[route] = stops

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps({"routes": routes}))
    _cache["mtime"] = None                              # invalidate
    return {"routes": len(routes),
            "stops": sum(len(v) for v in routes.values())}
