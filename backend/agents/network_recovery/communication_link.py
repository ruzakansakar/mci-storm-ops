"""Publish versioned proposal references and decisions, without changing the plan.

Only this producer writes recovery_plans. Review and human-decision records enter
through its contract; the execution layer never edits a shared row in place.
"""
from copy import deepcopy
from datetime import timedelta
import json


def sync_link(ctx, memory):
    if not memory.plans:
        return []
    state = getattr(memory, "link_state", {})
    read_at = getattr(memory, "link_read_at", None)
    for e in ctx.read_events(delivered_since=read_at - timedelta(microseconds=1) if read_at else None):
        p = e.get("payload") or {}
        key = p.get("proposal_id")
        if key is None:
            continue
        if e["type"] == "network_proposal_reviewed" and key not in state:
            state[key] = {**p, "status": "pending" if p.get("rule_check_ok") else "blocked"}
        elif e["type"] == "network_approval_recorded" and key in state:
            item = state[key]
            if (item["status"] == "pending" and item.get("rule_check_ok") and p.get("role") == item.get("role")
                    and p.get("plan_version") == item.get("plan_version")):
                if p.get("status") in ("approved", "rejected"):
                    item.update(status=p["status"], decided_at=e["occurred_at"].isoformat())
    memory.link_read_at = ctx.now
    row = deepcopy(memory.plans[-1])
    row.update(getattr(memory, "basis", None) or {})   # the upstream versions it read last (5a: no stale version recorded)
    active = set()
    for col in ("delays", "cancels", "crew_actions"):
        for item in row[col]:
            item.pop("proposal_id", None)
            if col == "delays":
                matches = [p for p in state.values() if p["action"] == "delay_flight" and p["flight_id"] == item["flight_id"]
                           and p.get("new_departure") == item["new_departure"]]
            elif col == "cancels":
                matches = [p for p in state.values() if p["action"] == "cancel_flight" and p["flight_id"] == item["flight_id"]]
            else:
                option = item.get("option") or {}
                item.update(crew_id=option.get("crew_id"), action=option.get("action"))
                matches = [p for p in state.values() if p["action"] == item.get("action") and p["flight_id"] == item["flight_id"]
                           and p.get("crew_id") == item.get("crew_id") and p.get("crew_role") == item["role"]]
            if matches:
                chosen = max(matches, key=lambda p: p["proposal_id"])
                item["proposal_id"] = chosen["proposal_id"]
                active.add(chosen["proposal_id"])
    for key, item in state.items():
        if key not in active and item.get("plan_version") and item["status"] in ("pending", "approved"):
            item["status"] = "superseded"
    # A proposal retains its originating version even when a later version publishes its approval.
    next_version = memory.version + 1
    for key in active:
        state[key].setdefault("plan_version", next_version)
    row["approval_status"] = [{k: p.get(k) for k in ("proposal_id", "plan_version", "status", "role", "rule_check_ok", "decided_at")}
                              for _, p in sorted(state.items()) if p.get("plan_version")]
    signature = json.dumps({k: row[k] for k in ("delays", "cancels", "crew_actions", "approval_status")}, sort_keys=True, default=str)
    memory.link_state = state
    if signature == getattr(memory, "link_signature", None) and row.get("approval_status") == memory.plans[-1].get("approval_status"):
        return row["approval_status"]
    memory.version = next_version
    row.update(version=next_version, planned_at=ctx.now, data_as_of=ctx.now)
    ctx.write("recovery_plans", row)
    ctx.record_event("recovery_plan_drafted", {"version": next_version, "reason": "proposal links and approval status",
                                               "applied": False})
    memory.plans.append({**row, "planned_at": ctx.now.isoformat(), "data_as_of": ctx.now.isoformat()})
    memory.link_signature = signature
    ctx.heartbeat({"latest_version": next_version})
    return row["approval_status"]
