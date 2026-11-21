"""Read the rule files in config/rules/ and refuse to start if they are broken.

Every file holds a list of rules. Each rule has: id, path, description, value, source, citation, enforced and (if enforced)
checked_by. The loader merges them into the dictionary the checks read: a rule with path "limits.fuel_buffer_min" becomes
rules["limits"]["fuel_buffer_min"]. A path ending in [] appends to a list (the safety ladder rungs, in file order).
"""
from functools import lru_cache
from pathlib import Path

import yaml

RULES_DIR = Path(__file__).resolve().parents[2] / "config" / "rules"
RULE_FILES = ("law", "southwest", "operations", "safety_ladder", "autonomy")
LEVELS = {"auto", "needs_approval", "human_only"}
SOURCES = {"law", "southwest", "assumption", "class_files"}
FIELDS = ("id", "path", "description", "value", "source", "citation", "enforced")


class RulesError(ValueError):
    pass


def read_entries(directory: Path = RULES_DIR) -> list[dict]:
    """Every rule from every file, in file order. Each entry also records which file it came from."""
    entries = []
    for name in RULE_FILES:
        data = yaml.safe_load((Path(directory) / f"{name}.yaml").read_text(encoding="utf-8"))
        for e in data["rules"]:
            entries.append({**e, "file": f"{name}.yaml"})
    return entries


def check_entries(entries: list[dict], checkers: dict | None = None) -> None:
    """Every rule has the fields, a known source, a citation, and (if enforced) a function that really exists."""
    if checkers is None:
        from backend.rules.registry import CHECKERS as checkers
    seen = set()
    for e in entries:
        who = e.get("id", "?")
        for f in FIELDS:
            if f not in e or (f in ("id", "path", "description", "citation") and not str(e[f]).strip()):
                raise RulesError(f"rule '{who}' is missing '{f}'")
        if e["id"] in seen:
            raise RulesError(f"rule id '{who}' appears twice")
        seen.add(e["id"])
        if e["source"] not in SOURCES:
            raise RulesError(f"rule '{who}': source must be one of {sorted(SOURCES)}")
        if not isinstance(e["enforced"], bool):
            raise RulesError(f"rule '{who}': enforced must be true or false")
        if e["enforced"] and e.get("checked_by") not in checkers:
            raise RulesError(f"rule '{who}' is enforced but has no function named '{e.get('checked_by')}'")


def set_path(rules: dict, path: str, value) -> None:
    append = path.endswith("[]")
    keys = path[:-2].split(".") if append else path.split(".")
    cur = rules
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    if append:
        cur.setdefault(keys[-1], []).append(value)
    else:
        if keys[-1] in cur:
            raise RulesError(f"two rules write to '{path}'")
        cur[keys[-1]] = value


def merge(entries: list[dict]) -> dict:
    rules = {"version": 2}
    for e in entries:
        set_path(rules, e["path"], e["value"])
    rules["entries"] = entries
    return rules


@lru_cache
def load_rules(path: Path = RULES_DIR) -> dict:
    entries = read_entries(path)
    check_entries(entries)
    rules = merge(entries)
    validate(rules)
    return rules


def validate(rules: dict) -> None:
    for key in ("safety_ladder", "limits", "child_alone", "autonomy", "compensation", "never_auto_rungs",
                "pilot", "fa", "ground_staff", "tarmac", "lightning", "airports", "wheelchair"):
        if key not in rules:
            raise RulesError(f"the rules are missing '{key}'")

    rungs = [r["rung"] for r in rules["safety_ladder"]]
    if rungs != sorted(rungs) or len(set(rungs)) != len(rungs):
        raise RulesError("safety_ladder rungs must be unique and in order")
    kinds = [r["kind"] for r in rules["safety_ladder"]]
    if "rank" in kinds and "filter" in kinds[kinds.index("rank"):]:
        raise RulesError("all filter rungs must come before rank rungs")

    from backend.rules.safety import CHECKS  # every check named in the files must exist in code
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

    table = rules["pilot"]["fdp_table_b"]
    for row in table["rows"]:
        if len(row["hours"]) != len(table["segments"]):
            raise RulesError(f"Table B row {row['from']} needs {len(table['segments'])} numbers")
