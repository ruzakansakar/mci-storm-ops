"""Measuring the Network Recovery agent (Phase 4d, advisory) against what today's process actually did. Measurement only: it
reads the simulator's record of the day (the answer key) and the agent's warnings, plans and rule-check results. Nothing here
goes back to an agent."""
from datetime import datetime

from backend.agents.network_recovery.plan import estimate_stranded
from simulator.clock import at


def _min(iso_or_dt, t_minute):
    """Minutes from an agent time (ISO) to a simulator minute (positive = the agent was earlier)."""
    a = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    return int((at(t_minute) - a).total_seconds() // 60)


def network_metrics(world, copilot, data) -> dict | None:
    nr = getattr(copilot, "network", None)
    if nr is None:
        return None
    mem, ev = nr.memory, world.events
    # crew time-outs that really happened today: a crew ran short and was replaced by a reserve, or the flight was cancelled
    real = [(e["t"], e["payload"]["flight_id"], e["payload"].get("role"), e["type"]) for e in ev
            if e["type"] in ("crew_out_of_duty", "reserve_crew_assigned") and (e["payload"] or {}).get("flight_id")
            and not e.get("overlay")]
    crew = []
    for t, fid, role, kind in real:
        w = mem.warned_crew.get((fid, role))
        lead = _min(w["first_at"], t) if w else None
        crew.append({"flight_id": fid, "role": role, "what_happened": "cancelled (no reserve)" if kind == "crew_out_of_duty"
                     else "replaced by a reserve", "at": f"{t // 60:02d}:{t % 60:02d}",
                     "first_warning": w["first_at"][11:16] if w else None, "lead_min": lead if lead is not None and lead >= 0 else None,
                     "best_option": next((x["best"] for p in reversed(mem.plans) for x in p["crew_actions"]
                                          if x["flight_id"] == fid and x["role"] == role), None)})
    diversions = [(e["t"], e["payload"]["flight_id"], e["payload"].get("diverted_to")) for e in ev if e["type"] == "flight_diverted"
                  and (e["payload"] or {}).get("flight_id") and not e.get("overlay")]
    fuel = []
    for t, fid, to in diversions:
        w = mem.warned_fuel.get(fid)
        lead = _min(w["at"], t) if w else None
        fuel.append({"flight_id": fid, "diverted_at": f"{t // 60:02d}:{t % 60:02d}", "to": to,
                     "first_warning": w["at"][11:16] if w else None, "lead_min": lead if lead is not None and lead >= 0 else None,
                     "must_leave_by": w["must_leave_by"][11:16] if w else None,
                     "safe_alternates": [a["airport"] for a in w["safe_alternates"]] if w else []})
    blocks = [v for v in nr.verdicts if v["verdict"]["result"] != "pass"]
    # the plan's cancellations vs today's, with the same stranded estimate for both
    seats = {r["destination"]: r.get("open_seats_later") or 0 for r in data["later_capacity"]}
    flights = {f["id"]: f for f in data["flights"]}
    today_c = [e["payload"]["flight_id"] for e in ev if e["type"] == "flight_cancelled" and (e["payload"] or {}).get("flight_id")
               and not e.get("overlay")]     # what the world really did (not the messy/fake reports chaos days inject)
    proposed = []
    for v in nr.verdicts:
        p = v["proposal"]
        if p["action"] == "cancel_flight" and p.get("flight_id") and p["flight_id"] not in proposed:
            proposed.append(p["flight_id"])
    stop_plans = [p for p in mem.plans if p["cancels"] or p["delays"]]
    final = stop_plans[-1] if stop_plans else None
    plan_c = [c["flight_id"] for c in final["cancels"]] if final else []
    saved = sorted({c["flight_id"] for c in crew if c["what_happened"].startswith("cancelled") and c["best_option"] not in (None, "none")})

    def est(ids):
        return estimate_stranded([(i, flights[i]["pax_total"] or 0, flights[i]["destination"]) for i in ids if i in flights], seats)
    return {
        "crew_timeouts": crew, "fuel_diversions": fuel,
        "proposals": len(nr.verdicts), "illegal_proposals": len(blocks),
        "blocked_reasons": [b["verdict"]["reasons"][:2] for b in blocks][:5],
        "by_action": {a: sum(1 for v in nr.verdicts if v["proposal"]["action"] == a) for a in sorted({v["proposal"]["action"] for v in nr.verdicts})},
        "plan_versions": mem.version, "errors": len(nr.errors),
        "today": {"cancelled": today_c, "estimate": est(today_c),
                  "actual_passengers_stranded_overnight": world.overnight_pax},
        "plan": {"final_in_stop_at": final["planned_at"][11:16] if final else None, "cancel": plan_c,
                 "ever_proposed_to_cancel": proposed, "delays": final["delays"] if final else [],
                 "crew_saves": saved, "estimate": est(plan_c)},
        "warnings_issued": {"crew": len(mem.warned_crew), "fuel": len(mem.warned_fuel)},
        "plans": [{k: p[k] for k in ("version", "planned_at", "cancels", "delays", "crew_actions", "hold_divert", "estimates")}
                  for p in mem.plans],
    }
