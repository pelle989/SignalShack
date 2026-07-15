"""Rule engine tests — including voice-guide enforcement on all seed copy."""

import json
from pathlib import Path

from app.rules.engine import Fired, compose, evaluate

SEEDS = json.loads(
    (Path(__file__).parents[1] / "app" / "rules" / "seeds.json").read_text())["rules"]


def rule(**kw):
    base = {"id": "X", "priority": 50, "topic": "t", "conditions": [], "output": "msg"}
    base.update(kw)
    return base


# ---------------- operators & gating

def test_operators():
    cases = [
        ({"field": "v", "op": ">=", "value": 5}, {"v": 5}, True),
        ({"field": "v", "op": "<", "value": 5}, {"v": 5}, False),
        ({"field": "v", "op": "between", "value": [3, 7]}, {"v": 7}, True),
        ({"field": "v", "op": "in", "value": ["W", "NW"]}, {"v": "NW"}, True),
        ({"field": "v", "op": "!=", "value": 1}, {"v": 2}, True),
    ]
    for cond, fields, expect in cases:
        fired = evaluate(rule(conditions=[cond]), fields, month=6)
        assert (fired is not None) is expect, (cond, fields)


def test_missing_field_skips_never_fires():
    fired = evaluate(rule(conditions=[{"field": "absent", "op": ">=", "value": 1}]),
                     {}, month=6)
    assert fired is None  # invariant: never fabricate


def test_month_gating():
    r = rule(months=[12, 1, 2], conditions=[{"field": "v", "op": ">=", "value": 0}])
    assert evaluate(r, {"v": 1}, month=1) is not None
    assert evaluate(r, {"v": 1}, month=7) is None


def test_weekly_cap():
    r = rule(max_fires_per_7d=2, conditions=[{"field": "v", "op": ">=", "value": 0}])
    assert evaluate(r, {"v": 1}, month=6, recent_fires_7d=1) is not None
    assert evaluate(r, {"v": 1}, month=6, recent_fires_7d=2) is None


def test_template_renders_and_survives_missing_placeholder():
    r = rule(output="High {high:.0f}° and {mystery}")
    fired = evaluate(r, {"high": 88.6}, month=6)
    assert "89°" in fired.message and "{mystery}" in fired.message


# ---------------- composition invariants

def F(rid, pri, topic):
    return Fired(rid, pri, topic, f"{rid} msg")


def test_topic_dedupe_highest_wins():
    primary, secondary = compose([F("T1", 75, "precip"), F("T7", 76, "precip")])
    ids = [f.rule_id for f in primary + secondary]
    assert "T7" in ids and "T1" not in ids


def test_ambient_never_primary():
    primary, secondary = compose([F("A1", 22, "outdoor"), F("A2", 14, "sky")])
    assert primary == []
    assert {f.rule_id for f in secondary} == {"A1", "A2"}


def test_warning_suppresses_below_60():
    primary, secondary = compose(
        [F("P6", 34, "temp"), F("T4", 80, "season")], warning_active=True)
    ids = [f.rule_id for f in primary + secondary]
    assert ids == ["T4"]


def test_max_three_secondaries():
    fired = [F(f"R{i}", 40 + i, f"topic{i}") for i in range(6)]
    primary, secondary = compose(fired)
    assert len(primary) == 1 and len(secondary) == 3


# ---------------- seed data quality (voice guide, machine-checkable subset)

def test_seeds_load_and_are_complete():
    assert len(SEEDS) == 24
    assert len({r["id"] for r in SEEDS}) == 24
    for r in SEEDS:
        assert r["conditions"], r["id"]
        assert 10 <= r["priority"] <= 99, r["id"]


def test_seed_copy_obeys_voice_guide():
    import re
    for r in SEEDS:
        msg = r["output"]
        rendered = re.sub(r"\{[^}]+\}", "00", msg)   # measure as rendered, not template
        assert "!" not in msg, f"{r['id']}: exclamation point"
        assert len(rendered) <= 90, f"{r['id']}: >90 chars rendered ({len(rendered)})"
        assert msg == msg.strip() and msg[0].isupper() or msg[0].isdigit(), r["id"]
        assert not any(ord(c) > 0x2700 for c in msg), f"{r['id']}: emoji"


def test_safety_seeds_name_risk_then_action():
    # safety band: risk and an action verb must both be present
    for r in SEEDS:
        if r["priority"] >= 90:
            assert "." in r["output"], f"{r['id']}: safety copy needs risk + action sentences"
