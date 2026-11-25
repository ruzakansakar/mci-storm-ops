"""Rebuild, from the plan published before the day and the events up to time T only, what Network Recovery knows at T.

The plan: the schedule (with each flight's aircraft, crew and priority), the crew roster with each crew's duty time left at the
start of the day, the reserve list, the fleet, stations, later capacity and the booked counts per group. Then the events:
delays, cancellations, departures, take-offs (with spare fuel), holding, diversions, landings, reserve crews assigned.

A crew's duty clock: it starts the day with the roster's duty time left; while its departure waits past the scheduled time, the
waiting uses that time up (the airline's own rule: "waiting uses up duty time"). A reserve that takes over a role brings its own
clock. Nothing here comes from an end-of-day table.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.settings import SIM_DATE, TZ

DAY0 = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, tzinfo=TZ)


def parse_clock(text: str) -> datetime:
    """'13:00' (or '06:30+1') on the simulated day. (Its own copy: agents never import each other's code.)"""
    day = 0
    if "+" in text:
        text, plus = text.split("+")
        day = int(plus)
    h, m = text.split(":")
    return DAY0 + timedelta(days=day, hours=int(h), minutes=int(m))

FIELD = {"pilot": "pilot_duty_left_min", "fa": "fa_duty_left_min"}
KIND = {"pilot": "pilot_pair", "fa": "fa_team"}


@dataclass
class Flight:
    id: str
    direction: str
    origin: str
    destination: str
    sched_dep: datetime
    sched_arr: datetime
    aircraft_id: str | None
    crew_id: str | None
    priority: str | None
    pax: int
    est_dep: datetime | None = None
    status: str = "scheduled"          # scheduled | delayed | cancelled | departed | airborne | holding | diverted | resumed | arrived
    airborne_at: datetime | None = None
    fuel0: int | None = None           # spare holding fuel at take-off (published on flight_airborne)
    diverted_to: str | None = None
    diverted_at: datetime | None = None
    departed_at: datetime | None = None
    cancelled_at: datetime | None = None
    arrived_at: datetime | None = None
    reserves: dict = field(default_factory=dict)    # role -> (reserve id, its duty left when assigned, when)
    next_day: bool = False

    @property
    def block(self) -> int:
        return int((self.sched_arr - self.sched_dep).total_seconds() // 60)

    @property
    def waiting(self) -> bool:
        return self.status in ("scheduled", "delayed")


@dataclass
class NRState:
    t: datetime
    flights: dict = field(default_factory=dict)
    crews: dict = field(default_factory=dict)        # id -> roster row (start of the day)
    used_reserves: set = field(default_factory=set)
    timed_out: list = field(default_factory=list)    # (time, flight, crew, role) crew_out_of_duty seen
    extended: set = field(default_factory=set)
    seen: set = field(default_factory=set)


def build_state(schedule, crews, events, t) -> NRState:
    st = NRState(t=t)
    for f in schedule:
        st.flights[f["id"]] = Flight(f["id"], f["direction"], f["origin"], f["destination"], f["sched_dep"], f["sched_arr"],
                                     f.get("aircraft_id"), f.get("crew_id"), f.get("priority"), f.get("pax_total") or 0,
                                     est_dep=f["sched_dep"])
    st.crews = {c["id"]: dict(c) for c in crews}
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        if e["occurred_at"] <= t:
            _apply(st, e)
    return st


def _apply(st: NRState, e):
    typ, p, when = e["type"], e.get("payload") or {}, e["occurred_at"]
    if typ == "next_day_flight_created":
        fid, route = p.get("flight_id"), p.get("route") or ""
        if fid and "-" in route and isinstance(p.get("departs"), str) and fid not in st.flights:
            dep = parse_clock(p["departs"])
            o, d = route.split("-", 1)
            st.flights[fid] = Flight(fid, "departure", o, d, dep, dep, None, None, None, 0, est_dep=dep, next_day=True)
        return
    f = st.flights.get(p.get("flight_id"))
    if f is None:
        return
    if typ in ("flight_delayed", "flight_delay_updated"):
        if f.waiting:
            f.status = "delayed"
        if isinstance(p.get("new_departure"), str):
            try:
                f.est_dep = parse_clock(p["new_departure"])
            except ValueError:
                pass
    elif typ == "flight_cancelled":
        f.status, f.cancelled_at = "cancelled", f.cancelled_at or when
    elif typ == "flight_departed":
        f.status, f.departed_at = "departed", when
    elif typ == "flight_airborne":
        f.status, f.airborne_at = "airborne", when
        if p.get("fuel_minutes_extra") is not None:
            f.fuel0 = int(p["fuel_minutes_extra"])
    elif typ == "flight_holding":
        f.status = "holding"
    elif typ == "flight_diverted":
        f.status, f.diverted_to, f.diverted_at = "diverted", p.get("diverted_to"), when
    elif typ == "flight_resumed":
        f.status = "resumed"
    elif typ == "flight_arrived":
        f.status, f.arrived_at = "arrived", when
    elif typ == "reserve_crew_assigned":
        f.reserves[p.get("role")] = (p.get("reserve_id"), p.get("reserve_duty_left_min"), when)
        st.used_reserves.add(p.get("reserve_id"))
    elif typ == "crew_reassigned":                     # 5b: a crew swapped onto this flight (its own duty clock runs now)
        f.reserves[p.get("role")] = (p.get("crew_id"), p.get("reserve_duty_left_min"), when)
        st.used_reserves.add(p.get("crew_id"))
    elif typ == "crew_out_of_duty":
        st.timed_out.append((when, f.id, p.get("crew_id"), p.get("role")))
    elif typ == "pilot_duty_extended":
        st.extended.add(p.get("crew_id"))


# ---------------------------------------------------------------- the duty clock

def need_min(f: Flight, s: dict, rules_buffer: int) -> int:
    """Duty a crew needs left to fly this flight: the flight, one more leg (planning estimate) and the legal buffer."""
    return f.block + s["crew"]["onward_leg_min"] + rules_buffer


def clock_for(st: NRState, f: Flight, role: str):
    """(crew id, duty left at the scheduled departure, the time its waiting starts) for whoever flies this role now."""
    if role in f.reserves:
        rid, left, when = f.reserves[role]
        return rid, left, max(when, f.sched_dep)
    crew = st.crews.get(f.crew_id)
    if crew is None or crew.get(FIELD[role]) is None:
        return None
    return crew["id"], crew[FIELD[role]], f.sched_dep


def illegal_at(st: NRState, f: Flight, role: str, need: int) -> datetime | None:
    """When this role's crew no longer has enough duty left for the flight (if it keeps waiting)."""
    c = clock_for(st, f, role)
    if c is None:
        return None
    _, left, start = c
    return start + timedelta(minutes=left - need)


def left_at(st: NRState, f: Flight, role: str, when: datetime) -> int | None:
    c = clock_for(st, f, role)
    if c is None:
        return None
    _, left, start = c
    return int(left - max(0, (when - start).total_seconds() // 60))
