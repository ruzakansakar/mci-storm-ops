"""A recovery draft is a proposed statement, never evidence that it is true."""
from datetime import datetime
from functools import lru_cache
import yaml
from backend.settings import ROOT, TZ


@lru_cache
def templates():
    return yaml.safe_load((ROOT / "config" / "recovery_message_templates.yaml").read_text(encoding="utf-8"))


def directives(view):
    facts = (view or {}).get("facts") or {}
    out = {}
    for col, action in (("delays", "delay_flight"), ("cancels", "cancel_flight"), ("crew_actions", None)):
        for row in facts.get(col) or []:
            if row.get("proposal_id") is not None:
                out[row["proposal_id"]] = {k: v for k, v in {**row, "action": action or row.get("action")}.items()
                                            if k in ("proposal_id", "action", "flight_id", "new_departure", "crew_id", "role", "report_by")}
    return out


def text_for(change, audience):
    t = templates()
    action = change["action"]
    values = {"flight_id": change["flight_id"], "crew_id": change.get("crew_id"),
              "change": t["change_names"][action]}
    if change.get("new_departure"):
        values["departure"] = datetime.fromisoformat(change["new_departure"]).astimezone(TZ).strftime("%H:%M")
    if audience == "staff":
        return t["staff"].format(**values)
    return t[audience][action].format(**values)


def draft_problems(draft, change):
    if draft.get("change") != change:
        return ["proposal facts changed; redraft required"]
    try:
        expected = text_for(change, draft["audience"])
    except (KeyError, TypeError, ValueError):
        return ["unsupported or incomplete recovery proposal"]
    return [] if draft.get("text") == expected else ["draft text is not the fact-checked proposal template"]


def send_problems(draft, world):
    view = world.get("recovery_view")
    change = directives(view).get(draft.get("proposal_id"))
    if change is None:
        return ["proposal rejected, superseded or no longer published"]
    problems = draft_problems(draft, change)
    approvals = {a["proposal_id"]: a for a in view["facts"].get("approval_status") or []}
    approval = approvals.get(draft["proposal_id"], {})
    role = "crew_scheduling" if change.get("crew_id") else "network_operations_control"
    if (approval.get("status") != "approved" or approval.get("rule_check_ok") is not True
            or approval.get("role") != role or approval.get("plan_version") != draft.get("plan_version")):
        problems.append("network proposal is not approved")
    if not world.get("recovery_fresh") or not world.get("truth_feed_fresh") or not world.get("flight_feed_fresh"):
        problems.append("recovery or operational source is stale")
    if change.get("crew_id") and not world.get("crew_feed_fresh"):
        problems.append("crew operations source is stale")
    if change["flight_id"] in world.get("open_conflicts", ()):
        problems.append("source conflict on flight")
    # Approval is not application. A source confirmation binds the applied action
    # to the proposal/version, and current operational facts must still match.
    applied = world.get("recovery_applications", {}).get(draft["proposal_id"])
    if (not applied or applied.get("plan_version") != draft.get("plan_version") or applied.get("change") != change):
        problems.append("approved, waiting to take effect")
    elif not truth_matches(change, world.get("recovery_flights", {}), world.get("recovery_crews", {})):
        problems.append("approved, waiting to take effect")
    if draft.get("audience") not in ("passenger", "crew"):
        problems.append("internal staff notes cannot use passenger delivery")
    if draft.get("audience") == "passenger":
        pid = draft.get("passenger_id")
        if change["flight_id"] not in world.get("recovery_passenger_flights", {}).get(pid, ()):
            problems.append("passenger is not associated with this flight")
        if draft.get("channel") not in world.get("screens", {}).get(pid, ()) or draft.get("channel") not in ("app", "sms"):
            problems.append("passenger has no permitted delivery channel")
        if draft.get("trip_facts") != world.get("recovery_trips", {}).get(pid):
            problems.append("passenger trip changed; redraft required")
    elif draft.get("crew_id") != change.get("crew_id") or draft.get("channel") != "crew":
        problems.append("crew recipient does not match the proposal")
    return list(dict.fromkeys(problems))


def truth_matches(change, flights, crews):
    flight = flights.get(change["flight_id"], {})
    action = change["action"]
    if action == "cancel_flight":
        return flight.get("status") == "cancelled"
    if flight.get("status") in (None, "cancelled", "departed", "arrived"):
        return False
    if action == "delay_flight":
        return flight.get("departure") == change.get("new_departure")
    return crews.get((change["flight_id"], change.get("role"))) == change.get("crew_id")
