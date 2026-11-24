"""Phase 4b-3: passenger messages and staff notes, written ONLY from confirmed facts, Situation's picture and approved decisions.

Templates (config/message_templates.yaml), no AI. Every message carries the CLAIMS it makes, so the message fact check
(backend/rules/message_check.py) can verify each one at send time. The agent runs that check on its own draft first (and tries a
more cautious draft if it fails); the orchestrator's rule check runs it again and is the one that counts.

When it writes:
  - a trip really changed (after a short settle, several changes become one message): delay, diversion, cancellation,
    rebooking, overnight;
  - the passenger's flight is affected by the incident and they have not heard from us yet (first notice);
  - an update was promised ("next update by HH:MM") and the passenger's flight is still waiting on the incident;
  - the passenger's app or SMS has shown something wrong for a while (correction);
  - a staff note when Situation's picture changes.
Who decides: restating the confirmed truth is automatic (send_status_update); anything with new consequences (cancellation,
rebooking, overnight, correction) needs the customer service supervisor (draft_message); one message to more than
mass_message_passengers passengers needs one approval for the batch (send_mass_message); staff notes need the supervisor (O08).
Never: a promise nobody approved, blame, a sensitive detail, another passenger's information, a detail a Situation conflict is
open about, or a name outside the app (names only for passengers it declares it is working on).
"""
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.agents.communication.state_at import hhmm
from backend.rules.message_check import message_problems, templates
from backend.settings import TZ

# automatic: restating confirmed facts (including a cancellation with its confirmed new flight). Approval: anything beyond
# confirmed facts (options, overnight arrangements, correcting an earlier wrong message), and any batch over the mass limit.
AUTO_TYPES = {"first_notice", "delay_update", "diverted_update", "still_delayed", "no_change", "cancellation_rebooking",
              "rebooking", "overnight_hotel", "overnight_home"}   # the last two state a decision a person already approved
APPROVAL_TYPES = {"cancellation_only", "overnight_notice", "correction"}
AIRBORNE_TYPES = {"first_notice", "no_change", "delay_update", "still_delayed"}   # the "what to do now" is about landing
DECISION = {"first_notice": ["O01", "O02", "O05", "O06"], "delay_update": ["O07", "O02", "O05", "O06"],
            "still_delayed": ["O07", "O02", "O05", "O06"], "diverted_update": ["O13", "O02", "O05", "O06"],
            "no_change": ["O07", "O02", "O05", "O06"], "overnight_hotel": ["O14", "O05", "O06"],
            "overnight_home": ["O14", "O05", "O06"],
            "cancellation_rebooking": ["O14", "O05", "O06"], "cancellation_only": ["O14", "O05", "O06"],
            "overnight_notice": ["O14", "O05", "O06"], "rebooking": ["O14", "O05", "O06"], "correction": ["O04", "O05", "O06"],
            "staff_note": ["O08", "O05"]}


@dataclass
class MessageMemory:
    seq: int = 0
    seen_version: dict = field(default_factory=dict)       # pid -> the trip version last handled (the 05:00 record = baseline)
    seen_snap: dict = field(default_factory=dict)          # pid -> the trip as last handled
    sent: dict = field(default_factory=lambda: defaultdict(deque))   # pid -> times messages went out (anti-spam window)
    pending: dict = field(default_factory=dict)            # pid -> {"rec", "version", "type", "since"} waiting for approval
    next_due: dict = field(default_factory=dict)           # pid -> the "next update by" time we promised
    notified: set = field(default_factory=set)             # pids that had a first notice (or a change message) this incident
    corrected: set = field(default_factory=set)            # (pid, channel, since) already corrected
    held: set = field(default_factory=set)                 # (pid, version) already logged as held
    blocked: set = field(default_factory=set)              # (pid, version, type) already logged as blocked
    resent: set = field(default_factory=set)               # message ids already resent on the other channel
    by_id: dict = field(default_factory=dict)              # message id -> the message (for receipts, resends and pages)
    staff_key: tuple | None = None
    staff_at: datetime | None = None
    incident_version: int | None = None
    cities: dict | None = None                             # station code -> city name (published reference data)
    care: dict = field(default_factory=dict)               # pid -> approved hotel / transport details (Passenger Care's plan)
    care_told: set = field(default_factory=set)            # (pid, decision) overnight details already sent
    receipts_done: int = 0                                 # message receipts already looked at
    released: list = field(default_factory=list)           # (pid, promised by, when) "next update" no longer needed
    promised: dict = field(default_factory=dict)           # pid -> the "next update by" time last promised (ISO)
    log: list = field(default_factory=list)                # every message drafted, with what happened to it


def _date(d: datetime) -> str:
    d = d.astimezone(TZ)
    return f"{d:%b} {d.day}"


