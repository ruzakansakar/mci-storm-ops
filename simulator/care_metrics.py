"""Measuring the Passenger Care agent (Phase 4e). Measurement only: it reads the co-pilot's care plans, its hotel ledger and the
rule-check results. Nothing here goes back to an agent."""


def care_metrics(world, copilot, data, focus) -> dict | None:
    care = getattr(copilot, "care", None)
    if care is None:
        return None
    plans = care.memory.plans
    hotels = {h["id"]: h for h in data["hotels"]}
    used = {}
    for r in care.ledger:
        u = used.setdefault(r["hotel_id"], {"held": 0, "released": 0, "assigned": 0})
        u[{"hold": "held", "release": "released", "assign": "assigned"}[r["kind"]]] += r["rooms"]
    hotels_used = [{"hotel_id": h, "name": hotels[h]["name"], "rooms_available": hotels[h]["rooms_available"], **u,
                    "within_available": u["held"] - u["released"] <= hotels[h]["rooms_available"]
                    and u["assigned"] <= u["held"]} for h, u in sorted(used.items())]
    # every passenger who ever had a care plan today, as of their latest entry
    latest = {}
    for p in plans:
        for e in p["passengers"]:
            latest[e["passenger_id"]] = {**e, "plan_version": p["version"], "situation_version": p["situation_version"],
                                         "recovery_plan_version": p["recovery_plan_version"]}
    overnight = [e for e in latest.values() if "overnight" in e["needs"]]
    vulnerable = {}
    for p in plans:
        for v in p["vulnerable"]:
            vulnerable[(v["passenger_id"], v["kind"])] = v
    wheel_requests = {r["passenger_id"]: r for p in plans for r in p["wheelchair_requests"]}
    segments = {x["id"]: x["segment"] for x in data["passengers"]}
    checks = []
    for (pid, kind), v in sorted(vulnerable.items()):
        e = latest.get(pid, {})
        if kind == "child_alone":
            ok = "staff supervision" in v["plan"] and not e.get("hotel_decision") and "nonstop" in v["rebooking_rule"]
        else:
            ok = (pid in wheel_requests and wheel_requests[pid]["max_unattended_min"] <= 30
                  and ("overnight" not in e.get("needs", []) or e.get("local") or e.get("accessible_room")))
        checks.append({"passenger_id": pid, "kind": kind, "plan": v["plan"], "follows_the_rules": ok})
    blocked = [v for v in care.verdicts if v["verdict"]["result"] != "pass"]
    recs = [r for (t, _), r in care.context.saved.items() if t == "recommendations"]
    pending = {}
    for r in recs:
        if r.get("status") == "pending":
            key = {"rebook_passenger": "rebooking advice (advisory, never applied)",
                   "supervise_child_alone": "child supervision (waiting for Station & Staff)"}.get(r.get("_action"), r.get("_action"))
            pending[key] = pending.get(key, 0) + 1
    waits = [v for v in (e.get("waited_min") for e in latest.values()) if v is not None]
    final = plans[-1] if plans else {}
    maya_msgs = [m for m in (copilot.messages.log if copilot.messages else []) if m["passenger_id"] == focus
                 and m["type"] in ("overnight_hotel", "overnight_home")]
    return {"overnight": [{k: e.get(k) for k in ("passenger_id", "local", "hotel_name", "hotel_status", "transport", "transport_status",
                                                  "compensation", "approved_by", "waited_min")} for e in overnight],
            "hotels_used": hotels_used, "rooms_held": sum(u["held"] - u["released"] for u in used.values()),
            "rooms_assigned": sum(u["assigned"] for u in used.values()),
            "transport": {k: sum(1 for e in overnight if (e.get("transport") or "").startswith(k))
                          for k in sorted({(e.get("transport") or "none").split(" (")[0] for e in overnight})},
            "costs": final.get("costs"), "vulnerable": checks,
            "vulnerable_on_affected_flights": sorted({pid for pid, e in latest.items() if segments.get(pid) in ("child_alone", "wheelchair")}),
            "rule_check_blocks": len(blocked), "applied_blocked": 0, "proposals": len(care.verdicts),
            "pending_at_day_end": pending, "approval_waits_min": waits, "plan_versions": care.memory.version,
            "maya": latest.get(focus), "maya_overnight_message": maya_msgs[0]["text"] if maya_msgs else None,
            "rebooking_advice": sorted({(a["flight_id"], tuple(a["passengers"])) for p in plans for a in p["rebooking_advice"]})}
