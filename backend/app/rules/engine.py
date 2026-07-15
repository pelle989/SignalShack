"""Generalized rule engine — evaluates (adapter, entity, field) triples.

AIDEV-CAUTION: this is the product's nervous system (plan §7.1a). Rules are
DATA (see seeds.json / signal_rule.condition_json), never code. The engine
knows nothing about weather — adapters expose fields; rules compare them.

Field contract: adapters provide a flat dict of base + derived fields
(e.g. weather: low_tonight, max_gust_today, freeze_snap_onset, dew_drop_vs_yday).
A rule whose field is absent SKIPS silently — invariant: never fabricate data.

Invariants enforced here:
  - rules only see <=48h data (adapters' responsibility to provide only that)
  - active Warning suppresses priority < 60
  - ambient band (< 30) never occupies the primary slot
  - one rule per topic per composition (highest priority wins)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Fired:
    rule_id: str
    priority: int
    topic: str
    message: str
    window: str = ""


def _check(cond: dict, fields: dict) -> bool | None:
    """One condition row. None = field unavailable (rule skips)."""
    name, op, value = cond["field"], cond["op"], cond["value"]
    if name not in fields or fields[name] is None:
        return None
    actual = fields[name]
    match op:
        case ">=":
            return actual >= value
        case "<=":
            return actual <= value
        case ">":
            return actual > value
        case "<":
            return actual < value
        case "==":
            return actual == value
        case "!=":
            return actual != value
        case "between":
            return value[0] <= actual <= value[1]
        case "in":
            return actual in value
    raise ValueError(f"unknown operator: {op}")


class _Safe(dict):
    def __missing__(self, key):  # unresolved placeholder -> visible, not a crash
        return "{" + key + "}"


def evaluate(rule: dict, fields: dict, month: int,
             recent_fires_7d: int = 0) -> Fired | None:
    """rule: {id, priority, topic, conditions[], output, months?, window?,
    max_fires_per_7d?}. Returns Fired or None."""
    if rule.get("months") and month not in rule["months"]:
        return None
    if rule.get("max_fires_per_7d") and recent_fires_7d >= rule["max_fires_per_7d"]:
        return None
    for cond in rule["conditions"]:
        ok = _check(cond, fields)
        if ok is not True:          # False fails; None (missing field) skips
            return None
    msg = rule["output"].format_map(_Safe({k: v for k, v in fields.items() if v is not None}))
    return Fired(rule["id"], rule["priority"], rule.get("topic", "misc"),
                 msg, rule.get("window", ""))


def compose(fired: list[Fired], warning_active: bool = False
            ) -> tuple[list[Fired], list[Fired]]:
    """-> (primary[0..1], secondary[0..3]) after suppression + dedupe."""
    kept = [f for f in fired if not (warning_active and f.priority < 60)]
    best: dict[str, Fired] = {}
    for f in sorted(kept, key=lambda x: -x.priority):
        best.setdefault(f.topic, f)
    ranked = sorted(best.values(), key=lambda x: -x.priority)
    primary = [f for f in ranked if f.priority >= 30][:1]
    secondary = [f for f in ranked if not primary or f.rule_id != primary[0].rule_id][:3]
    return primary, secondary