def flight_facts(st) -> dict:
    """Flight facts as the agent knows them from flight operations (the same view the rule check gets)."""
    out = {}
    for fid, f in st.flights.items():
        status = "cancelled" if f.cancelled_at else "arrived" if f.arrived else "diverted" if f.diverted_to else \
            "departed" if f.done else "delayed" if f.est_dep > f.sched_dep else "scheduled"
        out[fid] = {"status": status, "origin": f.origin, "destination": f.destination, "dep_time": hhmm(f.est_dep),
                    "dep_date": _date(f.est_dep), "diverted_to": f.diverted_to, "airborne": f.airborne,
                    "waiting_at_diversion": bool(f.diverted_to) and not f.airborne and not f.arrived and not f.resumed}
    return out


def counts(st) -> dict:
    """Flights at MCI today, as flight operations has reported them (for staff notes)."""
    today = [f for f in st.flights.values() if f.sched_dep.astimezone(TZ).date() == st.t.astimezone(TZ).date()] if st.t else []
    return {"cancelled": sum(1 for f in today if f.cancelled_at), "diverted": sum(1 for f in today if f.diverted_to),
            "delayed": sum(1 for f in today if not f.cancelled_at and f.est_dep > f.sched_dep)}


def _end(pic, S, channel=None, now=None):
    """Situation's expected end, never as an exact promise. Returns (sentence, claim)."""
    T = templates()
    sms = "_sms" if channel == "sms" else ""
    if pic and pic["facts"].get("status") == "recovery":
        return T["end"]["ended" + sms], {"kind": "end", "phrase": "ended"}
    sus = (pic or {}).get("suspected") or {}
    likely = (sus.get("expected_end_likely") or {})
    if likely.get("value") is None or likely.get("confidence") is None or likely["confidence"] < S["end_confidence_low"]:
        return T["end"]["unknown" + sms], {"kind": "end", "phrase": "unknown"}
    value = likely["value"] if isinstance(likely["value"], datetime) else datetime.fromisoformat(likely["value"])
    if now is not None and value <= now:                # the estimate has passed and Situation has not said "over": no time
        return T["end"]["unknown" + sms], {"kind": "end", "phrase": "unknown_passed"}
    tm = hhmm(value)
    return T["end"]["around" + sms].format(end_time=tm), {"kind": "end", "phrase": "around", "time": tm}


def _cause(pic, S):
    T = templates()
    typ = ((pic or {}).get("suspected") or {}).get("type") or {}
    word = T["cause"]["weather"] if typ.get("value") == "weather" and (typ.get("confidence") or 0) >= S["cause_confidence_min"] \
        else T["cause"]["other"]
    return word, {"kind": "cause", "word": word}


class Writer:
    """Renders one message from a template and records the claims behind every fact it states."""

    def __init__(self, cities):
        self.cities, self.claims, self.T = cities, [], templates()

    def city(self, code):
        return self.cities.get(code, code)

    def flight(self, f, fid):
        self.claims.append({"kind": "route", "flight": fid, "from": f[fid]["origin"], "to": f[fid]["destination"]})
        return fid

    def render(self, kind, channel, values, next_sentence, first_name=None, do_now=None):
        tpl = self.T["templates"][kind]
        parts = {"know": tpl["know"].format(**values), "dont_know": tpl["dont_know"].format(**values).strip(),
                 "do_now": do_now or (tpl.get("sms_do_now") if channel == "sms" and tpl.get("sms_do_now") else tpl["do_now"]).format(**values),
                 "next": tpl.get("next") or next_sentence}
        greeting = self.T["greeting"]["app_named"].format(first_name=first_name) if first_name and channel == "app" else ""
        if greeting:
            self.claims.append({"kind": "name", "value": first_name})
        text = greeting + " ".join(parts[k] for k in ("know", "dont_know", "do_now", "next"))
        return text, parts


def _next_flight(trip, flights):
    """The passenger's next flight that hasn't gone (truth first)."""
    for fid in trip["itinerary"]:
        if fid in flights and flights[fid]["status"] not in ("departed", "arrived"):
            return fid
    return None


