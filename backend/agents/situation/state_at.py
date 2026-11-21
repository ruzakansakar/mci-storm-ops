"""Rebuild the state of MCI at a time T from the events up to T only.

The truth tables (flights, aircraft, stations) hold END-of-day values after a run, so the agent never uses them for state.
This module takes the published schedule and the events the agent was allowed to see (AgentContext already cut them at T)
and works out: flight status and estimated times, diversions, the FAA program and its announced end, the ramp, airport and
police notices, system status, the terminal crowd.

Real feeds are messy, so it is defensive about its inputs (whatever the source):
  * an event seen twice (same type, time and content) counts once;
  * events are applied in the order they happened, even if one was delivered late;
  * an event whose payload is missing fields or has the wrong types is set aside and counted as unreadable, never guessed at.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.settings import SIM_DATE, TZ

DAY0 = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, tzinfo=TZ)


def at_minute(m: int) -> datetime:
    return DAY0 + timedelta(minutes=int(m))


def hhmm(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%H:%M") if dt else "-"


def parse_clock(text: str, ref: datetime | None = None) -> datetime:
    """'13:00' (or '06:30+1') on the simulated day."""
    day = 0
    if "+" in text:
        text, plus = text.split("+")
        day = int(plus)
    h, m = text.split(":")
    return DAY0 + timedelta(days=day, hours=int(h), minutes=int(m))


# What each event must contain to be usable: field -> allowed types. Anything else is "unreadable".
_INT, _STR = (int,), (str,)
REQUIRED = {
    "_program_start": {"expected_end": _INT},
    "_program_extend": {"expected_end": _INT},
    "ramp_closed": {},
    "flight_delayed": {"flight_id": _STR, "delay_min": _INT},
    "flight_delay_updated": {"flight_id": _STR, "delay_min": _INT},
    "flight_holding": {"flight_id": _STR},
    "flight_diverted": {"flight_id": _STR},
    "flight_resumed": {"flight_id": _STR},
    "flight_cancelled": {"flight_id": _STR, "reason": _STR},
    "tarmac_started": {"flight_id": _STR},
    "tarmac_ended": {"flight_id": _STR},
    "flight_departed": {"flight_id": _STR},
    "flight_arrived": {"flight_id": _STR},
    "station_status": {"people_in_terminal": _INT},
    "airport_ops_notice": {"kind": _STR},
    "police_tsa_notice": {"kind": _STR},
    "system_status_changed": {"system": _STR, "status": _STR},
}
OPTIONAL = {"expected_end": _INT, "new_departure": _STR, "pax": _INT, "expected_landing": _STR, "reason": _STR}


@dataclass
class Program:
    kind: str                       # ground_stop | ground_delay
    started: datetime
    announced_end: datetime
    reason: str
    extensions: int = 0
    shortened: int = 0
    ended: datetime | None = None
    notices: list = field(default_factory=list)      # (event id, time, type)

    @property
    def last_notice_at(self) -> datetime:
        return self.notices[-1][1] if self.notices else self.started


@dataclass
class Notice:
    source: str                     # airport_ops | police_tsa
    kind: str                       # power_outage | runway_closed | security_threat
    area: str | None
    since: datetime
    expected_end: datetime | None
    event: tuple
    cleared: datetime | None = None


@dataclass
class FlightState:
    id: str
    direction: str
    origin: str
    destination: str
    sched_dep: datetime
    sched_arr: datetime
    crew_id: str | None
    status: str = "scheduled"       # scheduled | delayed | holding | diverted | resumed | cancelled | departed | arrived
    est_dep: datetime | None = None
    est_landing: datetime | None = None
    delay_min: int = 0
    delay_reason: str | None = None
    diverted_to: str | None = None
    diverted_at: datetime | None = None
    cancel_reason: str | None = None
    cancelled_at: datetime | None = None
    pax: int | None = None
    on_tarmac: bool = False
    tarmac_since: datetime | None = None
    hold_since: datetime | None = None
    events: list = field(default_factory=list)       # (event id, time, type)


@dataclass
class State:
    t: datetime
    program: Program | None = None
    past_programs: list = field(default_factory=list)
    ramp_open: bool = True
    ramp_closed_since: datetime | None = None
    ramp_expected_open: datetime | None = None
    ramp_closures: list = field(default_factory=list)  # [start, end or None, event ids, reason]
    notices: list = field(default_factory=list)        # airport and police notices (active and cleared)
    systems: dict = field(default_factory=dict)        # system -> {"status", "since", "event"}
    flights: dict = field(default_factory=dict)
    crowd: dict | None = None
    crowd_at: datetime | None = None
    missed_connections: int = 0
    stranded_overnight: int = 0
    stranded_local: int = 0
    next_day_flights: int = 0
    departed_during_stop: list = field(default_factory=list)   # (flight id, event id, time) departures while ramp closed + stop on
    delay_reasons: dict = field(default_factory=dict)
    newly_delayed: list = field(default_factory=list)          # (time, flight id) departures that became delayed
    last_event_at: dict = field(default_factory=dict)          # feed -> time of its last event
    unreadable: list = field(default_factory=list)             # (event id, type, why) set aside
    duplicates: int = 0
    incident_seen: bool = False                                # something abnormal was signalled earlier today

    def active_notices(self, source=None):
        return [n for n in self.notices if n.cleared is None and (source is None or n.source == source)]


def build_state(schedule: list[dict], events: list[dict], t: datetime) -> State:
    st = State(t=t)
    for f in schedule:
        if f["sched_dep"] is None or f["sched_dep"].astimezone(TZ).date() != SIM_DATE:
            continue                                           # only today's published schedule
        st.flights[f["id"]] = FlightState(f["id"], f["direction"], f["origin"], f["destination"], f["sched_dep"], f["sched_arr"],
                                          f.get("crew_id"))
    seen = set()
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        if e["occurred_at"] > t:                               # (AgentContext already refuses these; belt and braces)
            continue
        key = (e["type"], e["occurred_at"], json.dumps(e.get("payload"), sort_keys=True, default=str))
        if key in seen:
            st.duplicates += 1
            continue
        seen.add(key)
        why = _unusable(e)
        if why:
            st.unreadable.append((e["id"], e["type"], why))
            continue
        try:
            _apply(st, e)
        except (KeyError, TypeError, ValueError, AttributeError) as err:     # never let one odd event break the picture
            st.unreadable.append((e["id"], e["type"], f"could not use it: {err}"))
    return st


def _unusable(e: dict) -> str | None:
    p = e.get("payload")
    if not isinstance(p, dict):
        return "payload is not a set of fields"
    typ = e["type"]
    rule = REQUIRED.get("_program_start" if typ.endswith("_started") and typ.startswith("ground_") else
                        "_program_extend" if typ.endswith("_extended") and typ.startswith("ground_") else typ, {})
    for k, types in rule.items():
        if k not in p:
            return f"missing '{k}'"
        if not isinstance(p[k], types) or isinstance(p[k], bool):
            return f"'{k}' has the wrong type"
    for k, types in OPTIONAL.items():
        if k in p and p[k] is not None and k not in rule and (not isinstance(p[k], types) or isinstance(p[k], bool)):
            return f"'{k}' has the wrong type"
    for k in ("new_departure", "expected_landing"):
        if isinstance(p.get(k), str):
            try:
                parse_clock(p[k])
            except ValueError:
                return f"'{k}' is not a time"
    return None


def _apply(st: State, e: dict):
    typ, p, when, eid = e["type"], e.get("payload") or {}, e["occurred_at"], e["id"]
    st.last_event_at[e.get("feed", "?")] = when
    program_event = typ.startswith(("ground_stop_", "ground_delay_"))
    if program_event and typ.endswith("_started"):
        if st.program:
            st.past_programs.append(st.program)
        st.program = Program(p.get("program", typ.rsplit("_", 1)[0]), when, at_minute(p["expected_end"]), p.get("reason") or "")
        st.program.notices.append((eid, when, typ))
        st.incident_seen = True
    elif program_event and typ.endswith("_extended") and st.program:
        new_end = at_minute(p["expected_end"])
        if new_end > st.program.announced_end:
            st.program.extensions += 1
        elif new_end < st.program.announced_end:
            st.program.shortened += 1
        st.program.announced_end = new_end
        st.program.reason = p.get("reason") or st.program.reason
        st.program.notices.append((eid, when, typ))
    elif program_event and typ.endswith("_ended") and st.program:
        st.program.ended = when
        st.program.notices.append((eid, when, typ))
    elif typ == "ramp_closed":
        st.ramp_open, st.ramp_closed_since = False, when
        st.ramp_expected_open = at_minute(p["expected_end"]) if p.get("expected_end") is not None else None
        st.ramp_closures.append([when, None, [eid], p.get("reason")])
        st.incident_seen = True
    elif typ == "ramp_reopened":
        st.ramp_open, st.ramp_closed_since, st.ramp_expected_open = True, None, None
        if st.ramp_closures and st.ramp_closures[-1][1] is None:
            st.ramp_closures[-1][1] = when
            st.ramp_closures[-1][2].append(eid)
    elif typ in ("airport_ops_notice", "police_tsa_notice"):
        source = "airport_ops" if typ == "airport_ops_notice" else "police_tsa"
        kind = p["kind"]
        if kind in ("power_restored", "runway_reopened", "all_clear"):
            opened = {"power_restored": "power_outage", "runway_reopened": "runway_closed", "all_clear": "security_threat"}[kind]
            for n in st.active_notices(source):
                if n.kind == opened and (p.get("area") is None or n.area == p.get("area")):
                    n.cleared = when
        else:
            end = at_minute(p["expected_end"]) if p.get("expected_end") is not None else None
            st.notices.append(Notice(source, kind, p.get("area"), when, end, (eid, when, typ)))
            st.incident_seen = True
    elif typ == "system_status_changed":
        st.systems[p["system"]] = {"status": p["status"], "since": when, "event": (eid, when, typ)}
        if p["status"] != "up":
            st.incident_seen = True
    elif typ == "station_status":
        st.crowd, st.crowd_at = dict(p), when
    elif typ == "missed_connection":
        st.missed_connections += 1
    elif typ == "passenger_stranded_overnight":
        st.stranded_overnight += 1
        st.stranded_local += 1 if p.get("lives_locally") else 0
    elif typ == "next_day_flight_created":
        st.next_day_flights += 1
    else:
        f = st.flights.get(p.get("flight_id"))
        if f is None:
            return
        f.events.append((eid, when, typ))
        if p.get("pax") is not None:
            f.pax = p["pax"]
        if typ in ("flight_delayed", "flight_delay_updated"):
            if f.status in ("scheduled", "delayed"):
                if f.status == "scheduled" and f.direction == "departure":
                    st.newly_delayed.append((when, f.id))
                f.status = "delayed"
            f.delay_min, f.delay_reason = p.get("delay_min", 0), p.get("reason")
            if p.get("new_departure"):
                f.est_dep = parse_clock(p["new_departure"])
            st.delay_reasons[p.get("reason") or "unknown"] = st.delay_reasons.get(p.get("reason") or "unknown", 0) + 1
        elif typ == "flight_holding":
            if f.status != "holding":
                f.hold_since = when
            f.status = "holding"
            if p.get("expected_landing"):
                f.est_landing = parse_clock(p["expected_landing"])
        elif typ == "flight_diverted":
            f.status, f.diverted_to, f.diverted_at = "diverted", p.get("diverted_to"), when
        elif typ == "flight_resumed":
            f.status = "resumed"
        elif typ == "flight_cancelled":
            f.status, f.cancel_reason, f.cancelled_at = "cancelled", p.get("reason"), when
        elif typ == "tarmac_started":
            f.on_tarmac, f.tarmac_since = True, when
        elif typ == "tarmac_ended":
            f.on_tarmac = False
        elif typ == "flight_departed":
            if f.status != "cancelled":
                f.status = "departed"
            if st.program and st.program.kind == "ground_stop" and st.program.ended is None and not st.ramp_open:
                st.departed_during_stop.append((f.id, eid, when))
        elif typ == "flight_arrived":
            f.status = "arrived"
