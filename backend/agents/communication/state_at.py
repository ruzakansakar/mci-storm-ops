"""Rebuild, from the events up to time T only, what is TRUE for each tracked passenger and what each channel SHOWS them.

Truth: the reservation system's trip record (trip_record_updated: itinerary, status, destination, bag routing). The latest one
at or before T is the truth at T. The end-of-day tables (bookings, channel_views) are never used.
Channels: every channel starts the day showing the start-of-day trip record (assumption: the overnight sync). After that a
channel changes only when one of its own updates arrives (channel_view_updated): a "main" update sets itinerary, status and
destination, a "bag" update sets the bag routing. A delivery receipt (channel_update_failed) says an update did not arrive.

A mismatch on (passenger, channel, field) starts at the end of the minute when the channel stops matching the truth (the truth
changed and the channel didn't follow) and ends at the end of the minute it matches again. All events of one minute are applied
first, then the comparison is made, so the same events always give the same stretches, whether they are read all at once (a full
rebuild) or a few at a time (an incremental scan).

Flights: the published schedule, then cancellations, new departure times, departures and arrivals from flight_ops.
"""
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.settings import SIM_DATE, TZ

DAY0 = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, tzinfo=TZ)
FIELDS = ("itinerary", "status", "destination", "bag_routing")
MAIN = ("itinerary", "status", "destination")
KIND = {"itinerary": "main", "status": "main", "destination": "main", "bag_routing": "bag"}


def clock(text: str) -> datetime:
    """'13:00' or '06:30+1' on the simulated day."""
    day = 0
    if "+" in text:
        text, plus = text.split("+")
        day = int(plus)
    h, m = text.split(":")
    return DAY0 + timedelta(days=day, hours=int(h), minutes=int(m))


def hhmm(t: datetime | None) -> str:
    return t.astimezone(TZ).strftime("%H:%M") if t else "-"


def show(v) -> str:
    """A field value as text (for the mismatches table and the printouts)."""
    if isinstance(v, (list, tuple)):
        return " > ".join(v) if v else "(none)"
    return "(none)" if v is None else str(v)


@dataclass
class Flight:
    id: str
    direction: str
    sched_dep: datetime
    est_dep: datetime
    cancelled_at: datetime | None = None
    done: bool = False                   # departed (or arrived)
    events: list = field(default_factory=list)
    origin: str | None = None
    destination: str | None = None
    diverted_to: str | None = None       # 4b-3: where it landed instead (flight_diverted)
    arrived: bool = False                # 4b-3: flight_arrived seen
    airborne: bool = False               # in the air now: took off (departed / airborne / holding), not yet landed or diverted


@dataclass
class CommState:
    """What the agent knows at `t`. Built by feeding it events (apply_minute), never by reading end-of-day tables."""
    t: datetime | None = None
    truth: dict = field(default_factory=dict)          # pid -> {field: value, "at": time, "event": id}
    history: dict = field(default_factory=dict)        # pid -> [(time, event id, {field: value})] every trip record, in order
    shown: dict = field(default_factory=dict)          # (pid, ch) -> {field: value}
    last_update: dict = field(default_factory=dict)    # (pid, ch, kind) -> (time, event id) of the last update that arrived
    receipts: dict = field(default_factory=dict)       # (pid, ch, kind) -> [(time, event id, queued_at)] failed deliveries
    all_receipts: list = field(default_factory=list)   # (time, ch, pid, event id)
    flights: dict = field(default_factory=dict)
    open: dict = field(default_factory=dict)           # (pid, ch, field) -> {"since", "evidence": [...]}
    closed: list = field(default_factory=list)         # [(pid, ch, field), since, ended, evidence] stretches that ended
    lags: dict = field(default_factory=dict)           # ch -> [minutes it took to catch up] (from the stretches that ended)
    seen: set = field(default_factory=set)             # event ids already applied
    dirty: set = field(default_factory=set)            # (pid, ch) to compare at the end of this minute
    frozen: set = field(default_factory=set)           # channels (or "*" = all) not compared now: their feed is stale
    message_receipts: list = field(default_factory=list)   # 4b-3: (time, delivered/dropped, pid, channel, message id, reason)

    def copy(self) -> "CommState":
        return copy.deepcopy(self)


def new_state(schedule: list[dict], channels: list[str], screens: dict | None = None) -> CommState:
    st = CommState()
    st.channels = list(channels)
    st.screens = dict(screens or {})                  # pid -> the screens this passenger has (no entry: all channels)
    for f in schedule:
        if f.get("sched_dep") is None:
            continue
        st.flights[f["id"]] = Flight(f["id"], f["direction"], f["sched_dep"], f["sched_dep"], origin=f.get("origin"),
                                     destination=f.get("destination"))
    return st


def apply_events(st: CommState, events: list[dict], upto: datetime) -> None:
    """Apply events (any order, duplicates ignored) minute by minute, comparing at the end of each minute."""
    # times in MCI local time (the database hands them back in UTC): the same instants, printed the way people read them
    todo = sorted(({**e, "occurred_at": e["occurred_at"].astimezone(TZ)} for e in events if e["id"] not in st.seen
                   and e["occurred_at"] <= upto), key=lambda e: (e["occurred_at"], e["id"]))
    i = 0
    while i < len(todo):
        minute = todo[i]["occurred_at"]
        while i < len(todo) and todo[i]["occurred_at"] == minute:
            _apply(st, todo[i])
            st.seen.add(todo[i]["id"])
            i += 1
        compare(st, minute)
    st.t = upto