def draft(kind, pid, channel, trip, prev, st, pic, S, cities, first_name=None, next_time=None, wrong=None, care=None):
    """One message (or None if there is nothing true to say). Every stated fact gets a claim."""
    f = flight_facts(st)
    w = Writer(cities)
    backed = None
    end_sentence, end_claim = _end(pic, S, channel, st.t)
    values = {"end": end_sentence}
    claims = [{"kind": "trip_status", "status": trip["status"]}]
    nxt = _next_flight(trip, f)
    if kind in ("first_notice", "delay_update", "still_delayed", "diverted_update", "correction", "no_change") and nxt is None:
        return None
    if kind in ("first_notice", "no_change"):
        word, cause_claim = _cause(pic, S)
        values.update(cause=word, flight=w.flight(f, nxt), city=w.city(f[nxt]["destination"]))
        claims += [cause_claim, end_claim, {"kind": "city", "from": "MCI"}]
    elif kind in ("delay_update", "still_delayed"):
        if f[nxt]["status"] != "delayed":
            return None
        values.update(flight=w.flight(f, nxt), city=w.city(f[nxt]["destination"]), dep_time=f[nxt]["dep_time"])
        claims += [end_claim, {"kind": "flight_status", "flight": nxt, "status": "delayed"},
                   {"kind": "departure", "flight": nxt, "time": f[nxt]["dep_time"], "date": f[nxt]["dep_date"]}]
    elif kind == "diverted_update":
        div = next((x for x in trip["itinerary"] if x in f and f[x]["diverted_to"]), None)
        if div is None:
            return None
        values.update(flight=w.flight(f, div), city=w.city(f[div]["destination"]), diverted_city=w.city(f[div]["diverted_to"]))
        claims += [end_claim, {"kind": "flight_status", "flight": div, "status": "diverted"},
                   {"kind": "diverted_to", "flight": div, "to": f[div]["diverted_to"]}]
    elif kind in ("cancellation_rebooking", "overnight_notice", "cancellation_only", "rebooking"):
        old = [x for x in (prev or {}).get("itinerary", ()) if x not in trip["itinerary"] and x in f and f[x]["status"] == "cancelled"]
        new = [x for x in trip["itinerary"] if x not in (prev or {}).get("itinerary", ())]
        if kind == "cancellation_only":
            gone = old[0] if old else next((x for x in trip["itinerary"] if x in f and f[x]["status"] == "cancelled"), None)
            if gone is None:
                return None
            values.update(old_flight=w.flight(f, gone), city=w.city(f[gone]["destination"]))
            claims += [{"kind": "flight_status", "flight": gone, "status": "cancelled"}]
        else:
            target = (new or [nxt])[-1] if (new or nxt) else None
            first_new = (new or [nxt])[0]
            if target is None or first_new not in f:
                return None
            values.update(new_flight=w.flight(f, first_new), dep_time=f[first_new]["dep_time"], dep_date=f[first_new]["dep_date"],
                          city=w.city(f[target]["destination"]))
            claims += [{"kind": "booked_on", "itinerary": list(trip["itinerary"])},
                       {"kind": "departure", "flight": first_new, "time": f[first_new]["dep_time"], "date": f[first_new]["dep_date"]}]
            if kind == "cancellation_rebooking":
                if not old:
                    return None
                values["old_flight"] = w.flight(f, old[0])
                values["city"] = w.city(f[old[0]]["destination"])
                claims += [{"kind": "flight_status", "flight": old[0], "status": "cancelled"}]
    elif kind in ("overnight_hotel", "overnight_home"):
        care = (care or {}).get(pid) or {}
        T = templates()
        if kind == "overnight_hotel":
            if care.get("hotel_status") != "approved" or not care.get("hotel_name"):
                return None
            shuttle = care.get("shuttle") == "Yes" and "shuttle" in (care.get("transport") or "")
            values.update(hotel=care["hotel_name"], transport_sentence=T["transport_sentence"]["shuttle" if shuttle else "van"]
                          .format(hotel=care["hotel_name"]))
            claims += [{"kind": "care_hotel", "name": care["hotel_name"], "decision": care.get("hotel_decision")},
                       {"kind": "care_transport", "value": care.get("transport")}]
            backed = care.get("hotel_decision")
        else:
            if care.get("transport_status") != "approved":
                return None
            claims += [{"kind": "care_transport", "value": care.get("transport"), "decision": care.get("transport_decision")}]
            backed = care.get("transport_decision")
    elif kind == "correction":
        T = templates()
        status = trip["status"]
        key = "overnight" if status == "overnight" else status if status in T["correct"] else "confirmed"
        values.update(channel_name=T["channel_names"][wrong["channel"]], wrong_status=T["status_words"].get(wrong["status"], wrong["status"]))
        claims += [end_claim, {"kind": "wrong_shown", "channel": wrong["channel"], "status": wrong["status"]}]
        inner = {"flight": w.flight(f, nxt), "city": w.city(f[nxt]["destination"]), "dep_time": f[nxt]["dep_time"],
                 "dep_date": f[nxt]["dep_date"], "new_flight": nxt, "diverted_city": w.city(f[nxt]["diverted_to"] or "")}
        if key == "delayed":
            claims += [{"kind": "flight_status", "flight": nxt, "status": "delayed"},
                       {"kind": "departure", "flight": nxt, "time": f[nxt]["dep_time"], "date": f[nxt]["dep_date"]}]
        elif key == "overnight":
            claims += [{"kind": "booked_on", "itinerary": list(trip["itinerary"])},
                       {"kind": "departure", "flight": nxt, "time": f[nxt]["dep_time"], "date": f[nxt]["dep_date"]}]
        elif key == "diverted" and f[nxt]["diverted_to"]:
            claims += [{"kind": "diverted_to", "flight": nxt, "to": f[nxt]["diverted_to"]}]
        elif key == "cancelled":
            claims += [{"kind": "flight_status", "flight": nxt, "status": "cancelled"}]
        values["correct"] = T["correct"][key].format(**inner)
        claims.append({"kind": "city", "from": "MCI"})
    else:
        return None
    if kind in ("cancellation_rebooking", "overnight_notice", "rebooking"):
        claims.append({"kind": "city", "from": "MCI"})
    T = templates()
    sms = "_sms" if channel == "sms" else ""
    next_sentence = T["next"]["timed" + sms].format(next_time=next_time) if next_time else T["next"]["if_changes" + sms]
    if next_time:
        claims.append({"kind": "next_update", "time": next_time})
    do_now = None
    up = next((x for x in trip["itinerary"] if f.get(x, {}).get("airborne")), None) if kind in AIRBORNE_TYPES else None
    sat = next((x for x in trip["itinerary"] if f.get(x, {}).get("waiting_at_diversion")), None) \
        if kind in AIRBORNE_TYPES and not up else None
    if sat:                                                # on a diverted plane on the ground: not "before you go to the gate"
        w.flight(f, sat)
        claims += [{"kind": "waiting_at_diversion", "flight": sat}, {"kind": "city", "from": f[sat]["diverted_to"]}]
        do_now = T["diverted_waiting"]["sms" if channel == "sms" else "app"].format(div_city=w.city(f[sat]["diverted_to"]))
    if up:                                                 # in the air now: what to do is about landing, not the gate
        w.flight(f, up)                                    # backs the landing city
        claims.append({"kind": "airborne", "flight": up})
        later = trip["itinerary"][trip["itinerary"].index(up) + 1:]
        key = ("connection" if later else "final") + ("_sms" if channel == "sms" else "")
        do_now = T["airborne"][key].format(land_city=w.city(f[up]["destination"]))
    text, parts = w.render(kind, channel, values, next_sentence, first_name, do_now)
    out = {"type": kind, "passenger_id": pid, "channel": channel, "text": text, "parts": parts, "claims": claims + w.claims,
            "trip_version": trip["at"].isoformat(),
            "trip_facts": {"itinerary": list(trip["itinerary"]), "status": trip["status"]}}   # what it says about the trip
    if backed is not None:
        out["backed_by"] = backed                          # the approved decision that allows naming a hotel or a ride
    return out


