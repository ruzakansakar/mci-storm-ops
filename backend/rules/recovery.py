"""When are operations at MCI back after an incident, and when is its aftermath cleared?

ONE definition, used by the Situation agent (on what its feeds have told it) and by the test judge (on what really happened), so
the two can't disagree about what "back" means. Pure functions on plain lists; no database, no settings file of its own. The one
number, quiet_min, is the rule `operations_back_quiet_min` in config/rules/operations.yaml.

Operations are BACK at time t when all of these hold:
  1. no FAA ground stop or ground delay program is active;
  2. the ramp is open;
  3. no flight is holding;
  4. every feed is healthy and no system is reported down (and no airport or police notice is still open);
  5. no flight has been newly delayed or cancelled in the last quiet_min minutes.
Items 1-4 are "blocks" (intervals); item 5 is "activity" (moments). A busy spell is a run of blocks and activity with no gap of
back-time between them: a block keeps operations from being back until it ends, a new delay or cancellation until quiet_min after it.

The aftermath (recovery -> normal) is cleared when no cancelled-flight passengers are still without a rebooking and no aircraft
is out of position. Passengers stranded overnight keep it in recovery until the orchestrator closes the incident (A10).
"""
from datetime import datetime, timedelta


def busy_spells(t: datetime, blocks: list, activity: list, quiet_min: float) -> list[dict]:
    """The busy spells up to t, in order. blocks: [(start, end or None, why)]; activity: [(time, why)].
    Each spell: {"start", "back_at" (None while still busy at t), "blocks": [...], "activity": [...]}."""
    quiet = timedelta(minutes=quiet_min)
    items = [(s, e, "block", why) for s, e, why in blocks if s <= t] + [(x, x, "activity", why) for x, why in activity if x <= t]
    items.sort(key=lambda i: (i[0], i[2]))
    spells = []
    for start, end, kind, why in items:
        back = (end if end is not None and end <= t else None) if kind == "block" else end + quiet
        if spells and (spells[-1]["_back"] is None or start <= spells[-1]["_back"]):     # at that very minute: not back yet
            cur = spells[-1]
            cur["_back"] = None if cur["_back"] is None or back is None else max(cur["_back"], back)
        else:
            cur = {"start": start, "_back": back, "blocks": [], "activity": []}
            spells.append(cur)
        (cur["blocks"] if kind == "block" else cur["activity"]).append((start, end, why) if kind == "block" else (start, why))
    for s in spells:
        b = s.pop("_back")
        s["back_at"] = b if b is not None and b <= t else None
    return spells


def operations_back(t: datetime, blocks: list, activity: list, quiet_min: float) -> bool:
    spells = busy_spells(t, blocks, activity, quiet_min)
    return not spells or spells[-1]["back_at"] is not None


def why_not_back(t: datetime, blocks: list, activity: list, quiet_min: float) -> list[str]:
    """Plain reasons operations are not back at t (empty if they are)."""
    out = [why for s, e, why in blocks if s <= t and (e is None or e > t)]
    recent = [x for x, _ in activity if t - timedelta(minutes=quiet_min) < x <= t]
    if recent:
        out.append(f"{len(recent)} flight(s) newly delayed or cancelled in the last {quiet_min:g} min")
    return out


def aftermath(unrebooked_passengers: int | None, aircraft_out_of_position: list, stranded_overnight: int,
              incident_closed: bool = False) -> list[str]:
    """What still keeps the status at 'recovery' (empty = the aftermath is cleared). None for the passengers means not known."""
    out = []
    if unrebooked_passengers is None or unrebooked_passengers > 0:
        out.append("cancelled-flight passengers still to rebook" if unrebooked_passengers is None else
                   f"{unrebooked_passengers} cancelled-flight passengers without a rebooking")
    if aircraft_out_of_position:
        out.append(f"{len(aircraft_out_of_position)} aircraft out of position")
    if stranded_overnight and not incident_closed:
        out.append(f"{stranded_overnight} passengers stranded overnight (until the incident is closed, A10)")
    return out
