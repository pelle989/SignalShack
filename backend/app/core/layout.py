"""Board layout — the ordered card list (plan §10 customization model).

layout_json on the default board: {"cards": [{"type": ..., "enabled": ...}]}.
First enabled card takes the primary slot. Remove = disable (config survives).
New card types added by updates append to the END, enabled — order changes
never surprise a household (invariant 1 in spirit).
"""

import json
import sqlite3

CARD_TYPES = ["weather", "alerts", "transit", "air", "announcements", "tomorrow"]
DEFAULT = [{"type": t, "enabled": True} for t in CARD_TYPES]


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
    with conn:
        conn.execute("UPDATE board SET layout_json=? WHERE is_default=1",
                     (json.dumps({"cards": cards}),))


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
        if c["type"] == "transit" and not ctx.get("transit"):
            continue
        if c["type"] == "air" and ctx.get("air") is None:
            continue
        if c["type"] == "tomorrow" and ctx.get("tomorrow") is None:
            continue
        out.append(c["type"])
    return out