def waiting_on_incident(trip, f, pic=None, S=None, now=None) -> bool:
    """Still waiting on the incident: a promised update must follow (delayed or diverted, or affected while it is on)."""
    nxt = _next_flight(trip, f)
    if nxt is None:
        return False
    if trip["status"] in ("delayed", "diverted"):
        return True
    return pic is not None and (pic.get("facts") or {}).get("status") == "abnormal" and affected(trip, f, pic, S, now)


def affected(trip, f, pic, S, now) -> bool:
    """First notice: the passenger's next flight leaves before Situation's latest expected end (+ a margin), or is already hit."""
    nxt = _next_flight(trip, f)
    if nxt is None or f.get(nxt, {}).get("status") == "departed":
        return False
    if trip["status"] != "confirmed":
        return True
    latest = (((pic or {}).get("suspected") or {}).get("expected_end_latest") or {}).get("value")
    if latest is None:
        return False
    latest = latest if isinstance(latest, datetime) else datetime.fromisoformat(latest)
    from backend.agents.communication.state_at import clock
    dep = clock(f[nxt]["dep_time"]) if f[nxt]["dep_date"] == _date(now) else None
    return dep is not None and now <= dep <= latest + timedelta(minutes=S["affected_window_min"])


def held_by_conflict(msg, view) -> list[str]:
    """Flights this message mentions whose status a Situation conflict is open about (that detail is held)."""
    return sorted({c["flight"] for c in msg["claims"] if c.get("flight") in view.conflicts
                   and "status" in (view.conflicts[c["flight"]].get("fields") or ["status"])})


# ---------------------------------------------------------------- the minute's plan

def _snap(trip):
    return {"itinerary": tuple(trip["itinerary"]), "status": trip["status"], "destination": trip["destination"],
            "bag_routing": tuple(trip["bag_routing"])}


