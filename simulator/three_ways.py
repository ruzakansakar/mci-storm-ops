"""Phase 5b: one summary per run, so today, copilot and orchestrated can be compared side by side (measurement only)."""
from simulator.clock import at


def _hm(t):
    return f"{t // 60:02d}:{t % 60:02d}" if isinstance(t, int) else None


def mode_summary(world, metrics, copilot, focus) -> dict:
    ev = world.events
    flights = world.flights
    delivered = [e for e in ev if e["type"] in ("message_delivered", "crew_message_delivered")]
    o = metrics.get("orchestrator") or {}
    peak = {r: max(h.values()) for r, h in (o.get("by_role_hour") or {}).items()}
    care = metrics.get("care") or {}
    fb = {}
    for f in getattr(world, "fallbacks", []):
        fb[f["process"]] = fb.get(f["process"], 0) + 1
    applied = [a for a in getattr(world, "applied", [])]
    # Maya's day: her trip as it ended, the flights she was on, and what she was told
    trip = world.truth.get(focus) or {}
    legs = []
    for fid in trip.get("itinerary") or []:
        f = flights.get(fid) or {}
        legs.append({"flight_id": fid, "status": f.get("status"), "left": _hm(f.get("actual_dep")), "arrived": _hm(f.get("actual_arr")),
                     "cancel_reason": f.get("cancel_reason")})
    told = [{"at": _hm(e["t"]), "type": e["payload"].get("type"), "text": e["payload"].get("text")}
            for e in delivered if e["payload"].get("passenger_id") == focus]
    return {"cancellations": metrics.get("flights_cancelled"), "delays": metrics.get("flights_delayed"),
            "diversions": metrics.get("flights_diverted"), "stranded_overnight": metrics.get("passengers_stranded_overnight"),
            "crew_legality_violations": len(getattr(world, "crew_violations", [])),
            "fallbacks": sum(fb.values()), "fallbacks_by_process": fb,
            "applied": sum(1 for a in applied if a["applied"]), "refused": sum(1 for a in applied if not a["applied"]),
            "wrong_screen_minutes": metrics.get("passenger_minutes_of_disagreement"),
            "messages_delivered": len(delivered), "promises_found": metrics.get("promises_found", 0),
            "approvals": (o.get("queue") or {}).get("by_status", {}).get("answered", 0),
            "busiest_role_per_hour": max(peak.items(), key=lambda kv: kv[1]) if peak else None,
            "hotel_rooms": care.get("rooms_assigned"), "care_cost_usd": (care.get("costs") or {}).get("committed_usd"),
            "ground_waits": len(getattr(world, "ground_waits", [])),
            "maya": {"status": trip.get("status"), "itinerary": list(trip.get("itinerary") or []), "legs": legs,
                     "message": trip.get("message"), "told": told}}
