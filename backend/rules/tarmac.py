"""The tarmac helper: for a plane sitting on the ground with passengers, what deadlines apply and what is in the way of getting people off.

It NEVER decides to keep people on board. When something blocks deplaning and the time to start returning has come, it says
"captain_decides": a safety or air-traffic exception would be needed (14 CFR 259.4(c)(3)), and only the captain can make that call.
"""
from backend.rules.crew import to_min
from backend.rules.safety import limits_for


def tarmac_timeline(plane: dict, rules: dict) -> dict:
    """plane keys:
      kind                 'departure' | 'arrival' | 'diversion'
      door_close, touchdown  minutes (the clock starts at door close for departures, at touchdown for arrivals and diversions;
                             a diversion that already let passengers off and re-boarded is a departure: pass kind='departure')
      now                  minutes
      international        True/False
      gate_available, stairs_available, staff_available, ramp_open        True/False
      station_has_customs  True/False (international flights need customs where passengers get off)
      tsa_checkpoint_open  True/False (needed when people get off by stairs, outside the secure area)
      wheelchair, wheelchair_staff, child_alone, child_alone_staff        counts
    """
    lim = limits_for(rules)
    tm = lim["tarmac"]
    kind = plane["kind"]
    start = plane["door_close"] if kind == "departure" else plane["touchdown"]
    limit = lim["tarmac_international_min"] if plane.get("international") else lim["tarmac_domestic_min"]
    now = plane["now"]
    elapsed = now - start
    deadlines = {
        "status_announcement": start + tm["status_announcement_min"],
        "food_and_water": start + lim["tarmac_food_water_min"],
        "start_return_by": start + limit - tm["start_return_before_limit_min"],
        "limit": start + limit,
    }

    use_gate = bool(plane.get("gate_available") and plane.get("staff_available"))
    use_stairs = bool(plane.get("stairs_available") and plane.get("staff_available") and plane.get("ramp_open", True))
    blockers, partial = [], []
    if not plane.get("staff_available"):
        blockers.append("no ground staff to receive the plane")
    if not (plane.get("gate_available") or plane.get("stairs_available")):
        blockers.append("no gate and no stairs available")
    elif not use_gate and plane.get("stairs_available") and not plane.get("ramp_open", True):
        blockers.append("the ramp is closed for lightning, so stairs can't be used")
    if plane.get("international") and not plane.get("station_has_customs"):
        blockers.append("international passengers can't get off where there is no customs")
    if use_stairs and not use_gate and not plane.get("tsa_checkpoint_open", True):
        blockers.append("passengers would get off outside the secure area and the TSA checkpoint is closed")
    if plane.get("wheelchair", 0) and plane.get("wheelchair_staff", 0) < 1:
        partial.append(f"{plane['wheelchair']} wheelchair passengers need help getting off and none is available")
    if plane.get("child_alone", 0) and plane.get("child_alone_staff", 0) < 1:
        partial.append(f"{plane['child_alone']} children flying alone need a Southwest staff member and none is available")

    can_deplane = not blockers
    due = now >= deadlines["start_return_by"]
    if can_deplane:
        decision, message = "deplane_when_possible", "People can get off. Start deplaning (a human approves the action)."
    elif due:
        decision = "captain_decides"
        message = ("Something blocks getting people off and it is time to start returning. Keeping people on board would need a "
                   "safety or air-traffic exception. Only the captain decides that, and it must be logged.")
    else:
        decision, message = "not_yet", "Something blocks getting people off. Work on the blockers before the time to start returning."
    return {
        "kind": kind, "clock_starts_at": "door_close" if kind == "departure" else "touchdown",
        "clock_start": start, "elapsed_min": elapsed, "limit_min": limit, "deadlines": deadlines,
        "past": {k: now >= v for k, v in deadlines.items()},
        "always": ["working toilets", "medical attention if needed"],
        "notify_each_time_deplaning_is_possible": kind in ("departure", "diversion"),
        "dot_report_due_within_days": tm["dot_report_days"] if elapsed > limit else None,
        "blockers": blockers, "partial_blockers": partial,
        "can_deplane": can_deplane, "decision": decision, "message": message,
        "human_only_exceptions": ["pilot-in-command: safety or security", "air traffic control: would significantly disrupt the airport"],
    }