def classify(prev, trip, f):
    """What kind of change this is, for the passenger (None: nothing to tell them, e.g. only the bag routing changed)."""
    new = [x for x in trip["itinerary"] if x not in prev["itinerary"]]
    gone_cancelled = [x for x in prev["itinerary"] if x not in trip["itinerary"] and f.get(x, {}).get("status") == "cancelled"]
    if new and gone_cancelled:
        return "cancellation_rebooking"
    if trip["status"] == "cancelled" and prev["status"] != "cancelled":
        return "cancellation_only"
    if new and trip["status"] != "overnight":
        return "rebooking"                                  # another flight, nothing cancelled (e.g. a missed connection)
    if trip["status"] == "overnight" and prev["status"] != "overnight":
        return "overnight_notice"
    if trip["status"] == "diverted" and prev["status"] != "diverted":
        return "diverted_update"
    if trip["status"] == "delayed":
        return "delay_update"
    return None


def check_world(st, view, mem, first_names=None, working_on=()):
    """The facts the message fact check compares against: the same view the rule check gets (built from published data)."""
    return {"now": st.t, "flights": flight_facts(st), "cities": getattr(mem, "cities", None) or {}, "counts": counts(st),
            "situation": view.picture,
            "confirmed_trips": {pid: {"itinerary": list(t["itinerary"]), "status": t["status"]} for pid, t in st.truth.items()},
            "trip_versions": {pid: t["at"].isoformat() for pid, t in st.truth.items()},
            "was_shown_wrong": {(pid, ch): v["status"] for (pid, ch), v in st.shown.items()
                                if pid in st.truth and v["status"] != st.truth[pid]["status"]},
            "care": dict(getattr(mem, "care", {}) or {}),
            "approved": {d for e in (getattr(mem, "care", {}) or {}).values() for d, ok in
                         ((e.get("hotel_decision"), e.get("hotel_status") == "approved"),
                          (e.get("transport_decision"), e.get("transport_status") == "approved")) if ok and d is not None},
            "working_on": set(working_on), "first_names": dict(first_names or {}),
            "other_names": set((first_names or {}).values())}


def _recent(mem, pid, now):
    q = mem.sent[pid]
    while q and now - q[0] >= timedelta(hours=1):
        q.popleft()
    return len(q)