def _apply(st: CommState, e: dict) -> None:
    typ, p, when, eid = e["type"], e.get("payload") or {}, e["occurred_at"], e["id"]
    if typ == "trip_record_updated":
        pid = p.get("passenger_id")
        if not isinstance(pid, str):
            return
        snap = {"itinerary": tuple(p.get("itinerary") or ()), "status": p.get("status"), "destination": p.get("destination"),
                "bag_routing": tuple(p.get("bag_routing") or ())}
        first = pid not in st.truth
        st.truth[pid] = {**snap, "at": when, "event": eid}
        st.history.setdefault(pid, []).append((when, eid, snap))
        mine = [ch for ch in st.channels if ch in st.screens.get(pid, st.channels)]
        if first:                                         # start of day: each of HIS screens shows the start-of-day record
            for ch in mine:
                st.shown[(pid, ch)] = dict(snap)
        for ch in mine:
            st.dirty.add((pid, ch))
    elif typ == "channel_view_updated":
        pid, ch, kind = p.get("passenger_id"), p.get("channel"), p.get("kind")
        if (pid, ch) not in st.shown:
            return
        cur = st.shown[(pid, ch)]
        if kind == "main":
            cur.update(itinerary=tuple(p.get("shown_itinerary") or ()), status=p.get("shown_status"),
                       destination=p.get("shown_destination"))
        elif kind == "bag":
            cur.update(bag_routing=tuple(p.get("shown_bag_routing") or ()))
        else:
            return
        st.last_update[(pid, ch, kind)] = (when, eid)
        st.dirty.add((pid, ch))
    elif typ == "channel_update_failed":
        pid, ch, kind = p.get("passenger_id"), p.get("channel"), p.get("kind")
        if kind == "message":                              # a message receipt: not about what a screen shows
            st.message_receipts.append((when, "message_failed", pid, ch, p.get("message_id"), None))
            return
        st.receipts.setdefault((pid, ch, kind), []).append((when, eid, p.get("queued_at")))
        st.all_receipts.append((when, ch, pid, eid))
        st.dirty.add((pid, ch))
    elif typ == "next_day_flight_created":                 # a flight for tomorrow (rebooked passengers may be on it)
        fid, route = p.get("flight_id"), p.get("route") or ""
        if fid and fid not in st.flights and "-" in route and isinstance(p.get("departs"), str):
            try:
                dep = clock(p["departs"])
            except ValueError:
                return
            origin, dest = route.split("-", 1)
            st.flights[fid] = Flight(fid, "departure", dep, dep, origin=origin, destination=dest)
    elif typ in ("message_delivered", "message_dropped"):
        st.message_receipts.append((when, typ, p.get("passenger_id"), p.get("channel"), p.get("message_id"), p.get("reason")))
    else:
        f = st.flights.get(p.get("flight_id"))
        if f is None:
            return
        f.events.append((eid, when, typ))
        if typ == "flight_cancelled":
            f.cancelled_at = f.cancelled_at or when
        elif typ in ("flight_delayed", "flight_delay_updated") and isinstance(p.get("new_departure"), str):
            try:
                f.est_dep = clock(p["new_departure"])
            except ValueError:
                pass
        elif typ in ("flight_departed", "flight_arrived"):
            f.done = True
        elif typ == "flight_diverted":
            f.diverted_to = p.get("diverted_to")
        if typ == "flight_arrived":
            f.arrived = True
        if typ in ("flight_departed", "flight_airborne", "flight_holding"):
            f.airborne = True
        elif typ in ("flight_arrived", "flight_diverted", "flight_cancelled"):
            f.airborne = False


def compare(st: CommState, minute: datetime) -> None:
    """End of a minute: open a mismatch where a channel stopped matching the truth, close it where it matches again.
    Channels whose feed is stale (or everything, if the truth feed is stale) are not compared: they stay dirty for later."""
    keep = set()
    for pid, ch in st.dirty:
        if "*" in st.frozen or ch in st.frozen or pid not in st.truth:
            keep.add((pid, ch))
            continue
        truth, shown = st.truth[pid], st.shown[(pid, ch)]
        for f in FIELDS:
            key = (pid, ch, f)
            wrong = shown[f] != truth[f]
            if wrong and key not in st.open:
                st.open[key] = {"since": minute, "evidence": [{"event_id": truth["event"], "at": truth["at"].isoformat(),
                                                               "type": "trip_record_updated"}]}
            elif not wrong and key in st.open:
                m = st.open.pop(key)
                upd = st.last_update.get((pid, ch, KIND[f]))
                ev = m["evidence"] + ([{"event_id": upd[1], "at": upd[0].isoformat(), "type": "channel_view_updated"}] if upd else [])
                st.closed.append([key, m["since"], minute, ev])
                st.lags.setdefault(ch, []).append(int((minute - m["since"]).total_seconds() // 60))
    st.dirty = keep


def build_state(schedule: list[dict], events: list[dict], t: datetime, channels: list[str], frozen: set | None = None,
                screens: dict | None = None) -> CommState:
    """The full rebuild: everything known at t, from scratch."""
    st = new_state(schedule, channels, screens)
    st.frozen = set(frozen or ())
    apply_events(st, events, t)
    return st
