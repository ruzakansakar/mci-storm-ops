"""Network Recovery -> Communication: prepare now, approve and verify before sending.

This module reads the shared plan and published application confirmations only
through AgentContext. It never calls Network Recovery or changes its plans.
"""
from dataclasses import dataclass, field
from copy import deepcopy
from datetime import timedelta

from backend.rules.recovery_message import directives, draft_problems, send_problems, text_for


@dataclass
class RecoveryMemory:
    drafts: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    view: dict | None = None
    world: dict = field(default_factory=dict)
    applications: dict = field(default_factory=dict)
    crews: dict = field(default_factory=dict)
    read_at: object = None
    told: set = field(default_factory=set)          # (recipient, text) already delivered: never told the same thing twice


def _record(ctx, memory, item, status, reason):
    if item.get("status") == status and item.get("reason") == reason:
        return
    item.update(status=status, reason=reason, as_of=ctx.now.isoformat())
    payload = deepcopy(item)
    memory.history.append(payload)
    ctx.record_event("recovery_message_" + {"held": "held", "sent": "sent", "discarded": "discarded"}[status], payload)


def _world(ctx, result, view, situation, memory, settings, screens):
    st = result["state"]
    beat = ctx.producer_heartbeat("network_recovery")
    fresh = beat is not None and (ctx.now - beat).total_seconds() <= settings["recovery"]["producer_max_age_min"] * 60
    events = ctx.read_events(delivered_since=memory.read_at - timedelta(microseconds=1) if memory.read_at else None)
    applications, crews = memory.applications, memory.crews
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        p = e.get("payload") or {}
        if e["type"] == "recovery_action_applied":
            if p.get("proposal_id") is None:
                continue
            c = {k: v for k, v in p.items() if k != "plan_version"}
            applications[p["proposal_id"]] = {"plan_version": p.get("plan_version"), "change": c}
            if c.get("crew_id"):
                crews[(c.get("flight_id"), c.get("role"))] = c["crew_id"]
        elif e["type"] in ("reserve_crew_assigned", "crew_reassigned"):
            crews[(p.get("flight_id"), p.get("role"))] = p.get("reserve_id") or p.get("crew_id")
    memory.read_at = ctx.now
    flights = {fid: {"status": "cancelled" if f.cancelled_at else "arrived" if f.arrived else "departed" if f.done else "waiting",
                     "departure": f.est_dep.isoformat()} for fid, f in st.flights.items()}
    trips = {pid: {"itinerary": list(t["itinerary"]), "status": t["status"]} for pid, t in st.truth.items()}
    # a Customer is told about a flight only while it is on their CURRENT trip (not every flight ever on it: after a rebooking
    # they must never hear about the flight they left)
    passenger_flights = {pid: set(t["itinerary"]) for pid, t in st.truth.items()}
    return {"now": ctx.now, "recovery_view": view, "recovery_fresh": fresh, "recovery_applications": applications,
            "recovery_flights": flights, "recovery_crews": crews, "recovery_trips": trips, "screens": screens,
            "recovery_passenger_flights": passenger_flights, "open_conflicts": set(situation.conflicts),
            "truth_feed_fresh": ctx.feed_age_sec("reservations") <= ctx.contract.max_data_age_sec,
            "crew_feed_fresh": ctx.feed_age_sec("crew_ops") <= ctx.contract.max_data_age_sec,
            "flight_feed_fresh": ctx.feed_age_sec("flight_ops") <= ctx.contract.max_data_age_sec}


