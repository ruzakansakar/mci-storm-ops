"""What frontline staff and the duty desk see in live mode: a flight, a Customer, the station, the clocks and the handover.

These are views of the airline's own records in the simulated world (the official systems the co-pilot sits on top of) and of
the co-pilot's shared records. Names and booking references only: never a contact detail. Every write (rebook, refund) goes
through Passenger Care's existing path and the rule check.
"""
from collections import Counter
from datetime import timedelta

from backend.execution.live_plain import clean
from backend.screens import screens_for
from backend.settings import read_limits
from simulator.world import WAITING_DEP

NEEDS = {"wheelchair": "wheelchair", "child_alone": "unaccompanied_minor"}


def fmt(m):
    if m is None:
        return None
    m = int(m)
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def screen_text(row) -> str:
    trip = " -> ".join(row.get("itinerary") or []) or "no flight"
    return f"{row.get('status', '')}: {trip}"


class Views:
    def __init__(self, session):
        self.s = session
        self.cfg = session.s["views"]

    @property
    def w(self):
        return self.s.world

    # ------------------------------------------------------------ helpers
    def crew_legal_until(self, f):
        """The latest departure at which every crew on this flight is still legal (duty left minus what the flight needs)."""
        from simulator.world import CREW_ROLES
        best = None
        for role, field, _ in CREW_ROLES:
            crew = self.w._crew_for(f, role)
            if crew is None or crew.get(field) is None or f.get("_crew_need") is None:
                continue
            # a waiting crew's duty runs from its scheduled departure (the world burns it while the flight waits)
            m = max(self.w.t, f["sched_dep"]) + crew[field] - f["_crew_need"]
            best = m if best is None else min(best, m)
        return best

    def gate_of(self, fid):
        rows = self.s.copilot.store.records["station_plans"]
        gates = (rows[-1].get("gates") or []) if rows else []
        g = next((x for x in gates if x.get("flight_id") == fid), None)
        if g is None:                                   # a departure leaves from the gate its inbound plane holds
            f = self.w.flights.get(fid) or {}
            inbound = [a for a in self.w.arrivals if a.get("aircraft_id") == f.get("aircraft_id") and a["sched_arr"] <= f.get("sched_dep", 0)]
            ids = {a["id"] for a in inbound}
            g = next((x for x in reversed(gates) if x.get("flight_id") in ids), None)
        return (g or {}).get("gate")

    def tracked_on(self, fid):
        return [pid for pid, t in self.w.truth.items() if fid in (t.get("itinerary") or ())]

    # ------------------------------------------------------------ a flight
    def flight(self, fid):
        f = self.w.flights.get(fid)
        if f is None:
            return None
        crew_id = (f.get("_role_crew") or {}).get("pilot") or f.get("crew_id")
        legal = self.crew_legal_until(f) if f["status"] not in ("departed", "arrived", "cancelled") else None
        script = self.script(fid)
        shown = {}
        pids = self.tracked_on(fid)
        for ch in ("app", "sms", "gate_screen", "call_center"):
            texts = Counter(screen_text(self.s.channels.shown[(p, ch)]) for p in pids if ch in self.s.channels.screens.get(p, ()))
            shown[ch] = texts.most_common(1)[0][0] if texts else ""
        conn = sum(v.get("count", 0) for (g, seg), v in self.w.groups.items() if g == fid and seg == "connecting")
        return {"id": fid, "from": f["origin"], "to": f["destination"], "sched_dep": fmt(f["sched_dep"]), "est_dep": fmt(f.get("est_dep")),
                "sched_arr": fmt(f["sched_arr"]), "est_arr": fmt(f.get("est_arr")), "status": f["status"], "gate": self.gate_of(fid),
                "crew": {"id": crew_id, "legal_until": fmt(legal)}, "customers": int(f.get("pax_total") or 0), "connections": int(conn),
                "script": script, "notes": list(self.s.notes.get(fid, [])), "on_screens": shown}

    def script(self, fid):
        """What to tell Customers: the newest message the co-pilot actually sent about this flight (a staff note first)."""
        best = None
        for h in self.s.copilot.recovery.history:
            if h.get("flight_id") == fid and h.get("status") == "sent" and h.get("audience") in ("staff", "passenger"):
                if best is None or (h.get("audience") == "staff", h.get("as_of") or "") >= (best.get("audience") == "staff",
                                                                                             best.get("as_of") or ""):
                    best = h
        if best is None:
            return {"text": "", "updated": None}
        from backend.execution.live_trace import hhmm
        return {"text": best["text"], "updated": hhmm(best.get("as_of") or best.get("created"))}

    # ------------------------------------------------------------ Customers (names and booking references only)
    def search(self, q):
        q = (q or "").strip().lower()
        if len(q) < self.cfg["search_min_chars"]:
            return []
        out = []
        for pid in sorted(self.w.truth):
            p = self.w.passengers[pid]
            if q in (p.get("full_name") or "").lower() or q == (p.get("booking_ref") or "").lower():
                t = self.w.truth[pid]
                out.append({"id": pid, "name": p.get("full_name"), "booking_ref": p.get("booking_ref"),
                            "flight_id": self.current_flight(pid), "status": t["status"]})
        return out[: self.cfg["search_max_results"]]

    def current_flight(self, pid):
        for fid in self.w.truth[pid]["itinerary"]:
            if self.w.flights.get(fid, {}).get("status") not in ("departed", "arrived"):
                return fid
        return (self.w.truth[pid]["itinerary"] or [None])[-1]

    def trip(self, pid):
        out = []
        for fid in self.w.pax_truth(pid)["itinerary"]:
            f = self.w.flights.get(fid) or {}
            out.append({"flight_id": fid, "from": f.get("origin"), "to": f.get("destination"), "dep": fmt(f.get("est_dep")),
                        "arr": fmt(f.get("est_arr")), "status": f.get("status")})
        return out

    def customer(self, pid):
        if pid not in self.w.truth:
            return None
        p = self.w.passengers[pid]
        t = self.w.pax_truth(pid)
        screens = []
        for ch in self.s.channels.screens.get(pid, ()):
            shown = self.s.channels.shown[(pid, ch)]
            screens.append({"channel": ch, "shows": screen_text(shown),
                            "wrong": any((pid, ch, f) in self.s.channels.dis for f in ("itinerary", "status", "destination"))})
        care = self.care_entry(pid)
        hotel = None
        if care and care.get("hotel_name"):
            hotel = {"name": care.get("hotel_name"), "status": care.get("hotel_status"), "shuttle": care.get("shuttle")}
        return {"id": pid, "name": p.get("full_name"), "trip": self.trip(pid), "true_status": screen_text(t), "screens": screens,
                "needs": [NEEDS[p["segment"]]] if p.get("segment") in NEEDS else [],
                "rebooking": self.rebooking(pid), "hotel": hotel, "refund_due": self.refund_due(pid)}

    def care_entry(self, pid):
        rows = self.s.copilot.store.records["care_plans"]
        return next((x for x in (rows[-1].get("passengers") or []) if x.get("passenger_id") == pid), None) if rows else None

    def stranded(self, pid):
        t = self.w.pax_truth(pid)
        return t["status"] in ("cancelled", "overnight") or any(self.w.flights.get(f, {}).get("_next_day") for f in t["itinerary"])

    def refund_due(self, pid):
        """DOT: a Customer whose flight was cancelled, moved to tomorrow or significantly delayed may decline and is owed a refund."""
        f = self.w.flights.get(self.leaving_on(pid) or "") or {}
        late = f.get("status") in WAITING_DEP and f.get("est_dep", 0) - f.get("sched_dep", 0) >= self.cfg["significant_delay_min"]
        return bool(self.stranded(pid) or late)

    def leaving_on(self, pid):
        """The Customer's flight out of MCI that has not left yet (the leg a counter can change)."""
        for fid in self.w.pax_truth(pid)["itinerary"]:
            f = self.w.flights.get(fid) or {}
            if f.get("origin") == "MCI" and f.get("status") in WAITING_DEP:
                return fid
        return None

    def delayed_enough(self, pid):
        f = self.w.flights.get(self.leaving_on(pid) or "") or {}
        return bool(f) and not f.get("_next_day") and f["est_dep"] - f["sched_dep"] >= self.cfg["rebook_offer_delay_min"]

    def rebooking(self, pid):
        """Later flights today to the Customer's destination with seats left in the reservation system, Passenger Care's own
        advice first and marked recommended. For a Customer whose trip was cancelled or moved to tomorrow, or whose flight out
        of MCI is late (settings views.rebook_offer_delay_min)."""
        stranded = self.stranded(pid)
        if not stranded and not self.delayed_enough(pid):
            return []
        current = None if stranded else self.leaving_on(pid)
        dest = self.w.truth[pid]["destination"]
        rows = self.s.copilot.store.records["care_plans"]
        advice = (rows[-1].get("rebooking_advice") or []) if rows else []
        mine = next((a for a in advice if pid in (a.get("passengers") or [])), None)
        out = []
        for f in sorted(self.w.departures, key=lambda d: d["est_dep"]):
            if f["destination"] != dest or f.get("_next_day") or f["status"] not in WAITING_DEP or f["est_dep"] <= self.w.t                     or f["id"] == current:
                continue
            left = self.w._room(f)                       # the reservation system's own seats left (what a counter sees)
            if left < 1:
                continue
            rec = bool(mine and mine.get("flight_id") == f["id"])
            out.append({"option_id": f"r{len(out) + 1}", "flights": [{"flight_id": f["id"], "dep": fmt(f["est_dep"]),
                                                                       "arr": fmt(f["est_arr"]), "from": f["origin"], "to": f["destination"]}],
                        "arrives": fmt(f["est_arr"]), "seats_left": int(left),
                        "why": "Passenger Care's own advice for this Customer" if rec else "a later flight today with free seats",
                        "recommended": rec})
            if len(out) >= self.cfg["rebooking_max_options"]:
                break
        if out and not any(o["recommended"] for o in out):      # no advice from Passenger Care: the earliest arrival
            first = min(out, key=lambda o: o["arrives"])
            first.update(recommended=True, why="arrives earliest of the flights with free seats")
        out.sort(key=lambda o: not o["recommended"])
        for i, o in enumerate(out, 1):
            o["option_id"] = f"r{i}"
        return out

    # ------------------------------------------------------------ the station, the clocks, the handover
    def station(self):
        from backend.agents.station_staff.station import load_settings as station_settings
        S = station_settings()["gates"]
        now = self.s.now()
        closed = self.s.humans.closed_gates if self.s.humans else set()
        rows = self.s.copilot.store.records["station_plans"]
        plan = rows[-1] if rows else {}
        last_ramp = next((e for e in reversed(self.w.events) if e["type"] in ("ramp_closed", "ramp_reopened")), None)
        closed_now = self.w.ramp_until is not None
        ramp = {"open": not closed_now, "reason": ((((last_ramp or {}).get("payload") or {}).get("reason") or "lightning")
                                                   if closed_now else ""), "since": fmt(last_ramp["t"]) if last_ramp else ""}
        busy = {}
        for g in plan.get("gates") or []:
            if g.get("gate") and g["from"] <= now.isoformat() < g["to"]:
                busy[g["gate"]] = g["flight_id"]
        staffed = {r["flight_id"] for r in (self.s.copilot.station.staff_ledger if self.s.copilot.station else [])
                   if r.get("kind") == "assign" and r.get("flight_id") and r["from_at"] <= now.isoformat() < r["to_at"]}
        gates = []
        for name in [f"G{i + 1:02d}" for i in range(S["southwest_gates"])] + [f"R{i + 1}" for i in range(S["remote_stands"])]:
            fid = busy.get(name)
            gates.append({"gate": name, "status": "closed" if name in closed else "in_use" if fid else "open", "flight_id": fid,
                          "staffed": bool(fid and fid in staffed), "note": closed.get(name, "") if isinstance(closed, dict) else ""})
        gaps = [{"role": x["role"], "from": fmt_iso(x["from"]), "to": fmt_iso(x["to"]), "short_by": x["short_by"]}
                for x in plan.get("staff_shortages") or [] if x["to"] > now.isoformat()]
        return {"ramp": ramp, "gates": gates, "staffing_gaps": gaps}

    def clocks(self):
        limits = read_limits()
        t = self.w.t
        tarmac = [{"flight_id": f["id"], "minutes": t - f["tarmac_since"], "limit": int(limits["tarmac_domestic_min"])}
                  for f in self.w.flights.values() if f.get("tarmac_since") is not None]
        crew = []
        for f in self.w.departures:
            if f["status"] not in WAITING_DEP or f.get("_next_day"):
                continue
            if f.get("tarmac_since") is not None:
                continue                                 # boarded and waiting on the tarmac: the tarmac clock applies
            legal = self.crew_legal_until(f)
            if legal is None or legal < t or legal - t > self.cfg["crew_clock_within_min"]:
                continue                                 # already past it: today's crew check replaces the crew or cancels
            crew.append({"crew_id": (f.get("_role_crew") or {}).get("pilot") or f.get("crew_id"), "flight_id": f["id"],
                         "legal_until": fmt(legal), "minutes_left": int(legal - t)})
        crew.sort(key=lambda c: c["minutes_left"])
        auto = {pid for q in (self.s.humans.questions if self.s.humans else []) if q.status == "auto" and q.action == "offer_refund"
                for pid in q.passenger_ids}
        refunds = []
        for pid in sorted(self.w.truth):
            if not self.refund_due(pid):
                continue
            since = self.s.stranded_since.get(pid, t)
            refunds.append({"customer_id": pid, "flight_id": self.first_bad_flight(pid),
                            "due_by": f"{self.cfg['refund_due_days']} business days after the Customer asks (from {fmt(since)})",
                            "status": "auto_issued" if pid in auto else "due"})
        helped = {r.get("passenger_id") for r in (self.s.copilot.station.staff_ledger if self.s.copilot.station else [])
                  if r.get("passenger_id")}
        special = []
        for pid in sorted(self.w.truth):
            need = NEEDS.get(self.w.passengers[pid].get("segment"))
            if not need or pid in helped:
                continue
            since = self.s.disrupted_since.get(pid)
            if since is None:
                continue
            special.append({"customer_id": pid, "need": need, "flight_id": self.current_flight(pid), "waiting_min": int(t - since)})
        return {"tarmac": tarmac, "crew": crew, "refunds": refunds, "special_assistance": special}

    def first_bad_flight(self, pid):
        old = self.s.first_trip.get(pid) or self.w.truth[pid]["itinerary"]
        return next((f for f in old if self.w.flights.get(f, {}).get("status") == "cancelled"), (old or [None])[0])

    def handover(self, since):
        h = self.s.humans
        qs = h.questions if h else []
        start = since if since is not None else 0
        answered = [q for q in qs if q.answered_at is not None and str((q.answer or {}).get("by", "")).startswith("human:")
                    and _min(q.answered_at) >= start]
        auto = [q for q in qs if q.status == "auto" and _min(q.asked_at) >= start]
        open_ = [{"id": i["id"], "headline": i["headline"], "deadline": i["deadline"], "section": i["section"],
                  "bundle": i["bundle"]["title"]} for i in self.s.queue_items()]
        c = self.clocks()
        watch = []
        st = self.s.state()
        if st["world"]["ground_stop"]:
            watch.append(f"The ground stop is still on (expected to end about {st['world']['stop_expected_end']}).")
        if not st["world"]["ramp_open"]:
            watch.append("The ramp is closed.")
        for x in c["tarmac"]:
            if x["minutes"] >= self.cfg["watch_tarmac_min"]:
                watch.append(f"{x['flight_id']} has been on the tarmac {x['minutes']} minutes (limit {x['limit']}).")
        for x in c["crew"][:3]:
            watch.append(f"{x['flight_id']}'s crew is legal only until {x['legal_until']} ({x['minutes_left']} minutes).")
        urgent = [o for o in open_ if o["section"] == "urgent"]
        if urgent:
            watch.append(f"{len(urgent)} urgent decision{'s' if len(urgent) != 1 else ''} waiting, first: {urgent[0]['headline']}.")
        text = (f"Since {fmt(start)}: {len(answered)} decision{'s' if len(answered) != 1 else ''} made, {len(auto)} done "
                f"automatically, {len(open_)} still open. Wrong screens now: {st['truth']['wrong_now']} Customers "
                f"(today's process at this minute: {st['truth']['today_wrong_now']}).")
        return {"since": fmt(start), "decisions": len(answered), "open": open_, "watch": [clean(x) for x in watch],
                "auto_done": len(auto), "text": clean(text)}


def fmt_iso(v):
    from backend.execution.live_trace import hhmm
    return hhmm(v)


def _min(t):
    from backend.execution.live_trace import minute
    return minute(t)
