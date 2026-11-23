"""The rule check: plain code, no AI. The orchestrator runs it on every proposal before any human sees it.

check(proposal, pending_proposals, world) -> {result: "pass" | "block", reasons, rule_ids, needs_human, blocks}

An agent's own checks never count as the final word: only this result does. Missing or stale data is a block, never a pass.
Nobody can override a block here; only a named human, and only where a law rule allows an exception (see override.py).

proposal (a plain dict; keys that don't apply can be left out):
  agent, action, decision_ids, data_as_of (datetime), requested_level ("auto" if the agent wants it done automatically)
  flight_id                 the flight the action is about
  option                    the facts the safety ladder needs (same keys as safety.py: tarmac_min, crew, fa, staff, pax, ...)
  safety_rung               the highest-priority rung it touches
  assign                    {"crew_ids": [...], "aircraft_id": "..."} what it would use
  rebook_to, passengers     a flight passengers would be moved to, and how many
  offer                     {"kind": "hotel" | "compensation", "backed_by": <approved decision/recommendation id>, "items": [...]}
  owed_inputs               the passenger facts for compensation_for (to check an offer against the money rules)
  message                   {"text": ..., "claims": {flight_id: status}, "promises": [{"what": ..., "backed_by": id}]}
world: {"now": datetime, "flights": {id: {"status", "seats", "pax_total"}}, "approved": set of approved ids,
        "approved_actions": [{"action", "flight_id"}]}
"""
from datetime import datetime

from backend.rules import load_rules
from backend.rules.autonomy import autonomy_for
from backend.rules.compensation import compensation_for
from backend.rules.safety import CHECKS, limits_for

REQUIRED = ("agent", "action", "decision_ids", "data_as_of")
# Actions that cannot both happen to the same flight.
CONTRADICTORY = [{"delay_flight", "cancel_flight"}, {"cancel_flight", "swap_aircraft"}, {"cancel_flight", "start_deplaning"},
                 {"cancel_flight", "hold_departure_for_connections"}, {"delay_flight", "combine_flights"}]
CANCELLED = {"cancelled"}


def _rule_ids_by_check(rules: dict) -> dict[str, list[str]]:
    out = {}
    for e in rules["entries"]:
        if e.get("checked_by") and not e["id"].startswith("rung_"):
            out.setdefault(e["checked_by"], []).append(e["id"])
    return out


def _max_age(agent: str):
    from backend.contracts.loader import load_contracts
    c = load_contracts().get(agent)
    return c.max_data_age_sec if c else None