def plan_messages(ctx, result, view, mem: MessageMemory, settings: dict, screens: dict) -> dict:
    """This minute's messages. Returns {"proposals": [...], "withdrawn": [rec ids]}; the caller sends them to the rule check."""
    S, st, now = settings["messages"], result["state"], ctx.now
    if getattr(mem, "cities", None) is None:
        mem.cities = {r["code"]: r["name"] for r in ctx.read("stations", ["code", "name"])}
    pic = view.picture
    f = flight_facts(st)
    settle = timedelta(minutes=S["settle_min"])
    if "care_plans" in (ctx.contract.shared_reads or {}):
        care = ctx.read_shared("care_plans")             # audited with the version read
        mem.care = {e["passenger_id"]: e for e in ((care or {}).get("facts") or {}).get("passengers") or []}
    withdrawn, wanted = [], []
    open_wrong = {}
    for x in result["findings"]:
        if x["channel"] in ("app", "sms") and x["field"] in ("status", "itinerary") and x["age_min"] >= S["correction_after_min"]:
            open_wrong.setdefault((x["passenger_id"], x["channel"]), x)
    abnormal = bool(pic) and pic["facts"].get("status") == "abnormal"
    if abnormal and mem.incident_version is None:          # a new incident: everyone may get a first notice again
        mem.incident_version, mem.notified = pic["version"], set()
    elif pic and pic["facts"].get("status") == "normal":
        mem.incident_version = None
    for pid in sorted(st.truth):
        chans = [c for c in S["channel_order"] if c in (screens.get(pid) or ())]
        trip = st.truth[pid]
        ver = trip["at"]
        if pid not in mem.seen_version:                    # the start-of-day record is the baseline: nothing to tell
            mem.seen_version[pid], mem.seen_snap[pid] = ver, _snap(trip)
            continue
        if not chans:
            continue                                        # no app or SMS on file: staff notes cover the gate and call center
        pend = mem.pending.get(pid)
        if pend and pend["facts"] != {"itinerary": list(trip["itinerary"]), "status": trip["status"]}:
            withdrawn.append(pend["rec"])
            ctx.record_event("message_withdrawn", {"passenger_id": pid, "recommendation": pend["rec"], "type": pend["type"],
                                                   "reason": "the trip changed while it waited for approval; redrafted",
                                                   "as_of": now.isoformat()})
            mem.pending.pop(pid)
            _restore(mem, pend)
            pend = None
        if pend:
            continue
        kind, wrong, changed = None, None, ver != mem.seen_version[pid]
        if changed:
            if now - ver < settle:
                continue                                    # let several changes become one message
            kind = classify(mem.seen_snap[pid], trip, f)
            if kind is None:
                mem.seen_version[pid], mem.seen_snap[pid] = ver, _snap(trip)
        if kind is None and mem.next_due.get(pid) and now >= mem.next_due[pid]:
            if waiting_on_incident(trip, f, pic, S, now):
                nxt = _next_flight(trip, f)
                kind = "still_delayed" if f.get(nxt, {}).get("status") == "delayed" else "no_change"
            else:
                mem.released.append((pid, mem.promised.get(pid) or mem.next_due[pid].isoformat(), now.isoformat()))
                mem.next_due[pid] = None                    # nothing left to wait for: the flight left or the trip settled
        if kind is None and pid not in mem.notified and abnormal and affected(trip, f, pic, settings["messages"], now):
            kind = "first_notice"
        if kind is None:                                 # Passenger Care's approved room or ride, once each
            c = mem.care.get(pid) or {}
            if c.get("hotel_status") == "approved" and (pid, c.get("hotel_decision")) not in mem.care_told:
                kind = "overnight_hotel"
            elif c.get("transport_status") == "approved" and "home" in (c.get("transport") or "") and \
                    (pid, c.get("transport_decision")) not in mem.care_told:
                kind = "overnight_home"
        if kind is None:
            for ch in chans:
                x = open_wrong.get((pid, ch))
                if x and (pid, ch, x["since"]) not in mem.corrected:
                    kind, wrong = "correction", {"channel": ch, "status": st.shown[(pid, ch)]["status"], "since": x["since"]}
                    break
        if kind is None:
            continue
        if _recent(mem, pid, now) >= S["max_per_hour"]:
            continue                                        # anti-spam: wait (the latest truth goes out when there is room)
        wanted.append((pid, kind, wrong["channel"] if wrong else chans[0], wrong, changed))

    # names: only first names, only in the app, only for the passengers it now declares it is working on
    # (and keeps declaring the ones whose named message is still waiting for approval)
    app_pids = sorted({pid for pid, _, ch, _, _ in wanted if ch == "app"} | {v["pid"] for v in mem.pending.values()
                                                                             if v.get("pid") and v.get("named")})
    first = {}
    if app_pids:
        ctx.working_on(app_pids)
        first = {r["id"]: (r["full_name"] or "").split(" ")[0] for r in
                 ctx.read("passengers", ["id", "full_name"], where={"id": app_pids})}
    world = check_world(st, view, mem, first, app_pids)
    drafts = []
    per_kind = defaultdict(int)
    for _, kind, _, _, _ in wanted:
        per_kind[kind] += 1
    for pid, kind, ch, wrong, changed in wanted:
        trip = st.truth[pid]
        # the next update: drafted after interval_min; if it goes out as a mass message it needs an approval first, so the time
        # promised to the passenger allows for that (a promise is only made if it can be kept)
        due = now + timedelta(minutes=S["interval_min"])
        margin = S["mass_promise_margin_min"] if per_kind[kind] > S["mass_message_passengers"] else 0
        nxt_time = hhmm(due + timedelta(minutes=margin)) if waiting_on_incident(trip, f, pic, S, now) else None
        m = draft(kind, pid, ch, trip, mem.seen_snap[pid], st, pic, S, mem.cities, first.get(pid), nxt_time, wrong, mem.care)
        if m is None:
            if changed:
                mem.seen_version[pid], mem.seen_snap[pid] = trip["at"], _snap(trip)
            continue
        held = held_by_conflict(m, view)
        if held:
            from backend.agents.communication.situation_view import note_hold
            for fid in held:
                note_hold(view, fid, pid, now)              # counts toward asking network operations (A09)
            key = (pid, trip["at"].isoformat())
            if key not in mem.held:
                mem.held.add(key)
                ctx.record_event("message_held", {"passenger_id": pid, "type": kind, "flight_ids": held, "as_of": now.isoformat(),
                                                  "reason": f"held: Situation reports conflicting data on {held[0]}"})
            continue
        problems = message_problems(m, world)
        if problems:                                        # the agent's own check: never final, but it never sends a known-bad draft
            key = (pid, trip["at"].isoformat(), kind)
            if key not in mem.blocked:
                mem.blocked.add(key)
                ctx.record_event("message_blocked", {"passenger_id": pid, "type": kind, "reasons": problems[:5],
                                                     "by": "the agent's own fact check", "as_of": now.isoformat()})
            continue
        mem.seq += 1
        m["id"] = f"M{mem.seq}"
        m["created"] = now.isoformat()
        m["_next"] = nxt_time
        m["_due"] = due if nxt_time else None
        m["_prev"] = (mem.seen_version[pid], mem.seen_snap[pid])     # what the passenger was told before this message
        drafts.append(m)
        mem.notified.add(pid)
        if changed:
            mem.seen_version[pid], mem.seen_snap[pid] = trip["at"], _snap(trip)
        if kind == "correction":
            mem.corrected.add((pid, ch, wrong["since"]))
        if kind in ("overnight_hotel", "overnight_home"):
            c = mem.care.get(pid) or {}
            mem.care_told.add((pid, c.get("hotel_decision") if kind == "overnight_hotel" else c.get("transport_decision")))
    drafts += _resends(ctx, st, mem, S, screens, world)
    staff = _staff_note(ctx, st, view, mem, S, world, withdrawn)
    if staff:
        drafts.append(staff)
    return {"proposals": _propose(ctx, drafts, mem, S, view), "withdrawn": withdrawn}


