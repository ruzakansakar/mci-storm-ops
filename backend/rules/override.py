"""Overriding a rule-check block. The orchestrator (or any agent) can never do it.

Only a named human, in the role a law rule names in its `override_by` list, and only for a block whose rules all allow that
exception (for example the captain's safety call on the tarmac, 14 CFR 259.4(c)(3)(ii)). Every override goes to audit_log
with the person's role and reason.
"""
from datetime import datetime

from psycopg.types.json import Jsonb

from backend.rules import load_rules

NOT_HUMAN = {"orchestrator", "situation", "network_recovery", "station_staff", "passenger_care", "communication",
             "system", "simulator", "co-pilot", "agent"}


class OverrideRefused(PermissionError):
    pass


def overridable_by(rule_id: str, role: str, rules: dict) -> bool:
    """Can this role override this rule? Law rules say so in `override_by`; a human-only action entry follows its approver."""
    e = next((x for x in rules["entries"] if x["id"] == rule_id), None)
    if e is None:
        return False
    if e["file"] == "law.yaml":
        return role in (e.get("override_by") or [])
    if e["file"] == "autonomy.yaml" and isinstance(e["value"], dict):
        return e["value"].get("level") == "human_only" and e["value"].get("approver") == role
    return False


def human_override(check_result: dict, rule_id: str, role: str, person: str, reason: str, cur=None, now: datetime | None = None,
                   proposal_id=None, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    if not role or role in NOT_HUMAN or not person or person in NOT_HUMAN:
        raise OverrideRefused(f"'{role or person}' is not a named human role; only a person can override a block")
    if not reason or not reason.strip():
        raise OverrideRefused("an override needs the person's reason")
    if check_result.get("result") != "block" or rule_id not in check_result.get("rule_ids", []):
        raise OverrideRefused(f"there is no block on rule '{rule_id}' to override")
    law = [e for e in rules["entries"] if e["id"] == rule_id and e["file"] == "law.yaml"]
    if not law or role not in (law[0].get("override_by") or []):
        raise OverrideRefused(f"rule '{rule_id}' has no exception that the role '{role}' may use")
    remaining, lifted = [], []
    for b in check_result.get("blocks", []):
        if rule_id in b["rule_ids"]:
            if not all(overridable_by(r, role, rules) for r in b["rule_ids"]):
                raise OverrideRefused(f"this block also rests on rules '{role}' can't override: {b['rule_ids']}")
            lifted.append(b)
        else:
            remaining.append(b)
    if cur is not None:
        cur.execute(
            "INSERT INTO audit_log (at, actor, actor_name, what, entity_type, entity_id, before, after) "
            "VALUES (%s, 'human', %s, 'override of a rule-check block', 'rule', %s, %s, %s)",
            (now, person, rule_id, Jsonb({"result": "block", "reasons": [b["reason"] for b in lifted], "proposal_id": proposal_id}),
             Jsonb({"role": role, "person": person, "reason": reason, "rule_id": rule_id})))
    ids = []
    for b in remaining:
        ids += [r for r in b["rule_ids"] if r not in ids]
    return {"result": "block" if remaining else "pass", "reasons": [b["reason"] for b in remaining], "rule_ids": ids,
            "blocks": remaining, "needs_human": remaining[0]["needs_human"] if remaining else None,
            "overridden": {"rule_id": rule_id, "role": role, "person": person, "reason": reason}}
