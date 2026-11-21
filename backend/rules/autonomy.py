"""Who is allowed to do what: auto, needs_approval or human_only."""

SAFE_DEFAULT = {"level": "human_only", "approver": "station_manager"}


class NoOwnerRole(ValueError):
    """An automatic action had to go to a human, but nobody owns it. We never guess a role."""


def autonomy_for(action: str, rules: dict, safety_rung: int | None = None, rooms: int | None = None,
                 owner: str | None = None) -> dict:
    """How much the co-pilot may do on its own for this action.

    safety_rung: the lowest ladder rung this action changes (1 = life). Rungs listed in
    never_auto_rungs can never be automatic, even if the action is listed as auto.
    owner: the role that owns the decision (from the decision catalog). It approves instead of the action's default approver.
    When an auto action touches a never-auto rung, it goes to the decision's owner, else the action's own owner, else it fails.
    """
    a = dict(rules["autonomy"].get(action, SAFE_DEFAULT))
    reason = "listed in the autonomy rules" if action in rules["autonomy"] else "not listed, so a human decides"
    if a["level"] == "auto" and safety_rung in rules["never_auto_rungs"]:
        role = owner or a.get("owner")
        if not role:
            raise NoOwnerRole(f"'{action}' touches safety rung {safety_rung} so a person must approve it, but no owner role is defined")
        a = {"level": "needs_approval", "approver": role}
        reason = f"touches safety rung {safety_rung}, so it can't be automatic"
    elif a["level"] == "needs_approval" and owner:
        a = {**a, "approver": owner}
        reason = "listed in the autonomy rules; approved by the decision's owner"
    if a["level"] == "auto" and rooms is not None and "max_rooms" in a and rooms > a["max_rooms"]:
        a = {"level": "needs_approval", "approver": a.get("over_limit_approver", "customer_service_supervisor")}
        reason = f"{rooms} rooms is above the automatic limit"
    return {"action": action, "level": a["level"], "approver": a.get("approver"), "reason": reason}
