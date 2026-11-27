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
        """When this flight's crew stops being legal for this duty (minutes after midnight): their duty left runs out. The
        world burns a waiting crew's duty from the flight's scheduled departure on, so the duty ends at the later of now and the
        scheduled departure plus the duty left (the first of pilots and flight attendants to run out)."""
        from simulator.world import CREW_ROLES
        best = None
        for role, field, _ in CREW_ROLES:
            crew = self.w._crew_for(f, role)
            if crew is None or crew.get(field) is None:
                continue
            m = max(self.w.t, f["sched_dep"]) + crew[field]
            best = m if best is None else min(best, m)
        return best

    def gate_of(self, fid):
        rows = self.s.copilot.store.records["station_plans"]
        gates = (rows[-1].get("gates") or []) if rows else []
        g = next((x for x in gates if x.get("flight_id") == fid), None)
        if g is None:                                   # a departure leaves from the gate its inbound plane holds
            f = self.w.flights.get(fid) or {}
            ids = {f["_inbound"]} if f.get("_inbound") else                 {a["id"] for a in self.w.arrivals if a.get("aircraft_id") == f.get("aircraft_id") and a["sched_arr"] <= f.get("sched_dep", 0)}
            g = next((x for x in reversed(gates) if x.get("flight_id") in ids and x.get("gate")), None)
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
                "script": script, "notes": list(self.s.notes.get(fid, [])), "on_screens": shown,
                "truth": self.truth_of(f), "one_story": self.one_story(fid)}

    STATUS_WORDS = {"scheduled": "on time", "ground_stop": "waiting for the ground stop to lift", "delayed": "delayed",
                    "held": "held at MCI", "departed": "on its way", "arrived": "arrived", "cancelled": "cancelled",
                    "diverted": "diverted", "airborne": "in the air", "holding": "holding in the air"}

    def truth_of(self, f):
        """The flight's real status now, in one sentence, from the operational record."""
        city = self.s.plain.cities.get(f["destination"] if f["origin"] == "MCI" else f["origin"], "") if self.s.plain else ""
        where = f"{f['id']} {'to' if f['origin'] == 'MCI' else 'from'} {city}".strip()
        words = self.STATUS_WORDS.get(f["status"], f["status"].replace("_", " "))
        bits = [f"{where} is {words}"]
        if f["status"] in WAITING_DEP and f.get("est_dep") is not None:
            late = f["est_dep"] - f["sched_dep"]
            bits.append(f"now expected to leave at {fmt(f['est_dep'])}" + (f" ({late} min late)" if late > 0 else ""))
        elif f["status"] == "diverted" and f.get("diverted_to"):
            bits.append(f"to {f['diverted_to']}")
        elif f["status"] in ("departed", "arrived") and f.get("actual_dep") is not None:
            bits.append(f"it left at {fmt(f['actual_dep'])}")
        return ", ".join(bits) + "."

    def last_change(self, fid):
        """The last minute the flight's status or time changed (the record a message must have caught up with)."""
        kinds = ("flight_delayed", "flight_delay_updated", "flight_cancelled", "flight_departed", "flight_diverted", "flight_arrived",
                 "flight_resumed")
        return max((e["t"] for e in self.w.events if e["type"] in kinds and (e.get("payload") or {}).get("flight_id") == fid),
                   default=None)

    def one_story(self, fid):
        """What every place is saying about this flight now, word for word, and whether it agrees with the truth.
        Screens: what most of the flight's tracked Customers see (the screen's own text); staff places: the latest staff note
        or crew message about this flight. A place nothing was sent to is null."""
        out = []
        pids = self.tracked_on(fid)
        for ch in ("app", "sms", "gate_screen", "call_center"):
            have = [p for p in pids if ch in self.s.channels.screens.get(p, ())]
            if not have:
                out.append({"place": ch, "says": None, "matches": None, "since": None})
                continue
            texts = Counter(self.s.channels.shown[(p, ch)].get("message") for p in have)
            says = texts.most_common(1)[0][0]
            showing = [p for p in have if self.s.channels.shown[(p, ch)].get("message") == says]
            ok = not any((p, ch, k) in self.s.channels.dis for p in showing for k in ("itinerary", "status", "destination"))
            since = min((self.s.channels.shown[(p, ch)].get("updated_at") for p in showing
                         if self.s.channels.shown[(p, ch)].get("updated_at") is not None), default=None)
            out.append({"place": ch, "says": says, "matches": ok, "since": fmt(since) if since is not None else None})
        changed = self.last_change(fid)
        note = self._latest_sent(fid, ("staff",))
        crew = next((e for e in reversed(self.w.events) if e["type"] == "crew_message_delivered"
                     and (e.get("payload") or {}).get("flight_id") == fid), None)
        for place, sent in (("counter", None), ("gate_agents", note), ("crew", (crew["t"], crew["payload"].get("text")) if crew else None)):
            if sent is None:
                out.append({"place": place, "says": None, "matches": None, "since": None})
            else:
                out.append({"place": place, "says": sent[1], "matches": changed is None or sent[0] >= changed, "since": fmt(sent[0])})
        return out

    def _latest_sent(self, fid, audiences):
        """(minute, text) of the newest staff note about this flight that was sent."""
        from backend.execution.live_trace import minute
        best = None
        for h in self.s.copilot.recovery.history:
            if h.get("flight_id") == fid and h.get("status") == "sent" and h.get("audience") in audiences:
                t = minute(_dt(h.get("as_of") or h.get("created")))
                if best is None or t >= best[0]:
                    best = (t, h.get("text"))
        return best

    # ------------------------------------------------------------ 6. everything sent, newest last
    def sent(self, since=0):
        """Every message delivered, staff note, crew message and refund / hotel / rebooking confirmation since a minute. A mass
        send (one recommendation, one channel, one minute) is one row with its count; the text is its first message's words."""
        names = {pid: p.get("full_name") for pid, p in self.w.passengers.items()}
        log = {m["id"]: m for m in self.s.copilot.messages.log if m.get("id")}
        rows, groups = [], {}
        for e in self.w.events:
            if e["t"] < since:
                continue
            p, k = e.get("payload") or {}, e["type"]
            if k == "message_delivered":
                key = (e["t"], p.get("recommendation"), p.get("proposal_id"), p.get("channel"), p.get("type"))
                g = groups.get(key)
                if g is None:
                    g = groups[key] = {"t": fmt(e["t"]), "minute": e["t"], "to": None, "channel": p.get("channel"), "kind": "message",
                                       "text": p.get("text"), "count": 0, "_pids": [], "_flights": set()}
                    rows.append(g)
                g["count"] += 1
                g["_pids"].append(p.get("passenger_id"))
                m = log.get(p.get("message_id")) or {}
                facts = m.get("trip_facts") or {}
                g["_flights"] |= set(facts.get("itinerary") or [])
            elif k == "staff_note_published":
                rows.append({"t": fmt(e["t"]), "minute": e["t"], "to": "Gate agents and call center", "channel": "staff_note",
                             "kind": "message", "text": p.get("text"), "count": 1})
            elif k == "crew_message_delivered":
                rows.append({"t": fmt(e["t"]), "minute": e["t"], "to": f"Crew {p.get('crew_id')}" if p.get("crew_id") else "Crew",
                             "channel": "crew", "kind": "message", "text": p.get("text"), "count": 1})
            elif k == "care_decision_recorded" and p.get("action") == "offer_refund":
                who = [names.get(x) or x for x in p.get("passenger_ids") or []]
                ids = p.get("passenger_ids") or []
                rows.append({"t": fmt(e["t"]), "minute": e["t"], "to": ", ".join(who) or None, "channel": None, "kind": "refund",
                             "text": "Refund offered if the new trip is declined (Passenger Care's legal-floor refund).",
                             "count": max(1, len(who)), "customer_id": ids[0] if len(ids) == 1 else None})
            elif k == "hotel_room_assigned":
                rows.append({"t": fmt(e["t"]), "minute": e["t"], "to": names.get(p.get("passenger_id")) or p.get("passenger_id"),
                             "channel": None, "kind": "hotel", "text": f"Room booked at {self._hotel_name(p.get('hotel_id'))}.",
                             "count": 1, "customer_id": p.get("passenger_id")})
            elif (k == "decision_applied" and p.get("action") == "rebook_passenger") or k == "customer_rebooked":
                moved = p.get("moved") or p.get("facts", {}).get("passenger_ids") or [p.get("passenger_id")]
                to = (p.get("facts") or {}).get("flight_id") or p.get("flight_id")
                who = [names.get(x) or x for x in moved if x]
                ids = [x for x in moved if x]
                rows.append({"t": fmt(e["t"]), "minute": e["t"], "to": who[0] if len(who) == 1 else f"{len(who)} Customers",
                             "channel": None, "kind": "rebook", "text": f"Rebooked onto {to}.", "count": max(1, len(who)),
                             "customer_id": ids[0] if len(ids) == 1 else None})
        merged = []                                       # confirmations of one kind in one minute: one row with its count
        for r in rows:
            if r["kind"] in ("refund", "hotel", "rebook") and merged and merged[-1]["kind"] == r["kind"]                     and merged[-1]["minute"] == r["minute"] and merged[-1].get("text") == r.get("text"):
                m = merged[-1]
                m["count"] += r["count"]
                m["_who"] = m.get("_who", [m["to"]]) + [r["to"]]
                m["to"] = f"{m['count']} Customers"
                m["customer_id"] = None
                continue
            merged.append(r)
        for m in merged:
            m.pop("_who", None)
        rows = merged
        for g in groups.values():
            pids, fl = g.pop("_pids"), g.pop("_flights")
            if g["count"] == 1:
                g["to"] = names.get(pids[0]) or pids[0]
                g["customer_id"] = pids[0]
            else:
                one = sorted(fl)[0] if len(fl) == 1 else None
                g["to"] = f"{g['count']} Customers" + (f" on {one}" if one else "")
        rows.sort(key=lambda r: r["minute"])
        for r in rows:
            r.setdefault("customer_id", None)
            r.pop("_one", None)
        return rows

    def _hotel_name(self, hid):
        care = self.s.copilot.care
        h = next((x for x in (care.context.rows.get("hotels", []) if care else []) if x.get("id") == hid), None)
        return (h or {}).get("name") or hid

    # ------------------------------------------------------------ 1. a Passenger Care question: who, what and the words
    CARE_ACTIONS = {"rebook_passenger": "rebook", "issue_compensation": "hotel", "offer_refund": "refund",
                    "arrange_ground_transport": "ride"}

    def care_block(self, q):
        """For a Passenger Care question: each Customer, what is proposed for them (flights, hotel, amount) and why; the exact
        words they will get if a message already exists for it; and what Passenger Care already did on its own."""
        if q.agent != "passenger_care":
            return None
        from backend.execution.live_plain import clean
        care = self.s.copilot.care
        rec = care.context.saved.get(("recommendations", q.rec_id)) or {}
        opt = (rec.get("options") or [{}])[0]
        p = q.proposal or {}
        action = self.CARE_ACTIONS.get(q.action)
        if q.action == "issue_compensation" and p.get("kind") != "assign":
            action = None                                    # a compensation question that is not a room (who pays): no Customer line
        hotels = {h["id"]: h for h in care.context.rows.get("hotels", [])}
        customers = []
        for pid in q.passenger_ids if action else []:
            detail = {"flights": [], "hotel": None, "amount": None, "currency": "USD"}
            if action == "rebook" and opt.get("flight_id") in self.w.flights:
                f = self.w.flights[opt["flight_id"]]
                detail["flights"] = [{"flight_id": f["id"], "dep": fmt(f.get("est_dep")), "arr": fmt(f.get("est_arr"))}]
            elif action == "hotel":
                h = hotels.get(p.get("hotel_id")) or {}
                detail["hotel"] = {"name": h.get("name"), "price": opt.get("cost_usd"), "shuttle": h.get("shuttle")}
                detail["amount"] = opt.get("cost_usd")
            elif action == "ride":
                detail["amount"] = opt.get("cost_usd")             # the whole ride (shared when several Customers go together)
            customers.append({"id": pid, "name": (self.w.passengers.get(pid) or {}).get("full_name"), "action": action,
                              "detail": detail, "why": clean(rec.get("why") or "")})
        msg = next((m for m in self.s.copilot.messages.log if str(m.get("backed_by")) == str(q.rec_id)), None)
        auto = {}
        for a in self.s.humans.questions:
            if a.status != "auto" or a.agent != "passenger_care":
                continue
            r = care.context.saved.get(("recommendations", a.rec_id)) or {}
            k = self.CARE_ACTIONS.get(a.action, a.action)
            x = auto.setdefault(k, {"action": k, "count": 0, "total": 0})
            x["count"] += 1
            x["total"] += float(((r.get("options") or [{}])[0]).get("cost_usd") or 0)
        return {"customers": customers, "message_text": (msg or {}).get("text"), "auto_done": list(auto.values())}

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
        """Each leg as the operational record has it, and the connection to the next leg (at risk when it is shorter than the
        minimum connection time in the rules)."""
        mct = int(read_limits()["min_connection_min"])
        itin = list(self.w.pax_truth(pid)["itinerary"])
        out = []
        for i, fid in enumerate(itin):
            f = self.w.flights.get(fid) or {}
            conn = None
            if i + 1 < len(itin) and itin[i + 1] in self.w.flights:
                nxt = self.w.flights[itin[i + 1]]
                gap = (nxt.get("est_dep") or nxt["sched_dep"]) - (f.get("est_arr") or f.get("sched_arr") or 0)
                conn = {"to_flight": nxt["id"], "minutes": int(gap), "at_risk": gap < mct}
            out.append({"flight_id": fid, "from": f.get("origin"), "to": f.get("destination"), "dep": fmt(f.get("est_dep")),
                        "arr": fmt(f.get("est_arr")), "sched_dep": fmt(f.get("sched_dep")), "sched_arr": fmt(f.get("sched_arr")),
                        "est_dep": fmt(f.get("est_dep")), "est_arr": fmt(f.get("est_arr")),
                        "gate": self.gate_of(fid) if "MCI" in (f.get("origin"), f.get("destination")) else None,
                        "status": f.get("status"), "connection": conn})
        return out

    def international(self, pid):
        """True if the Customer is in the international group or a leg goes to or from a station recorded as international;
        False if every station on the trip has a recorded region; None if one is not recorded."""
        if self.w.passengers[pid].get("segment") == "international":
            return True
        region = {x["code"]: x.get("region") for x in self.w.stations.values()} if isinstance(self.w.stations, dict) else \
            {x["code"]: x.get("region") for x in self.w.stations}
        intl = (self.s.s["views"].get("international_regions") or [])
        regions = []
        for fid in self.w.pax_truth(pid)["itinerary"]:
            f = self.w.flights.get(fid) or {}
            for code in (f.get("origin"), f.get("destination")):
                if code and code != "MCI":
                    regions.append(region.get(code))
        if any(r in intl for r in regions if r):
            return True
        return None if any(r is None for r in regions) or not regions else False

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
        return {"id": pid, "name": p.get("full_name"), "trip": self.trip(pid), "true_status": screen_text(t), "screens": screens,
                "needs": [NEEDS[p["segment"]]] if p.get("segment") in NEEDS else [],
                "rebooking": self.rebooking(pid), "hotel": self.hotel(pid), "refund_due": self.refund_due(pid),
                "messages": self.messages_to(pid), "refunds": self.refunds(pid), "international": self.international(pid)}

    def messages_to(self, pid):
        """The exact words this Customer received: every message delivered to one of their channels."""
        return [{"t": fmt(e["t"]), "channel": e["payload"].get("channel"), "text": e["payload"].get("text")}
                for e in self.w.events if e["type"] == "message_delivered" and e["payload"].get("passenger_id") == pid]

    def _care_rec(self, key):
        care = self.s.copilot.care
        rid = care.memory.proposed.get(key) if care else None
        return (rid, care.context.saved.get(("recommendations", rid)) or {}) if rid is not None else (None, {})

    def issued(self, rid):
        """When Passenger Care's decision was carried out (its care_decision_recorded confirmation), or None."""
        e = next((e for e in self.w.events if e["type"] == "care_decision_recorded" and e["payload"].get("recommendation") == rid), None)
        return e["t"] if e else None

    def refunds(self, pid):
        """Passenger Care's refund offer for this Customer: issued once its decision was carried out, else due if one is owed.
        The amount is the ticket price, which is not on record, so it is null."""
        rid, rec = self._care_rec(("refund", pid))
        if rid is None:
            return [{"amount": None, "status": "due", "t": None}] if self.refund_due(pid) else []
        t = self.issued(rid)
        if t is not None:
            return [{"amount": None, "status": "issued", "t": fmt(t)}]
        return [{"amount": None, "status": "due", "t": None}] if self.refund_due(pid) else []

    def hotel(self, pid):
        """The room Passenger Care planned (name, price, shuttle from the hotel list it was given) and where it stands."""
        care = self.s.copilot.care
        entry = self.care_entry(pid) or {}
        hid, rid = (care.memory.pending_assign.get(pid) if care else None) or (None, None)
        assigned = next((e for e in reversed(self.w.events) if e["type"] == "hotel_room_assigned"
                         and e["payload"].get("passenger_id") == pid), None)
        if assigned is not None:
            hid = assigned["payload"].get("hotel_id")
        if hid is None and not entry.get("hotel_name"):
            return None
        hotels = {h["id"]: h for h in (care.context.rows.get("hotels", []) if care else [])}
        h = hotels.get(hid) or next((x for x in hotels.values() if x.get("name") == entry.get("hotel_name")), {})
        rec = (care.context.saved.get(("recommendations", rid)) or {}) if care and rid is not None else {}
        price = ((rec.get("options") or [{}])[0]).get("cost_usd")
        return {"name": h.get("name") or entry.get("hotel_name"), "price": price if price is not None else h.get("rate_usd"),
                "currency": "USD", "shuttle": h.get("shuttle") or entry.get("shuttle"),
                "status": "assigned" if assigned is not None else entry.get("hotel_status")}

    def request_options(self, pid):
        """Any Customer may ask to change their flight out of MCI that has not left, even on time: every other flight to their
        final destination with seats left (today, or tomorrow's flights already on the schedule), ranked by the earliest
        arrival, then fewer connections, then the same day. Nothing is invented: times and seats are the reservation system's."""
        leg = self.leaving_on(pid)
        if leg is None:
            return []
        t = self.w.pax_truth(pid)
        dest = t["destination"]
        cur = self.w.flights[leg]
        itin = list(t["itinerary"])
        before = itin[: itin.index(leg)]
        mct = int(read_limits()["min_connection_min"])
        ready = self.w.t
        if before and before[-1] in self.w.flights:              # still to land at MCI: the new flight must wait for them
            prev = self.w.flights[before[-1]]
            if prev.get("status") != "arrived":
                ready = max(ready, (prev.get("est_arr") or prev["sched_arr"]) + mct)
        local = bool(self.w.passengers[pid].get("lives_locally"))
        city = self.s.plain.cities.get(dest, dest) if self.s.plain else dest
        out = []
        for f in self.w.departures:
            if f["destination"] != dest or f["id"] == cur["id"] or f["status"] not in WAITING_DEP or f["est_dep"] <= ready:
                continue
            seats = self.w._room(f)
            if seats < 1:
                continue
            next_day = bool(f.get("_next_day")) or f["est_dep"] >= 1440
            why = (f"Arrives in {city} at {fmt(f['est_arr'])}{' tomorrow' if next_day else ' today'}, nonstop, "
                   f"{seats} seat{'s' if seats != 1 else ''} left")
            if next_day:
                why += "; overnight: " + ("the Customer lives locally and can go home" if local else "a hotel night is needed")
            out.append({"flights": [{"flight_id": f["id"], "dep": fmt(f["est_dep"]), "arr": fmt(f["est_arr"]), "from": f["origin"],
                                     "to": f["destination"]}],
                        "arrives": fmt(f["est_arr"]) + (" (tomorrow)" if next_day else ""), "seats_left": int(seats), "why": why,
                        "_key": (f["est_arr"], 0, next_day, f["id"])})
        out.sort(key=lambda o: o.pop("_key"))
        labels = self.cfg["option_labels"]
        for i, o in enumerate(out, 1):
            o.update(option_id=f"c{i}", rank=i, label=labels[i - 1] if i <= len(labels) else f"Option {i}", recommended=i == 1)
        return out[: self.cfg["request_max_options"]]

    # ------------------------------------------------------------ 2. who sees wrong information, by flight
    def out_of_date(self):
        """Customers whose screens disagree with the truth now (the channels' own count, the same as truth.wrong_now), by the
        flight they are on, with each wrong screen and since when; ten minutes ago from the saved minute rows."""
        ch = self.s.channels
        by = {}
        for (pid, channel, field) in ch.dis:
            if field == "bag_routing" and (pid not in ch.dis_pax):
                continue
            fid = self.current_flight(pid)
            row = by.setdefault(fid, {"customers": set(), "channels": {}})
            row["customers"].add(pid)
            since = self.s.wrong_since.get((pid, channel))
            if since is not None:
                row["channels"][channel] = min(row["channels"].get(channel, since), since)
            else:
                row["channels"].setdefault(channel, None)
        flights = []
        for fid, row in by.items():
            f = self.w.flights.get(fid)
            flights.append({"flight_id": fid, "label": self.s.plain.flight(fid) if self.s.plain else fid,
                            "customers": len(row["customers"]),
                            "channels": [{"channel": c, "since": fmt(m) if m is not None else None}
                                         for c, m in sorted(row["channels"].items(), key=lambda kv: (kv[1] is None, kv[1] or 0))],
                            "truth": self.truth_of(f) if f else None})
        flights.sort(key=lambda r: (-r["customers"], r["flight_id"] or ""))
        ago = next((r["wrong_now"] for r in reversed(self.s.minutes) if r["minute"] <= self.w.t - 10), None)
        return {"total": len(ch.dis_pax), "ten_min_ago": ago, "flights": flights}

    # ------------------------------------------------------------ 6. a flight's Customers
    def flight_customers(self, fid):
        f = self.w.flights.get(fid)
        if f is None:
            return None
        now = {p: self.w.pax_truth(p) for p in self.w.truth}        # the bookings as they stand this minute
        pids = sorted({p for p, t in now.items() if fid in t["itinerary"]} | {p for p, it in self.s.first_trip.items() if fid in it})
        out = []
        for pid in pids:
            p = self.w.passengers[pid]
            legs = self.trip(pid)
            leg = next((x for x in legs if x["flight_id"] == fid), None)
            opts = self.request_options(pid) if fid in now[pid]["itinerary"] else []
            out.append({"id": pid, "name": p.get("full_name"), "status": now[pid]["status"],
                        "needs": [NEEDS[p["segment"]]] if p.get("segment") in NEEDS else [],
                        "international": self.international(pid), "connection": (leg or {}).get("connection"),
                        "best_option": opts[0] if opts else None,
                        "rebooked": fid in self.s.first_trip.get(pid, []) and fid not in now[pid]["itinerary"]})
        return {"flight": {"id": fid, "from": f["origin"], "to": f["destination"], "sched_dep": fmt(f["sched_dep"]),
                           "est_dep": fmt(f.get("est_dep")), "status": f["status"], "gate": self.gate_of(fid),
                           "truth": self.truth_of(f)},
                "customers": out}

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
        for d in self.at_gate():                          # a departure still at its gate (held, delayed...) holds it until it goes
            gate = self.gate_of(d["id"])
            if gate:
                busy[gate] = d["id"]
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

    def at_gate(self):
        """Departures at their gate now: not gone yet (scheduled, ground stop, delayed or held) and their plane is here (the
        inbound landed, or an overnight plane), whatever the scheduled time says."""
        tow = self.w.ground["tow_to_gate_before_min"]
        out = []
        for d in self.w.departures:
            if d["status"] not in WAITING_DEP or d.get("_next_day"):
                continue
            inb = self.w.flights.get(d["_inbound"]) if d.get("_inbound") else None
            if (inb is not None and inb["status"] == "arrived") or (inb is None and self.w.t >= d["sched_dep"] - tow):
                out.append(d)
        return out

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


def _dt(v):
    from datetime import datetime
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def fmt_iso(v):
    from backend.execution.live_trace import hhmm
    return hhmm(v)


def _min(t):
    from backend.execution.live_trace import minute
    return minute(t)