def _resends(ctx, st, mem, S, screens, world):
    """A message whose delivery failed goes once more, on the passenger's other channel, if it is still true."""
    out = []
    receipts = st.message_receipts[getattr(mem, "receipts_done", 0):]
    mem.receipts_done = len(st.message_receipts)
    for when, what, pid, ch, mid, _ in receipts:
        m = mem.by_id.get(mid)
        if m is None or m["type"] in ("staff_note", "correction"):
            continue
        trip = st.truth.get(pid)
        if what == "message_dropped":
            # it never arrived (the trip changed while it travelled): the passenger still knows only what they knew before it,
            # so the next message must say everything since then, with the latest truth
            _restore_told(mem, pid, m)
            continue
        if what != "message_failed" or mid in mem.resent:
            continue
        mem.resent.add(mid)
        other = next((c for c in S["channel_order"] if c != ch and c in (screens.get(pid) or ())), None)
        if other is None or trip is None or m.get("trip_facts") != {"itinerary": list(trip["itinerary"]), "status": trip["status"]}:
            _restore_told(mem, pid, m)                      # no other channel, or the trip moved on: send the latest truth again
            continue
        r = draft(m["type"], pid, other, trip, (m.get("_prev") or (None, mem.seen_snap.get(pid)))[1], st, world["situation"], S, mem.cities,
                  None, m.get("_next"))
        if r is None or message_problems(r, world):
            continue
        mem.seq += 1
        r.update(id=f"M{mem.seq}", created=ctx.now.isoformat(), resend_of=mid, _next=m.get("_next"), _resend=True)
        out.append(r)
    return out


def _restore_told(mem, pid, m):
    """The passenger did not get message m: unless a later message has gone out since, go back to what they were told before."""
    later = [e for e in mem.log if e["passenger_id"] == pid and e["status"] == "sent" and e["created"] > m["created"]]
    if not later and m.get("_prev"):
        mem.seen_version[pid], mem.seen_snap[pid] = m["_prev"]
        mem.notified.discard(pid) if m["type"] == "first_notice" else None


def _staff_note(ctx, st, view, mem, S, world, withdrawn):
    pic = view.picture
    if not pic or pic["facts"].get("status") not in ("abnormal", "recovery"):
        return None
    word, cause_claim = _cause(pic, S)
    end_sentence, end_claim = _end(pic, S, None, ctx.now)
    c = counts(st)
    key = (pic["facts"]["status"], word, end_claim.get("phrase"), end_claim.get("time"), c["cancelled"], c["delayed"], c["diverted"])
    if key == mem.staff_key or (mem.staff_at and ctx.now - mem.staff_at < timedelta(minutes=S["staff_note_gap_min"])):
        return None
    pend = mem.pending.get("staff")
    if pend:
        withdrawn.append(pend["rec"])
        ctx.record_event("message_withdrawn", {"passenger_id": None, "recommendation": pend["rec"], "type": "staff_note",
                                               "reason": "Situation's picture changed while it waited for approval; redrafted",
                                               "as_of": ctx.now.isoformat()})
        mem.pending.pop("staff")
    w = Writer(mem.cities)
    values = {"cause": word, "end": end_sentence, "n_cancelled": c["cancelled"], "n_delayed": c["delayed"], "n_diverted": c["diverted"]}
    text, parts = w.render("staff_note", "staff", values, "")
    mem.staff_key, mem.staff_at = key, ctx.now
    mem.seq += 1
    m = {"id": f"M{mem.seq}", "type": "staff_note", "passenger_id": None, "channel": "staff", "text": text, "parts": parts,
         "claims": [cause_claim, end_claim, {"kind": "counts", **c}, {"kind": "city", "from": "MCI"}], "trip_version": None,
         "created": ctx.now.isoformat(), "situation_version": view.version}
    problems = message_problems(m, world)
    if problems:
        ctx.record_event("message_blocked", {"passenger_id": None, "type": "staff_note", "reasons": problems[:5],
                                             "by": "the agent's own fact check", "as_of": ctx.now.isoformat()})
        return None
    return m