def prepare(ctx, result, situation, memory, settings, screens):
    view = ctx.read_shared("recovery_plans")
    memory.view = view
    world = _world(ctx, result, view, situation, memory, settings, screens)
    memory.world = world
    changes = directives(view, world["recovery_flights"])
    approvals = {a["proposal_id"]: a for a in ((view or {}).get("facts") or {}).get("approval_status") or []}
    dropped = {receipt[4]: receipt[5] for receipt in getattr(result["state"], "message_receipts", [])
               if receipt[1] == "message_dropped"}
    for item in list(memory.drafts.values()):
        if item["status"] == "sent" and item["id"] in dropped:
            _record(ctx, memory, item, "discarded", dropped[item["id"]] or "dropped before delivery")
        if item["status"] in ("sent", "discarded"):
            continue
        change, approval = changes.get(item["proposal_id"]), approvals.get(item["proposal_id"], {})
        if change is None or approval.get("status") in ("rejected", "superseded", "blocked"):
            _record(ctx, memory, item, "discarded", approval.get("status") or "superseded")
        elif draft_problems(item, change):
            _record(ctx, memory, item, "discarded", "proposal facts changed; redrafting")
        elif item["audience"] == "passenger" and item.get("trip_facts") != world["recovery_trips"].get(item["passenger_id"]):
            _record(ctx, memory, item, "discarded", "trip facts changed; redrafting")

    for pid, change in sorted(changes.items()):
        approval = approvals.get(pid, {})
        if approval.get("status") in ("rejected", "superseded", "blocked") or not approval.get("plan_version"):
            continue
        version = approval["plan_version"]
        recipients = [("passenger", pax) for pax, fs in sorted(world["recovery_passenger_flights"].items()) if change["flight_id"] in fs]
        if change.get("crew_id"):
            recipients.append(("crew", change["crew_id"]))
        recipients.append(("staff", "internal"))
        for audience, recipient in recipients:
            key = (pid, audience, recipient)
            old = memory.drafts.get(key)
            if old and (old["status"] != "discarded" or old["reason"] not in ("proposal facts changed; redrafting", "trip facts changed; redrafting")):
                continue
            if audience == "passenger":
                channel = next((ch for ch in settings["messages"]["channel_order"] if ch in screens.get(recipient, ())), None)
                if not channel:
                    continue
            else:
                channel = audience
            revision = old.get("revision", 0) + 1 if old else 1
            item = {"id": f"N{pid}-{audience}-{recipient}-r{revision}", "type": "recovery_plan", "proposal_id": pid, "plan_version": version,
                    "observed_plan_version": view["version"], "revision": revision, "audience": audience, "channel": channel,
                    "passenger_id": recipient if audience == "passenger" else None, "crew_id": recipient if audience == "crew" else None,
                    "flight_id": change["flight_id"], "change": deepcopy(change), "text": text_for(change, audience),
                    "created": ctx.now.isoformat(), "decision_ids": ["O09"] if audience == "crew" else ["O08"] if audience == "staff" else ["O05"],
                    "sources": [{"source": "network_recovery", "as_of": view["time"].isoformat()}],
                    "internal": audience == "staff", "status": "drafted"}
            if audience == "passenger":
                item["trip_facts"] = deepcopy(world["recovery_trips"][recipient])
            if draft_problems(item, change):
                continue
            memory.drafts[key] = item
            ctx.record_event("recovery_message_drafted", deepcopy(item))
            memory.history.append(deepcopy(item))
            if audience == "staff":
                _record(ctx, memory, item, "sent", "internal staff only; not a passenger instruction")
            else:
                _record(ctx, memory, item, "held", f"waiting for approval of plan v{version}, proposal {pid}")

    ready = []
    for item in memory.drafts.values():
        if item["status"] != "held":
            continue
        approval = approvals.get(item["proposal_id"], {})
        if (item.get("passenger_id") or item.get("crew_id"), item["text"]) in memory.told:
            _record(ctx, memory, item, "discarded", "already told exactly this")
            continue
        problems = send_problems(item, world)
        if problems:
            reason = "approved, waiting to take effect" if approval.get("status") == "approved" and "approved, waiting to take effect" in problems else problems[0]
            if approval.get("status") == "pending":
                reason = f"waiting for approval of plan v{item['plan_version']}, proposal {item['proposal_id']}"
            _record(ctx, memory, item, "held", reason)
        else:
            ready.append(item)
    return ready


def mark_sent(ctx, memory, item):
    memory.told.add((item.get("passenger_id") or item.get("crew_id"), item["text"]))
    _record(ctx, memory, item, "sent", "approved and confirmed in operational records")


def hold_for_supervisor(ctx, memory, item):
    _record(ctx, memory, item, "held", "operational change confirmed; waiting for O09 message approval")


def discard(ctx, memory, item, reason):
    _record(ctx, memory, item, "discarded", reason)
