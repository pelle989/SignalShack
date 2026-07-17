"""Board layout — the ordered card list (plan §10 customization model).

layout_json on the default board: {"cards": [{"type": ..., "enabled": ...}]}.
First enabled card takes the primary slot. Remove = disable (config survives).
New card types added by updates append to the END, enabled — order changes
never surprise a household (invariant 1 in spirit).
"""

import json
import sqlite3

CARD_TYPES = ["weather", "alerts", "transit", "air", "pollen",
              "announcements", "tomorrow", "forecast", "outlook"]
DEFAULT = [{"type": t, "enabled": True} for t in CARD_TYPES]

# board-level density presets — deliberately NOT per-card sizing (3 states to
# test, not 3^n; every unit still looks like a SignalShack)
DENSITIES = ["comfortable", "compact", "focus"]
FOCUS_CARD_CAP = 3


FORECAST_STYLES = ["chart", "table"]
FORECAST_HORIZONS = [12, 48, 72]        # hours the forecast chart spans


def _persist(conn: sqlite3.Connection, **changes) -> None:
    """Write layout_json, preserving every board setting not being changed.
    Single source of truth so a new setting can't be silently clobbered by
    an unrelated setter (the old inline-dict pattern kept dropping keys)."""
    settings = {
        "cards": get_layout(conn),
        "density": get_density(conn),
        "forecast_style": get_forecast_style(conn),
        "forecast_horizon": get_forecast_horizon(conn),
    }
    settings.update(changes)
    with conn:
        conn.execute("UPDATE board SET layout_json=? WHERE is_default=1",
                     (json.dumps(settings),))


def get_forecast_style(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT layout_json FROM board WHERE is_default=1").fetchone()
    if row is None:
        return "chart"
    s = json.loads(row["layout_json"] or "{}").get("forecast_style")
    return s if s in FORECAST_STYLES else "chart"


def set_forecast_style(conn: sqlite3.Connection, style: str) -> None:
    if style not in FORECAST_STYLES:
        return
    _persist(conn, forecast_style=style)


def get_forecast_horizon(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT layout_json FROM board WHERE is_default=1").fetchone()
    if row is None:
        return 12
    h = json.loads(row["layout_json"] or "{}").get("forecast_horizon")
    return h if h in FORECAST_HORIZONS else 12


def set_forecast_horizon(conn: sqlite3.Connection, hours: int) -> None:
    if hours not in FORECAST_HORIZONS:
        return
    _persist(conn, forecast_horizon=hours)


def get_density(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT layout_json FROM board WHERE is_default=1").fetchone()
    if row is None:
        return "comfortable"
    d = json.loads(row["layout_json"] or "{}").get("density")
    return d if d in DENSITIES else "comfortable"


def set_density(conn: sqlite3.Connection, density: str) -> None:
    if density not in DENSITIES:
        return
    _persist(conn, density=density)


def get_layout(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute(
        "SELECT id, layout_json FROM board WHERE is_default=1").fetchone()
    if row is None:
        return [dict(c) for c in DEFAULT]
    data = json.loads(row["layout_json"] or "{}")
    cards = data.get("cards")
    if not cards:                       # legacy '{"preset": "default"}' boards
        cards = [dict(c) for c in DEFAULT]
    # updates may introduce new card types: append them, enabled
    known = {c["type"] for c in cards}
    for t in CARD_TYPES:
        if t not in known:
            cards.append({"type": t, "enabled": True})
    cards = [c for c in cards if c["type"] in CARD_TYPES]
    return cards


def _save(conn: sqlite3.Connection, cards: list[dict]) -> None:
    _persist(conn, cards=cards)     # reorders must not drop other settings


def move(conn: sqlite3.Connection, card_type: str, direction: str) -> None:
    cards = get_layout(conn)
    idx = next((i for i, c in enumerate(cards) if c["type"] == card_type), None)
    if idx is None:
        return
    swap = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap < len(cards):
        cards[idx], cards[swap] = cards[swap], cards[idx]
        _save(conn, cards)


def toggle(conn: sqlite3.Connection, card_type: str) -> None:
    cards = get_layout(conn)
    for c in cards:
        if c["type"] == card_type:
            c["enabled"] = not c["enabled"]
            _save(conn, cards)
            return


def visible_order(conn: sqlite3.Connection, ctx: dict) -> list[str]:
    """Enabled cards, in order, filtered by data availability:
    transit needs monitored lines; air needs a configured key."""
    out = []
    for c in get_layout(conn):
        if not c["enabled"]:
            continue
        if c["type"] == "transit" and not (ctx.get("transit")
                                           or ctx.get("transit_routes")):
            continue
        if c["type"] == "air" and ctx.get("air") is None:
            continue
        if c["type"] == "pollen" and ctx.get("pollen") is None:
            continue
        if c["type"] == "forecast" and ctx.get("forecast") is None:
            continue
        if c["type"] == "outlook" and ctx.get("outlook") is None:
            continue
        if c["type"] == "tomorrow" and ctx.get("tomorrow") is None:
            continue
        out.append(c["type"])
    if get_density(conn) == "focus":         # primary + two — nothing else
        out = out[:FOCUS_CARD_CAP]
    return out
