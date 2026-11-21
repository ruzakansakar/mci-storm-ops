"""Who is allowed to do what: auto, needs_approval or human_only."""

SAFE_DEFAULT = {"level": "human_only", "approver": "station_manager"}


def autonomy_for(action: str, rules: dict, safety_rung: int | None = None, rooms: int | None = None) -> dict:
    """How much the co-pilot may do on its own for this action.

    safety_rung: the lowest ladder rung this action changes (1 = life). Rungs listed in
    never_auto_rungs can never be automatic, even if the action is listed as auto.
    """
    a = dict(rules["autonomy"].get(action, SAFE_DEFAULT))
    reason = "listed in the autonomy rules" if action in rules["autonomy"] else "not listed, so a human decides"
    if a["level"] == "auto" and safety_rung in rules["never_auto_rungs"]:
        a = {"level": "needs_approval", "approver": "station_manager"}
        reason = f"touches safety rung {safety_rung}, so it can't be automatic"
    if a["level"] == "auto" and rooms is not None and "max_rooms" in a and rooms > a["max_rooms"]:
        a = {"level": "needs_approval", "approver": "customer_service_supervisor"}
        reason = f"{rooms} rooms is above the automatic limit"
    return {"action": action, "level": a["level"], "approver": a.get("approver"), "reason": reason}
