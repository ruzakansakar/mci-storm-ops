"""Cross-agent conflicts (shared-state rule 7), checked by the orchestrator every minute after the agents ran.

Each function takes plain facts (what each agent published or proposed) and returns conflicts: both views, who is asked, and
which proposals are blocked. Blocking is only ever of the newer proposal that is still waiting; nothing already applied is
undone, and the orchestrator never picks a side for a person (it logs both views and asks).

  rebooking_on_cancelled       Passenger Care advises a flight Network Recovery proposes to cancel: the advice is blocked.
  departure_without_capacity   Network Recovery proposes a departure where Station & Staff has no ramp crew / gate agents left
                               (more departures in a slot than it can work) or while the ramp is closed: escalated.
  draft_contradicts_plan       a Communication draft states a change the latest recovery plan no longer has: blocked (the
                               send check would also refuse it).
  resource_used_twice          the same crew, aircraft, gate or staff member used for two things at once: the newer pending
                               proposal is blocked; if both are already applied, a person is asked.
"""
from datetime import datetime


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def rebooking_on_cancelled(advice, cancels):
    """advice: [{agent, rec_id, flight_id, passenger_ids}] (pending rebooking proposals); cancels: {flight_id: plan version}."""
    out = []
    for a in advice:
        if a["flight_id"] in cancels:
            out.append({"kind": "rebooking_on_cancelled", "key": ("rebook", a["rec_id"], a["flight_id"]),
                        "detail": f"Passenger Care advises {a['flight_id']}, which Network Recovery proposes to cancel",
                        "views": {"passenger_care": {"recommendation": a["rec_id"], "rebook_to": a["flight_id"],
                                                     "passengers": a.get("passenger_ids", [])},
                                  "network_recovery": {"cancel": a["flight_id"], "plan_version": cancels[a["flight_id"]]}},
                        "block": [("passenger_care", a["rec_id"])], "pax": len(a.get("passenger_ids") or [])})
    return out


def departure_without_capacity(delays, station, plan_version=None):
    """delays: the latest recovery plan's delays; station: the latest station plan's facts (ramp_status, staff_shortages)."""
    if not station:
        return []
    out, ramp = [], station.get("ramp_status") or {}
    reopen = _dt(ramp.get("expected_reopen")) if ramp.get("open") is False and ramp.get("expected_reopen") else None
    caps = {}
    for s in station.get("staff_shortages") or []:
        if s.get("departures_workable") is None:
            continue
        a = _dt(s["from"])
        caps[a] = (min(caps.get(a, (10 ** 6, None))[0], int(s["departures_workable"])), _dt(s["to"]), s["role"])
    used = {}
    for d in sorted(delays, key=lambda d: (d["new_departure"], d["flight_id"])):
        t = _dt(d["new_departure"])
        why = None
        if reopen and t < reopen:
            why = f"the ramp is closed until about {reopen:%H:%M}"
        slot = next((a for a, (n, b, _) in caps.items() if a <= t < b), None)
        if slot is not None:
            used[slot] = used.get(slot, 0) + 1
            if used[slot] > caps[slot][0]:
                why = f"Station & Staff can work {caps[slot][0]} departure(s) {slot:%H:%M}-{caps[slot][1]:%H:%M}"
        if why:
            out.append({"kind": "departure_without_capacity", "key": ("dep", d["flight_id"], d["new_departure"]),
                        "detail": f"{d['flight_id']} proposed for {t:%H:%M} but {why}",
                        "views": {"network_recovery": {"flight_id": d["flight_id"], "new_departure": d["new_departure"],
                                                       "plan_version": plan_version},
                                  "station_staff": {"ramp_status": ramp, "slot": slot.isoformat() if slot else None,
                                                    "departures_workable": caps[slot][0] if slot else None}},
                        "block": [], "pax": d.get("pax", 0)})
    return out


def draft_contradicts_plan(drafts, directives, plan_version):
    """drafts: Communication's recovery drafts still drafted/held; directives: {proposal_id: change} in the latest plan."""
    out = []
    for d in drafts:
        if d.get("status") not in ("drafted", "held"):
            continue
        now = directives.get(d["proposal_id"])
        if now is not None and now == d.get("change"):
            continue
        out.append({"kind": "draft_contradicts_plan", "key": ("draft", d["id"]),
                    "detail": f"draft {d['id']} ({d.get('audience')}) says '{d.get('text')}' but plan v{plan_version} "
                              + ("no longer has proposal " + str(d["proposal_id"]) if now is None else "changed it"),
                    "views": {"communication": {"draft": d["id"], "change": d.get("change"), "plan_version": d.get("plan_version")},
                              "network_recovery": {"plan_version": plan_version, "change": now}},
                    "block": [("communication_draft", d["id"])], "pax": 1 if d.get("audience") == "passenger" else 0})
    return out


def resource_used_twice(kind, uses):
    """uses: [{resource, flight_id, from, to, agent, rec_id, applied}] (from/to None = the whole plan). Two different flights
    (or tasks) holding the same resource at overlapping times."""
    out, seen = [], set()
    rows = sorted(uses, key=lambda u: (str(u["resource"]), str(u.get("from")), str(u["rec_id"])))
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if b["resource"] != a["resource"]:
                break
            if (a.get("flight_id"), a.get("task")) == (b.get("flight_id"), b.get("task")) and a.get("flight_id") is not None:
                continue
            if a.get("from") is not None and b.get("from") is not None and not (_dt(a["from"]) < _dt(b["to"]) and _dt(b["from"]) < _dt(a["to"])):
                continue
            key = (kind, a["resource"], a.get("flight_id"), b.get("flight_id"), str(a.get("from")), str(b.get("from")))
            if key in seen:
                continue
            seen.add(key)
            newer = max((a, b), key=lambda u: (not u.get("applied"), u["rec_id"] or 0))
            block = [(newer["agent"], newer["rec_id"])] if not newer.get("applied") and newer.get("rec_id") is not None else []
            out.append({"kind": "resource_used_twice", "key": key, "resource_kind": kind,
                        "detail": f"{kind} {a['resource']} used for {a.get('flight_id')} and {b.get('flight_id')} at the same time",
                        "views": {a["agent"]: {k: a.get(k) for k in ("flight_id", "from", "to", "rec_id", "applied")},
                                  (b["agent"] if b["agent"] != a["agent"] else b["agent"] + " (other)"):
                                      {k: b.get(k) for k in ("flight_id", "from", "to", "rec_id", "applied")}},
                        "block": block, "pax": 0})
    return out
