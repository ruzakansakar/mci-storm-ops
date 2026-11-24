"""Measuring the orchestrator (Phase 5a). Measurement only: it reads the orchestrator's minute log, queue, conflicts and
incidents. Nothing here goes back to an agent or to the orchestrator."""
from datetime import datetime

AGENTS = ["situation", "network_recovery", "passenger_care", "station_staff", "communication"]
TABLE = {"situation": "situation_assessments", "network_recovery": "recovery_plans", "passenger_care": "care_plans",
         "station_staff": "station_plans"}


def connections(log, at: str):
    """How the agents connected at `at` (HH:MM): for each agent, the last minute it ran up to then, the shared versions it read
    that minute, and the newest version of its own plan published by then."""
    rows = [r for r in log if r["at"][11:16] <= at]
    out = []
    for a in AGENTS:
        last = next((r for r in reversed(rows) if a in r["ran"]), None)
        published = next((r["wrote"][a][-1] for r in reversed(rows) if a in r.get("wrote", {})), None)
        out.append({"agent": a, "last_ran": last["at"][11:16] if last else None, "read": (last or {}).get("read", {}).get(a, {}),
                    "published": {TABLE[a]: published} if a in TABLE and published is not None else {},
                    "stale": [s["detail"] for s in (last or {}).get("stale", []) if s["agent"] == a]})
    return out


def orchestrator_metrics(o) -> dict | None:
    if not hasattr(o, "incidents"):
        return None
    q = o.queue
    items = []
    for i in q.items:
        items.append({"id": i.id, "agent": i.agent, "action": i.action, "decision_ids": i.decision_ids, "role": i.role, "rung": i.rung,
                      "pax": i.pax, "asked_at": i.asked_at.isoformat(), "deadline": i.deadline.isoformat(), "status": i.status,
                      "kind": i.kind, "members": len(i.members), "escalated_to": i.alerted, "incident_id": i.incident_id,
                      "answered_by": i.answered_by, "facts": i.facts,
                      "waited_min": round((i.answered_at - i.asked_at).total_seconds() / 60, 1) if i.answered_at else None})
    by_hour = {}
    for i in q.items:
        if i.status == "refused_blocked":
            continue
        h = i.asked_at.strftime("%H:00")
        by_hour.setdefault(i.role, {}).setdefault(h, 0)
        by_hour[i.role][h] += 1
    limit = o.os["queue"]["workload_max_per_hour"]
    tags = {}
    for t in o.tags:
        tags.setdefault(t["incident_id"], {}).setdefault(t["kind"], 0)
        tags[t["incident_id"]][t["kind"]] += 1
    for i in q.items:
        tags.setdefault(i.incident_id, {}).setdefault("queue_item", 0)
        tags[i.incident_id]["queue_item"] += 1
    incidents = [{"id": i.id, "kind": i.kind, "rank": i.rank, "status": i.status, "opened_at": i.opened_at.isoformat(),
                  "recovery_at": i.recovery_at.isoformat() if i.recovery_at else None,
                  "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None, "situation_versions": i.situation_versions,
                  "work": tags.get(i.id, {})} for i in o.incidents.all]
    return {"connections_1415": connections(o.log, "14:15"), "queue": q.summary(), "queue_items": items,
            "by_role_hour": by_hour, "workload_limit": limit,
            "workload_flags": [{"role": r, "hour": h, "items": n} for r, hs in by_hour.items() for h, n in sorted(hs.items()) if n > limit],
            "conflicts": o.conflicts, "blocked": o.blocked, "stale": o.findings, "incidents": incidents,
            "untagged": tags.get(None, {}), "minutes_logged": len(o.log)}
