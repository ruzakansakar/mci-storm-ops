"""Seats at time T (fix-seats): ONE way to count them, shared by Network Recovery and Passenger Care (agents never import each
other, so the helper lives here, with the rules).

The reservations system publishes each flight's seat inventory as it changes (seats_updated: seats, booked, sellable = what
may still be sold under the airline's load limit). A seat is free for a rebooking if it is sellable now and not already
held or promised: seats Passenger Care has advised for passengers (pending or approved, not yet booked) and seats a planner
has already given away in the same plan. The published "open seats later today" figure (later_capacity) is never used here.
"""


def inventory(events) -> dict:
    """flight id -> {seats, booked, sellable, at}: the latest published inventory up to T (events already cut to T)."""
    out = {}
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        if e["type"] == "seats_updated":
            p = e.get("payload") or {}
            if p.get("flight_id"):
                out[p["flight_id"]] = {"seats": p.get("seats"), "booked": p.get("booked"), "sellable": p.get("sellable") or 0,
                                       "at": e["occurred_at"]}
    return out


def held_by_advice(advice) -> dict:
    """flight id -> seats promised by rebooking advice (each advised passenger holds one seat until booked or dropped)."""
    out = {}
    for a in advice or []:
        if a.get("flight_id"):
            out[a["flight_id"]] = out.get(a["flight_id"], 0) + len(a.get("passengers") or [])
    return out


def free(flight_id, inv: dict, held: dict | None = None) -> int:
    """Seats on this flight a rebooking can still use now (0 if its inventory is unknown: unknown is never counted as free)."""
    row = inv.get(flight_id)
    if not row:
        return 0
    return max(0, int(row["sellable"]) - int((held or {}).get(flight_id, 0)))


def later_free(candidates, inv: dict, held: dict | None = None) -> int:
    """Free seats across later flights a passenger could be moved to (candidates: their flight ids)."""
    return sum(free(fid, inv, held) for fid in candidates)


def stranded_if_cancelled(pax: int, candidates, inv: dict, held: dict | None = None) -> dict:
    """If this flight is cancelled, how many of its passengers find no seat on a later flight today (and would sleep over)."""
    fit = min(pax, later_free(candidates, inv, held))
    return {"passengers": pax, "rebooked_today": fit, "stranded": pax - fit}
