"""Rebuild the state of MCI at a time T from the events up to T only.

The truth tables (flights, aircraft, stations) hold END-of-day values after a run, so the agent never uses them for state.
This module takes the published schedule and the events the agent was allowed to see (AgentContext already cut them at T)
and works out: flight status and estimated times, diversions, the ground stop and its announced end, the ramp, the terminal crowd.
"""
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
    cancel_reason: str | None = None
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
    ramp_closures: list = field(default_factory=list)  # (start, end or None, event ids)
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


def build_state(schedule: list[dict], events: list[dict], t: datetime) -> State:
    st = State(t=t)
    for f in schedule:
        if f["sched_dep"] is None or f["sched_dep"].astimezone(TZ).date() != SIM_DATE:
            continue                                           # only today's published schedule
        st.flights[f["id"]] = FlightState(f["id"], f["direction"], f["origin"], f["destination"], f["sched_dep"], f["sched_arr"],
                                          f.get("crew_id"))
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        if e["occurred_at"] > t:                               # (AgentContext already refuses these; belt and braces)
            continue
        _apply(st, e)
    return st


def _apply(st: State, e: dict):
    typ, p, when, eid = e["type"], e.get("payload") or {}, e["occurred_at"], e["id"]
    st.last_event_at[e.get("feed", "?")] = when
    program_event = typ.startswith(("ground_stop_", "ground_delay_"))
    if program_event and typ.endswith("_started"):
        if st.program and st.program.ended is None:
            st.past_programs.append(st.program)
        st.program = Program(p.get("program", typ.rsplit("_", 1)[0]), when, at_minute(p["expected_end"]), p.get("reason") or "")
        st.program.notices.append((eid, when, typ))
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
    elif typ == "ramp_reopened":
        st.ramp_open, st.ramp_closed_since, st.ramp_expected_open = True, None, None
        if st.ramp_closures and st.ramp_closures[-1][1] is None:
            st.ramp_closures[-1][1] = when
            st.ramp_closures[-1][2].append(eid)
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
            f.status, f.diverted_to = "diverted", p.get("diverted_to")
        elif typ == "flight_resumed":
            f.status = "resumed"
        elif typ == "flight_cancelled":
            f.status, f.cancel_reason = "cancelled", p.get("reason")
        elif typ == "tarmac_started":
            f.on_tarmac, f.tarmac_since = True, when
        elif typ == "tarmac_ended":
            f.on_tarmac = False
        elif typ == "flight_departed":
            f.status = "departed"
            if st.program and st.program.kind == "ground_stop" and st.program.ended is None and not st.ramp_open:
                st.departed_during_stop.append((f.id, eid, when))
        elif typ == "flight_arrived":
            f.status = "arrived"