def _propose(ctx, drafts, mem, S, view):
    """Group into proposals: automatic restatements, approvals for new consequences, one approval for a mass message."""
    by_kind = defaultdict(list)
    for m in drafts:
        by_kind["resend" if m.get("_resend") else m["type"]].append(m)
    out = []
    for kind, msgs in sorted(by_kind.items()):
        if kind == "staff_note":
            groups = [("send_staff_briefing", msgs)]
        elif kind != "resend" and len(msgs) > S["mass_message_passengers"]:
            groups = [("send_mass_message", msgs)]
        elif kind == "resend":
            groups = [("resend_on_other_channel", [m]) for m in msgs]
        elif kind in AUTO_TYPES:
            groups = [("send_status_update", [m]) for m in msgs]
        else:
            groups = [("draft_message", [m]) for m in msgs]
        for action, group in groups:
            ids = sorted({d for m in group for d in DECISION.get(m["type"], ["O05"])} |
                         ({"O11"} if action == "resend_on_other_channel" else set()))
            clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in group]
            sources = [{"fact": "trip records", "source": "reservations", "as_of": ctx.feed_last("reservations").isoformat()},
                       {"fact": "flight times and status", "source": "flight_ops", "as_of": ctx.feed_last("flight_ops").isoformat()}]
            if view.picture:
                sources.append({"fact": f"Situation picture version {view.version}", "source": "situation agent",
                                "as_of": view.picture["time"].isoformat()})
            one = group[0]
            output = {"title": f"{len(group)} {one['type'].replace('_', ' ')} messages" if len(group) > 1 else
                      f"{one['type'].replace('_', ' ')} for {one['passenger_id'] or 'staff'}",
                      "why": "confirmed trip, flight and Situation facts only", "data_as_of": ctx.now.isoformat(), "options": [],
                      "findings": [], "decision_ids": ids, "sources": sources,
                      "audience_passenger_ids": [m["passenger_id"] for m in group if m["passenger_id"]],
                      "channels": sorted({m["channel"] for m in group if m["channel"] != "staff"}),
                      "draft_text": one["text"][:900], "used_template": True, "promises_compensation": False,
                      "messages": clean}
            rid = ctx.propose(action, output)
            option = {"agent": "communication", "action": action, "decision_ids": ids, "data_as_of": ctx.now,
                      "requested_level": "auto" if action in ("send_status_update", "resend_on_other_channel") else "needs_approval",
                      "messages": clean, "id": rid, "situation_version": view.version}
            for m in group:
                mem.by_id[m["id"]] = m
                mem.log.append({"id": m["id"], "rec": rid, "action": action, "type": m["type"], "passenger_id": m["passenger_id"],
                                "channel": m["channel"], "text": m["text"], "created": m["created"], "status": "proposed",
                                "resend_of": m.get("resend_of"), "backed_by": m.get("backed_by")})
            if option["requested_level"] == "needs_approval":
                for m in group:
                    mem.pending[m["passenger_id"] or "staff"] = {"rec": rid, "version": m["trip_version"], "type": m["type"],
                                                                 "since": ctx.now, "facts": m.get("trip_facts"),
                                                                 "prev": m.get("_prev"), "pid": m["passenger_id"],
                                                                 "named": any(c.get("kind") == "name" for c in m["claims"])}
            out.append(option)
    return out


def sent(mem: MessageMemory, option: dict, now: datetime, approved_by: str | None = None, waited_min: float | None = None):
    """The rule check passed and (if needed) a person approved: the messages are on their way. Anti-spam counts them here."""
    for m in option["messages"]:
        pid = m["passenger_id"]
        if pid:
            mem.sent[pid].append(now)
            full = mem.by_id.get(m["id"], {})
            due = full.get("_due")
            mem.next_due[pid] = max(due, now) if due else None      # when to draft the next update (before the promised time)
            nxt = full.get("_next")
            if nxt:
                h, mi = map(int, nxt.split(":"))
                p = now.astimezone(TZ).replace(hour=h, minute=mi, second=0, microsecond=0)
                mem.promised[pid] = (p + timedelta(days=1) if p < now - timedelta(hours=12) else p).isoformat()
            else:
                mem.promised.pop(pid, None)
        if mem.pending.get(pid or "staff", {}).get("rec") == option["id"]:
            mem.pending.pop(pid or "staff")
    for e in mem.log:
        if e["rec"] == option["id"]:
            e.update(status="sent", sent_at=now.isoformat(), approved_by=approved_by, waited_min=waited_min)


def _restore(mem: MessageMemory, pend: dict):
    """A message was withdrawn: go back to what the passenger was last told, so the redraft says everything new."""
    if pend.get("pid") and pend.get("prev"):
        mem.seen_version[pend["pid"]], mem.seen_snap[pend["pid"]] = pend["prev"]


def withdraw(mem: MessageMemory, rec_id, why):
    for e in mem.log:
        if e["rec"] == rec_id and e["status"] == "proposed":
            e.update(status="withdrawn", why=why)
    for k, v in list(mem.pending.items()):
        if v["rec"] == rec_id:
            mem.pending.pop(k)
            _restore(mem, v)
