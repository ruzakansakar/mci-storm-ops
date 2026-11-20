"""Read config/rules.yaml and refuse to start if it is broken."""
from functools import lru_cache
from pathlib import Path

import yaml

RULES_FILE = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"
LEVELS = {"auto", "needs_approval", "human_only"}


class RulesError(ValueError):
    pass


@lru_cache
def load_rules(path: Path = RULES_FILE) -> dict:
    rules = yaml.safe_load(Path(path).read_text())
    validate(rules)
    return rules


def validate(rules: dict) -> None:
    for key in ("safety_ladder", "limits", "child_alone", "autonomy", "compensation", "never_auto_rungs"):
        if key not in rules:
            raise RulesError(f"rules.yaml is missing '{key}'")

    rungs = [r["rung"] for r in rules["safety_ladder"]]
    if rungs != sorted(rungs) or len(set(rungs)) != len(rungs):
        raise RulesError("safety_ladder rungs must be unique and in order")
    kinds = [r["kind"] for r in rules["safety_ladder"]]
    if "rank" in kinds and "filter" in kinds[kinds.index("rank"):]:
        raise RulesError("all filter rungs must come before rank rungs")

    from backend.rules.safety import CHECKS  # every check named in the file must exist in code
    for r in rules["safety_ladder"]:
        for name in r.get("checks", []):
            if name not in CHECKS:
                raise RulesError(f"rung {r['rung']} names unknown check '{name}'")

    for action, a in rules["autonomy"].items():
        if a["level"] not in LEVELS:
            raise RulesError(f"{action}: level must be one of {sorted(LEVELS)}")
        if a["level"] != "auto" and not a.get("approver"):
            raise RulesError(f"{action}: {a['level']} needs an approver role")

    for k, v in rules["limits"].items():
        if not isinstance(v, (int, float)) or v < 0:
            raise RulesError(f"limits.{k} must be a non-negative number")

    ca = rules["child_alone"]
    for k in ("min_age", "max_age"):
        if not isinstance(ca.get(k), int) or ca[k] < 0:
            raise RulesError(f"child_alone.{k} must be a non-negative whole number")
    if ca["min_age"] > ca["max_age"]:
        raise RulesError("child_alone.min_age can't be above max_age")
    for k in ("nonstop_or_same_plane_only", "international_allowed"):
        if not isinstance(ca.get(k), bool):
            raise RulesError(f"child_alone.{k} must be true or false")

    for k in ("bag_refund_hours_domestic", "bag_refund_hours_international", "bag_refund_hours_international_long",
              "long_flight_hours", "bag_fee_usd"):
        v = rules["compensation"].get(k)
        if not isinstance(v, (int, float)) or v < 0:
            raise RulesError(f"compensation.{k} must be a non-negative number")