def check(proposal: dict, pending_proposals: list[dict] | None = None, world: dict | None = None, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    world = world or {}
    pending = [p for p in (pending_proposals or []) if p is not proposal and p.get("id") != proposal.get("id", object())]
    by_check = _rule_ids_by_check(rules)
    blocks = []

    def block(reason, rule_ids, human=None):
        blocks.append({"reason": reason, "rule_ids": list(rule_ids), "needs_human": human})

    action = proposal.get("action")
    known = action in rules["autonomy"]
    approver = rules["autonomy"][action].get("approver") if known else "station_manager"

    # ---- 1. missing or stale data is a block, never a pass
    missing = [k for k in REQUIRED if not proposal.get(k)]
    if missing:
        block(f"missing data: {', '.join(missing)}", ["missing_or_stale_data_blocks"], approver)
    now, as_of = world.get("now"), proposal.get("data_as_of")
    if as_of is not None:
        limit = _max_age(proposal.get("agent", ""))
        if not isinstance(as_of, datetime) or now is None:
            block("can't tell how old the data is", ["missing_or_stale_data_blocks"], approver)
        elif limit is not None and (now - as_of).total_seconds() > limit:
            block(f"data is {(now - as_of).total_seconds():.0f} s old; this agent may use data up to {limit} s old",
                  ["missing_or_stale_data_blocks"], approver)

    # ---- 2. the action and who may decide it
    rung = proposal.get("safety_rung")
    if action and not known:
        block(f"'{action}' is not a listed action, so only a human decides it", ["unlisted_action_is_human_only"], "station_manager")
    elif known:
        level = autonomy_for(action, rules, safety_rung=rung)
        if level["level"] == "human_only":
            block(f"'{action}' is a human-only decision ({level['approver']}); the co-pilot can only show facts",
                  [f"action_{action}"], level["approver"])
        elif proposal.get("requested_level") == "auto" and level["level"] != "auto":
            block(f"'{action}' can't be done automatically ({level['reason']})", [f"action_{action}", "never_auto_rungs"],
                  level["approver"])

    # ---- 3. the safety ladder (filter rungs) and every enforced rule that applies
    option = proposal.get("option")
    if option is not None:
        lim = limits_for(rules)
        for rung_def in rules["safety_ladder"]:
            if rung_def["kind"] != "filter":
                continue
            for name in rung_def["checks"]:
                why = CHECKS[name]({"id": proposal.get("id", "proposal"), **option}, lim)
                if why:
                    block(f"rung {rung_def['rung']} ({rung_def['name']}): {why}", by_check.get(name, []) or [rung_def and f"rung_{rung_def['rung']}"],
                          approver)

    # ---- 4. money: an offer can't give more than the rules allow
    if proposal.get("owed_inputs") is not None and (proposal.get("offer") or {}).get("items"):
        owed = compensation_for(proposal["owed_inputs"], rules)
        allowed = {i["item"]: i for i in owed["items"]}
        if owed.get("controllable") is None:
            block("the cause isn't classified yet, so what is owed can't be decided", ["uncontrollable_causes", "controllable_causes"],
                  "customer_service_supervisor")
        for item in proposal["offer"]["items"]:
            rule = allowed.get(item.get("item"))
            if rule is None:
                block(f"offer includes '{item.get('item')}', which the money rules don't give here", by_check.get("compensation_for", []),
                      "customer_service_supervisor")
            elif rule.get("usd") is not None and item.get("usd", 0) > rule["usd"]:
                block(f"offer of ${item['usd']} for '{item['item']}' is more than the ${rule['usd']} the rules give",
                      by_check.get("compensation_for", []), "customer_service_supervisor")

    # ---- 5. nothing may be promised that no approved decision backs
    approved = set(world.get("approved", ()))
    offer = proposal.get("offer")
    promises = list((proposal.get("message") or {}).get("promises", []))
    if offer and action not in ("issue_compensation", "hold_hotel_rooms", "book_crew_hotel"):
        promises.append({"what": f"{offer.get('kind', 'an offer')}", "backed_by": offer.get("backed_by")})
    for pr in promises:
        if pr.get("backed_by") not in approved:
            block(f"promises {pr.get('what')} but no approved decision backs it", ["offer_needs_approved_decision"],
                  "customer_service_supervisor")

    # ---- 6. a message must match the truth and the approved decisions
    flights = world.get("flights", {})
    approved_actions = world.get("approved_actions", [])
    for fid, claimed in ((proposal.get("message") or {}).get("claims") or {}).items():
        truth = flights.get(fid, {}).get("status")
        if truth is None:
            block(f"message talks about {fid} but its status is unknown", ["missing_or_stale_data_blocks", "message_matches_truth"],
                  "customer_service_supervisor")
        elif truth != claimed:
            block(f"message says {fid} is '{claimed}' but it is '{truth}'", ["message_matches_truth"], "customer_service_supervisor")
        for a in approved_actions:
            if a.get("flight_id") == fid and a.get("action") == "cancel_flight" and claimed != "cancelled":
                block(f"message says {fid} is '{claimed}' but cancelling it has been approved", ["message_matches_truth"],
                      "customer_service_supervisor")

    # ---- 7. conflicts with other pending proposals
    fid = proposal.get("flight_id")
    for other in pending:
        if fid and other.get("flight_id") == fid and {action, other.get("action")} in CONTRADICTORY:
            block(f"{action} and {other.get('action')} on {fid} can't both happen (other proposal from {other.get('agent')})",
                  ["proposal_conflicts_block"], "network_operations_control")
    target = proposal.get("rebook_to")
    if target:
        f = flights.get(target)
        if f is None:
            block(f"rebooking onto {target}, whose status is unknown", ["missing_or_stale_data_blocks"], approver)
        else:
            if f.get("status") in CANCELLED:
                block(f"rebooking onto {target}, which is cancelled", ["proposal_conflicts_block"], approver)
            if f.get("seats") is not None and f.get("pax_total", 0) + proposal.get("passengers", 1) > f["seats"]:
                block(f"rebooking {proposal.get('passengers', 1)} onto {target}, which has only "
                      f"{max(0, f['seats'] - f.get('pax_total', 0))} seats left", ["proposal_conflicts_block"], approver)
        for other in pending:
            if other.get("action") == "cancel_flight" and other.get("flight_id") == target:
                block(f"rebooking onto {target}, which another proposal ({other.get('agent')}) wants to cancel",
                      ["proposal_conflicts_block"], "network_operations_control")
        for a in approved_actions:
            if a.get("action") == "cancel_flight" and a.get("flight_id") == target:
                block(f"rebooking onto {target}, whose cancellation has been approved", ["proposal_conflicts_block"], approver)
    mine = proposal.get("assign") or {}
    for other in pending:
        theirs = other.get("assign") or {}
        if other.get("flight_id") == fid:
            continue
        for crew in set(mine.get("crew_ids", [])) & set(theirs.get("crew_ids", [])):
            block(f"crew {crew} is also assigned to {other.get('flight_id')} by another proposal", ["no_double_assignment"], "crew_scheduling")
        if mine.get("aircraft_id") and mine.get("aircraft_id") == theirs.get("aircraft_id"):
            block(f"aircraft {mine['aircraft_id']} is also assigned to {other.get('flight_id')} by another proposal",
                  ["no_double_assignment"], "network_operations_control")

    # ---- the verdict
    from backend.rules.channel_fix import reasons as channel_fix_reasons
    for reason in channel_fix_reasons(proposal, world):
        block(reason, ["message_matches_truth", "missing_or_stale_data_blocks"], "customer_service_supervisor")
    if blocks:
        rule_ids = []
        for b in blocks:
            rule_ids += [r for r in b["rule_ids"] if r and r not in rule_ids]
        return {"result": "block", "reasons": [b["reason"] for b in blocks], "rule_ids": rule_ids,
                "needs_human": next((b["needs_human"] for b in blocks if b["needs_human"]), approver), "blocks": blocks}
    level = autonomy_for(action, rules, safety_rung=rung)
    return {"result": "pass", "reasons": [], "rule_ids": [], "blocks": [],
            "needs_human": None if level["level"] == "auto" else level["approver"]}
