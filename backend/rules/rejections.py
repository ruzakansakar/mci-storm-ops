"""A person's final "no" (live human-in-the-loop mode).

When the person in the loop rejects a proposal for good, a human_rejected event is recorded (actor "human:<role>") with the
proposal's action and its target (flight, crew member, passengers, ...). An agent that reads that event through its contract must
never propose the same action for the same target again; it comes back with a different plan if it has one.

One definition of "the same action for the same target", used by the live layer (backend/execution/live_human.py) and by the
agents. Nothing here is ever produced in today, copilot or orchestrated mode, so those modes are unchanged."""
TARGET_KEYS = ("flight_id", "crew_id", "passenger_ids", "hotel_id", "gate", "staff_id", "kind")


def target_of(facts: dict) -> dict:
    """The target of a proposal: which flight, crew member, passengers, hotel, gate or staff member it acts on."""
    out = {}
    for k in TARGET_KEYS:
        v = (facts or {}).get(k)
        if v in (None, "", []):
            continue
        out[k] = sorted(v) if isinstance(v, (list, tuple, set)) else v
    return out


def key_of(agent: str, action: str, target: dict) -> tuple:
    return (agent, action, tuple((k, tuple(v) if isinstance(v, list) else v) for k, v in sorted((target or {}).items())))


def final_rejections(events, agent: str) -> set:
    """(action, flight_id, crew_id) of every proposal of this agent a person rejected for good (empty in every other mode)."""
    out = set()
    for e in events or ():
        if e.get("type") != "human_rejected":
            continue
        p = e.get("payload") or {}
        if p.get("agent") != agent or not p.get("final"):
            continue
        t = p.get("target") or {}
        out.add((p.get("action"), t.get("flight_id"), t.get("crew_id")))
    return out
