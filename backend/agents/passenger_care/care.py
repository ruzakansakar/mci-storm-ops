"""The Passenger Care agent (Phase 4e).

Every run (every few minutes and when a trigger event is delivered) it rebuilds, from the events up to T, who needs care and what
was already arranged, reads the Situation and Network Recovery pictures (shared state) and proposes:

  a. who needs care: cancelled, misconnected, stranded overnight (trip records), and children alone / wheelchair users on an
     affected flight;
  b. overnight (M01-M07, M09, M10): locals home with transport (M04); everyone else a hotel room: blocks held early (automatic up
     to autonomy.yaml max_rooms, then approval), then one room per passenger (approval), chosen by rooms left, preference,
     shuttle, distance, last check-in and rate; accessible rooms for wheelchair users; transport sized from transport_options;
     holds nobody needs are released;
  c. compensation (N01-N05) with backend/rules/compensation.py: the legal floor (a refund if they decline rebooking) is always
     offered; Southwest's commitments (meal, $75 voucher, hotel cost) only once a person has decided the cause was controllable
     (A03). Until then they are "pending the supervisor's controllability decision" and a person is asked, never promised.
     Goodwill always needs approval. Every cost is tracked against a cap;
  d. vulnerable passengers (J01-J03): a child alone is supervised by staff and the guardian contacted, never sent to a hotel
     alone, rebooked nonstop only; a wheelchair user gets an accessible room and a help request for Station & Staff (J04 is
     theirs: written into the care plan);
  e. rebooking advice (K01, K03, K05, J02): a better flight than today's where seats exist, vulnerable first, families together.
     ADVISORY: recorded, bookings are never changed;
  f. one care_plans version (shared, append-only) whenever the plan changes, with the Situation and recovery plan versions used.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml

from backend.rules.compensation import compensation_for
from backend.settings import ROOT, SIM_DATE, TZ

DAY0 = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, tzinfo=TZ)
VULNERABLE = ("child_alone", "wheelchair")


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "agent_settings" / "passenger_care.yaml").read_text(encoding="utf-8"))


def clock(text: str) -> datetime:
    """'13:00', '24:00' or '06:30+1' on the simulated day (its own copy: agents never import each other's code)."""
    day = 0
    if "+" in text:
        text, plus = text.split("+")
        day = int(plus)
    h, m = text.split(":")
    return DAY0 + timedelta(days=day, hours=int(h), minutes=int(m))


def hhmm(t) -> str:
    return t.astimezone(TZ).strftime("%H:%M") if t else "-"


@dataclass
class Memory:
    static: dict | None = None
    version: int = 0
    last_key: object = None
    proposed: dict = field(default_factory=dict)     # key -> recommendation id
    asked: set = field(default_factory=set)
    pending_rooms: dict = field(default_factory=dict)  # hotel -> rooms in hold proposals not confirmed yet
    pending_assign: dict = field(default_factory=dict)  # pid -> (hotel, recommendation)
    plans: list = field(default_factory=list)


def _state(events):
    """What has happened by T, from the events only."""
    st = {"trips": {}, "cancelled": {}, "stranded": {}, "misconnect": {}, "next_day": {}, "departed": set(), "a03": {},
          "held": {}, "held_accessible": {}, "released": {}, "assigned": {}, "approved": {}}
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        p, typ = e.get("payload") or {}, e["type"]
        if typ == "trip_record_updated" and p.get("passenger_id"):
            prev = st["trips"].get(p["passenger_id"])
            st["trips"][p["passenger_id"]] = {"itinerary": list(p.get("itinerary") or []), "status": p.get("status"),
                                              "destination": p.get("destination"), "at": e["occurred_at"],
                                              "first": (prev or {}).get("first") or {"itinerary": list(p.get("itinerary") or []),
                                                                                    "status": p.get("status")}}
        elif typ == "flight_cancelled" and p.get("flight_id"):
            st["cancelled"][p["flight_id"]] = {"reason": p.get("reason"), "at": e["occurred_at"]}
        elif typ == "flight_departed" and p.get("flight_id"):
            st["departed"].add(p["flight_id"])
        elif typ == "next_day_flight_created" and p.get("flight_id"):
            st["next_day"][p["flight_id"]] = p
        elif typ == "passenger_stranded_overnight" and p.get("passenger_id"):
            st["stranded"][p["passenger_id"]] = p
        elif typ == "missed_connection" and p.get("passenger_id"):
            st["misconnect"][p["passenger_id"]] = p
        elif typ == "human_decision_recorded" and p.get("decision_id") == "A03" and p.get("flight_id"):
            st["a03"][p["flight_id"]] = p.get("answer_side")
        elif typ == "hotel_rooms_held":
            k = "held_accessible" if p.get("accessible") else "held"
            st[k][p["hotel_id"]] = st[k].get(p["hotel_id"], 0) + int(p.get("rooms") or 0)
        elif typ == "hotel_rooms_released":
            st["released"][p["hotel_id"]] = st["released"].get(p["hotel_id"], 0) + int(p.get("rooms") or 0)
        elif typ == "hotel_room_assigned" and p.get("passenger_id"):
            st["assigned"][p["passenger_id"]] = p
        elif typ == "care_decision_recorded" and p.get("recommendation") is not None:
            st["approved"][p["recommendation"]] = p
    return st


class Care:
    def __init__(self, ctx, memory: Memory, s: dict):
        self.ctx, self.m, self.s, self.now = ctx, memory, s, ctx.now
        if memory.static is None:
            memory.static = {
                "flights": {f["id"]: f for f in ctx.read("flights")},
                "pax": {p["id"]: p for p in ctx.read("passengers", ["id", "segment", "final_destination", "lives_locally",
                                                                     "loyalty_tier"])},
                "hotels": ctx.read("hotels"), "transport": ctx.read("transport_options"),
                "later": {r["destination"]: r for r in ctx.read("later_capacity")}}
        self.static = memory.static
        self.proposals, self.costs = [], {"committed_usd": 0, "pending_usd": 0, "cap_usd": s["money"]["cost_cap_usd"]}
        self.sources = [{"fact": "trip records", "source": "reservations", "as_of": ctx.feed_last("reservations").isoformat()},
                        {"fact": "hotels and transport (published before the day)", "source": "hotel vendor list",
                         "as_of": DAY0.replace(hour=5).isoformat()}]

    # ------------------------------------------------------------ helpers
    def propose(self, key, action, title, why, options, decision_ids, facts, rooms_needed=0, owed=0.0, pids=()):
        if key in self.m.proposed:
            return self.m.proposed[key]
        for name in self.ctx.contract.required_checks.get(action, []):
            self.ctx.run_check(name, facts.get("option") or {})
        out = {"title": title, "why": why, "data_as_of": self.now.isoformat(), "options": options, "sources": self.sources,
               "decision_ids": decision_ids, "owed_total_usd": float(owed), "hotel_rooms_needed": int(rooms_needed),
               "passenger_ids": list(pids)}
        rid = self.ctx.propose(action, out)
        self.m.proposed[key] = rid
        self.proposals.append({"agent": "passenger_care", "action": action, "decision_ids": decision_ids, "data_as_of": self.now,
                               "id": rid, "passenger_ids": list(pids), **facts})
        return rid

    def ask(self, key, decision, facts, reason):
        if key not in self.m.asked:
            self.m.asked.add(key)
            self.ctx.ask_human(decision, facts, reason)

    def rank(self, h):
        H = self.s["hotels"]
        pref = H["preference_order"].index(h["use_preference"]) if h["use_preference"] in H["preference_order"] else 9
        shut = H["shuttle_order"].index(h["shuttle"]) if h["shuttle"] in H["shuttle_order"] else 9
        return (pref, shut, h["distance_miles"], h["rate_usd"], h["id"])

    def check_in_ok(self, h):
        last = clock(h["last_checkin"]) if h.get("last_checkin") else None
        return last is None or self.now + timedelta(minutes=self.s["hotels"]["travel_to_hotel_min"]) <= last

    # ------------------------------------------------------------ the run
    def run(self, events, sit, plan):
        st, S = _state(events), self.s
        self.st = st
        pax, flights = self.static["pax"], self.static["flights"]
        rooms = {h["id"]: h for h in self.static["hotels"]}
        # a. who needs care
        entries = {}
        for pid, t in sorted(st["trips"].items()):
            p = pax.get(pid) or {}
            seg = p.get("segment")
            gone = [f for f in t["first"]["itinerary"] if f not in t["itinerary"] and f in st["cancelled"]]
            needs = []
            if t["status"] == "cancelled" or gone or any(f in st["cancelled"] for f in t["itinerary"]):
                needs.append("cancelled")
            if pid in st["misconnect"]:
                needs.append("misconnect")
            if t["status"] == "overnight" or pid in st["stranded"]:
                needs.append("overnight")
            affected = bool(needs) or t["status"] not in ("confirmed", None)
            if seg in VULNERABLE and affected:
                needs.append(seg)
            if not needs:
                continue
            local = bool((st["stranded"].get(pid) or {}).get("lives_locally", p.get("lives_locally")))
            cancelled_flight = (gone or [f for f in t["itinerary"] if f in st["cancelled"]] or
                                [(st["stranded"].get(pid) or {}).get("from_flight")])[0]
            entries[pid] = {"passenger_id": pid, "needs": needs, "local": local, "trip_status": t["status"],
                            "flight_id": cancelled_flight, "itinerary": t["itinerary"], "since": t["at"].isoformat(),
                            "segment_kind": "vulnerable" if seg in VULNERABLE else "standard"}
        # names: it is working on exactly these passengers now (booking refs are needed to keep families together)
        if entries:
            self.ctx.working_on(sorted(entries))
            refs = {r["id"]: r["booking_ref"] for r in self.ctx.read("passengers", ["id", "booking_ref"], where={"id": sorted(entries)})}
        else:
            refs = {}
        hotels_out = self.hotels(entries, rooms)
        self.transport(entries, rooms)
        for e in entries.values():
            self.compensation(e)
        vulnerable, wheel = self.vulnerable(entries, pax)
        advice = self.rebooking(entries, pax, refs, plan)
        self.release(entries, rooms)
        return {"passengers": list(entries.values()), "hotels": hotels_out, "vulnerable": vulnerable, "wheelchair": wheel,
                "advice": advice}

    # ------------------------------------------------------------ b. hotels and transport
    def hotels(self, entries, rooms):
        st, H = self.st, self.s["hotels"]
        pax = self.static["pax"]
        need = [e for e in entries.values() if "overnight" in e["needs"] and not e["local"]
                and pax.get(e["passenger_id"], {}).get("segment") != "child_alone"]       # a child alone never goes to a hotel alone
        held = lambda hid: st["held"].get(hid, 0) - st["released"].get(hid, 0)
        assigned_to = {}
        for pid, a in st["assigned"].items():
            assigned_to[a["hotel_id"]] = assigned_to.get(a["hotel_id"], 0) + 1
        for pid, (hid, _) in self.m.pending_assign.items():
            if pid not in st["assigned"]:
                assigned_to[hid] = assigned_to.get(hid, 0) + 1
        # confirmed holds replace the pending ones
        for hid in list(self.m.pending_rooms):
            if held(hid) >= self.m.pending_rooms[hid]:
                self.m.pending_rooms.pop(hid)
        free_held = sum(max(held(h) - assigned_to.get(h, 0), 0) for h in rooms) + sum(self.m.pending_rooms.values())
        unassigned = [e for e in need if e["passenger_id"] not in st["assigned"] and e["passenger_id"] not in self.m.pending_assign]
        late = self.now >= clock(H["release_after"])
        want = len(unassigned) + (H["spare_rooms"] if need and not late else 0)   # no spare rooms after the release time
        short = want - free_held
        ranked = sorted((h for h in rooms.values() if self.check_in_ok(h)), key=self.rank)
        for h in ranked:                                  # M01/M02/M03: hold a block at the best hotels with rooms left
            if short <= 0:
                break
            free = h["rooms_available"] - max(held(h["id"]), 0) - self.m.pending_rooms.get(h["id"], 0)   # never more than it has
            n = min(free, short)
            if n <= 0:
                continue
            rid = self.propose(("hold", h["id"], self.now), "hold_hotel_rooms", f"Hold {n} rooms at {h['name']}",
                               f"{len(need)} passengers need a room tonight (+{H['spare_rooms']} spare)",
                               [{"id": f"hold-{h['id']}", "summary": f"{n} rooms at {h['name']} (${h['rate_usd']})",
                                 "cost_usd": 0}], ["M01", "M02", "M06"], {"hotel_id": h["id"], "rooms": n, "kind": "hold",
                                                                          "option": {}}, rooms_needed=n)
            if rid is not None:
                self.m.pending_rooms[h["id"]] = self.m.pending_rooms.get(h["id"], 0) + n
            short -= n
        # one room per passenger, at a hotel where we hold a free room (N04: a person approves; the cost stays pending A03)
        for e in sorted(need, key=lambda e: e["since"]):
            pid = e["passenger_id"]
            wheel = pax.get(pid, {}).get("segment") == "wheelchair"
            if pid in st["assigned"]:
                a = st["assigned"][pid]
                h = rooms[a["hotel_id"]]
                e.update(hotel_name=h["name"], hotel_status="approved", hotel_decision=a["recommendation"],
                         approved_by=a.get("approved_by"), waited_min=a.get("waited_min"), shuttle=h["shuttle"],
                         accessible_room=wheel)
                self.costs["committed_usd"] += h["rate_usd"] * self.s["money"]["nights"]
                continue
            if pid in self.m.pending_assign:
                hid, rid = self.m.pending_assign[pid]
                h = rooms[hid]
                e.update(hotel_name=h["name"], hotel_status="waiting for approval", hotel_decision=rid, shuttle=h["shuttle"],
                         accessible_room=wheel)
                self.costs["pending_usd"] += h["rate_usd"] * self.s["money"]["nights"]
                continue
            pick = next((h for h in ranked if max(held(h["id"]), 0) - assigned_to.get(h["id"], 0) > 0), None)
            if pick is None:
                e.update(hotel_status="waiting for a room hold")
                continue
            cost = pick["rate_usd"] * self.s["money"]["nights"]
            if self.costs["committed_usd"] + self.costs["pending_usd"] + cost > self.costs["cap_usd"]:
                e.update(hotel_status="over the cost cap: a person decides")
                self.ask(("cap", pid), "N04", {"passenger_id": pid, "cost_usd": cost, **self.costs},
                         "the room would take care costs over the cap")
                continue
            if wheel:                                      # M09: an accessible room
                e["accessible_room"] = True
            rid = self.propose(("assign", pid), "issue_compensation", f"Room at {pick['name']} tonight for {pid}",
                               "stranded overnight away from home; who pays waits for the controllability decision (A03)",
                               [{"id": f"room-{pid}", "summary": f"1 room at {pick['name']}" + (" (accessible)" if wheel else ""),
                                 "cost_usd": cost}], ["N04", "M02"] + (["M09"] if wheel else []),
                               {"hotel_id": pick["id"], "rooms": 1, "kind": "assign", "accessible": wheel,
                                "option": {"pax": {"wheelchair": 1} if wheel else {}}}, rooms_needed=1, owed=cost, pids=[pid])
            self.m.pending_assign[pid] = (pick["id"], rid)
            assigned_to[pick["id"]] = assigned_to.get(pick["id"], 0) + 1
            e.update(hotel_name=pick["name"], hotel_status="waiting for approval", hotel_decision=rid, shuttle=pick["shuttle"])
            self.costs["pending_usd"] += cost
        return [{"hotel_id": h["id"], "name": h["name"], "held": max(held(h["id"]), 0) + self.m.pending_rooms.get(h["id"], 0),
                 "assigned": assigned_to.get(h["id"], 0), "rooms_available_at_start": h["rooms_available"]}
                for h in rooms.values() if held(h["id"]) > 0 or self.m.pending_rooms.get(h["id"])]

    def transport(self, entries, rooms):
        T = {t["name"]: t for t in self.static["transport"]}
        by_hotel = {}
        for e in entries.values():
            if e.get("hotel_name"):
                by_hotel.setdefault(e["hotel_name"], []).append(e)
        for name, group in by_hotel.items():
            h = next(x for x in rooms.values() if x["name"] == name)
            if h["shuttle"] == self.s["transport"]["shuttle_fits_if"] and len(group) <= (h.get("bus_capacity") or 0):
                for e in group:
                    e.update(transport=f"{name} shuttle", transport_status="hotel shuttle (no booking needed)")
                continue
            vans = sorted((t for t in T.values() if t.get("hourly_rate_usd")), key=lambda t: t["capacity"])
            van = next((t for t in vans if t["capacity"] >= len(group)), vans[-1])
            n = math.ceil(len(group) / van["capacity"])
            cost = n * van["hourly_rate_usd"] * self.s["transport"]["van_hours"]
            rid = self.propose(("van", name, len(group)), "arrange_ground_transport", f"{n} x {van['name']} to {name}",
                               f"{len(group)} passengers; the hotel shuttle is '{h['shuttle']}'",
                               [{"id": f"van-{h['id']}", "summary": f"{n} x {van['name']}", "cost_usd": cost}], ["M05"],
                               {"kind": "transport", "option": {}}, owed=cost, pids=[e["passenger_id"] for e in group])
            ok = rid in self.st["approved"]
            for e in group:
                e.update(transport=van["name"], transport_decision=rid,
                         transport_status="approved" if ok else "waiting for approval")
            self.costs["committed_usd" if ok else "pending_usd"] += cost
        uber = T.get(self.s["transport"]["uber_for_locals"])
        for e in entries.values():                       # M04: locals go home
            if "overnight" in e["needs"] and e["local"] and uber:
                pid = e["passenger_id"]
                rid = self.propose(("home", pid), "arrange_ground_transport", f"Ride home for {pid} ({uber['name']})",
                                   "lives locally: home tonight, back for tomorrow's flight",
                                   [{"id": f"home-{pid}", "summary": uber["name"], "cost_usd": uber["per_trip_rate_usd"]}], ["M04"],
                                   {"kind": "transport", "option": {}}, owed=uber["per_trip_rate_usd"], pids=[pid])
                ok = rid in self.st["approved"]
                e.update(transport=f"{uber['name']} home", transport_decision=rid,
                         transport_status="approved" if ok else "waiting for approval")
                self.costs["committed_usd" if ok else "pending_usd"] += uber["per_trip_rate_usd"]

    # ------------------------------------------------------------ c. compensation
    def compensation(self, e):
        rules = self.ctx.rules
        c = rules["compensation"]
        fid = e["flight_id"]
        decided = self.st["a03"].get(fid)
        if decided is None:
            cause = "unknown"
        else:
            cause = c["controllable_causes"][0] if "controllable" in str(decided).lower() and "un" not in str(decided).lower() \
                else c["uncontrollable_causes"][0]
        facts = {"cause": cause, "delay_min": 24 * 60 if "overnight" in e["needs"] else 180, "cancelled": "cancelled" in e["needs"],
                 "rebooking_accepted": False, "overnight": "overnight" in e["needs"], "lives_locally": e["local"]}
        owed = compensation_for(facts, rules)
        pid = e["passenger_id"]
        rid = self.propose(("refund", pid), "offer_refund", f"Offer {pid} a refund if they decline the rebooking",
                           "the legal floor: always offered for a cancellation or significant delay",
                           [{"id": f"refund-{pid}", "summary": "cash refund of the ticket if rebooking is declined", "cost_usd": 0}],
                           ["N02", "K07"], {"kind": "refund", "option": {}}, pids=[pid])
        legal = "legal floor offered" if rid in self.st["approved"] else "legal floor: refund offer waiting for approval"
        if decided is None:
            would = compensation_for({**facts, "cause": c["controllable_causes"][0]}, rules)
            items = [i["item"] for i in would["items"] if i["kind"] == "southwest"]
            what = " + ".join({"meal voucher": "meal", "travel voucher": f"${c['voucher_usd']} voucher",
                               "hotel room and ground transport": "hotel cost"}.get(i, i) for i in items) or "nothing more"
            e["compensation"] = f"{legal}; {what} pending the supervisor's controllability decision"
            e["compensation_pending_usd"] = would["total_usd"]
            self.ask(("A03", fid), "N01", {"flight_id": fid, "would_be_owed_if_controllable": [i["item"] for i in would["items"]]},
                     f"What {fid}'s passengers are owed depends on whether the cause was within Southwest's control (A03).")
        elif owed["controllable"]:
            sw = [i for i in owed["items"] if i["kind"] == "southwest"]
            rid2 = self.propose(("commit", pid), "issue_compensation", f"Southwest commitments for {pid}",
                                "a person confirmed the cause was within Southwest's control",
                                [{"id": f"comp-{pid}", "summary": ", ".join(i["item"] for i in sw), "cost_usd": sum(i["usd"] for i in sw)}],
                                ["N03", "N04"], {"kind": "commitment", "option": {}}, owed=sum(i["usd"] for i in sw), pids=[pid])
            e["compensation"] = f"{legal}; commitments " + ("approved" if rid2 in self.st["approved"] else "waiting for approval")
        else:
            gw = [i for i in owed["items"] if i["kind"] == "goodwill"]
            if gw:
                rid3 = self.propose(("goodwill", pid), "issue_compensation", f"Goodwill credit for {pid}",
                                    "not owed (outside Southwest's control); a supervisor decides",
                                    [{"id": f"gw-{pid}", "summary": gw[0]["item"], "cost_usd": gw[0]["usd"]}], ["N05"],
                                    {"kind": "goodwill", "option": {}}, owed=gw[0]["usd"], pids=[pid])
                e["compensation"] = f"{legal}; goodwill " + ("approved" if rid3 in self.st["approved"] else "waiting for approval")
            else:
                e["compensation"] = f"{legal}; nothing more is owed"

    # ------------------------------------------------------------ d. vulnerable passengers
    def vulnerable(self, entries, pax):
        out, wheel = [], []
        for e in entries.values():
            pid, seg = e["passenger_id"], pax.get(e["passenger_id"], {}).get("segment")
            if seg == "child_alone":
                overnight = "overnight" in e["needs"]
                plan = "staff supervision + guardian contacted" + ("; never a hotel alone: stays with staff until the guardian "
                                                                   "collects them" if overnight else "")
                opt = {"pax": {"child_alone": 1}, "child_alone_plan": plan, "staff_supervised": True,
                       "destination_kind": "staff_supervision"}
                rid = self.propose(("child", pid, overnight), "supervise_child_alone", f"Supervise child flying alone ({pid})",
                                   plan, [{"id": f"child-{pid}", "summary": plan, "cost_usd": 0}], ["J01"] + (["J03"] if overnight else []),
                                   {"kind": "child", "option": opt}, pids=[pid])
                e.update(child_plan=plan, child_decision=rid, hotel_status=e.get("hotel_status") or "no hotel (child alone)")
                out.append({"passenger_id": pid, "kind": "child_alone", "plan": plan, "decision": rid,
                            "rebooking_rule": "nonstop or same plane only, never international"})
            elif seg == "wheelchair":
                from backend.rules.safety import limits_for
                req = {"passenger_id": pid, "flight_id": e["flight_id"],
                       "max_unattended_min": limits_for(self.ctx.rules)["wheelchair"]["unattended_max_min"],
                       "needs": "help at every step; accessible room if overnight", "for": "station_staff (J04)"}
                wheel.append(req)
                out.append({"passenger_id": pid, "kind": "wheelchair", "plan": req["needs"],
                            "accessible_room": e.get("accessible_room", False)})
        return out, wheel

    # ------------------------------------------------------------ e. rebooking advice (advisory)
    def rebooking(self, entries, pax, refs, plan):
        flights, st = self.static["flights"], self.st
        seats = dict(((plan or {}).get("facts") or {}).get("seats_by_destination") or {})
        left = {d: v.get("open_seats_later_today", 0) for d, v in seats.items()}
        lead = timedelta(minutes=self.s["rebooking"]["min_lead_min"])
        want = [e for e in entries.values() if e["trip_status"] in ("overnight", "cancelled") or
                any(f in st["next_day"] for f in e["itinerary"])]
        vul = lambda e: 0 if pax.get(e["passenger_id"], {}).get("segment") in VULNERABLE else 1
        groups = {}
        for e in want:
            groups.setdefault(refs.get(e["passenger_id"]) or e["passenger_id"], []).append(e)
        out = []
        for ref, group in sorted(groups.items(), key=lambda kv: (min(vul(e) for e in kv[1]), kv[1][0]["since"])):
            e0 = group[0]
            dest = pax.get(e0["passenger_id"], {}).get("final_destination")
            child = any(pax.get(e["passenger_id"], {}).get("segment") == "child_alone" for e in group)
            options = [f for f in flights.values() if f["direction"] == "departure" and f["destination"] == dest
                       and f["origin"] == "MCI" and f["id"] not in st["cancelled"] and f["id"] not in st["departed"]
                       and f["sched_dep"] >= self.now + lead]
            options.sort(key=lambda f: f["sched_dep"])
            if not options or left.get(dest, 0) < len(group):
                continue
            f = options[0]                               # nonstop MCI -> destination: fine for a child alone too (J02)
            left[dest] -= len(group)
            pids = [e["passenger_id"] for e in group]
            rid = self.propose(("rebook", tuple(pids), f["id"]), "rebook_passenger",
                               f"Advice: move {len(group)} passenger(s) to {f['id']} today ({hhmm(f['sched_dep'])})",
                               "a seat later today instead of tomorrow (advisory: the booking is not changed)",
                               [{"id": f"rebook-{f['id']}", "summary": f"{f['id']} at {hhmm(f['sched_dep'])} to {dest}", "cost_usd": 0,
                                 "flight_id": f["id"]}], ["K01", "K03", "K05"] + (["J02"] if child else []),
                               {"kind": "advice", "option": {"pax": {"child_alone": int(child)}, "plane_changes": 0}}, pids=pids)
            for e in group:
                e["rebooking_advice"] = f"{f['id']} today at {hhmm(f['sched_dep'])} (advisory)"
            out.append({"passengers": pids, "flight_id": f["id"], "departs": hhmm(f["sched_dep"]), "family": len(group) > 1,
                        "vulnerable": min(vul(e) for e in group) == 0, "decision": rid})
        return out

    def release(self, entries, rooms):
        """M10: after release_after, give back held rooms nobody was assigned to."""
        if self.now < clock(self.s["hotels"]["release_after"]):
            return
        st = self.st
        for hid, h in rooms.items():
            held = st["held"].get(hid, 0) - st["released"].get(hid, 0)
            used = sum(1 for a in st["assigned"].values() if a["hotel_id"] == hid) + \
                sum(1 for pid, (x, _) in self.m.pending_assign.items() if x == hid and pid not in st["assigned"])
            spare = held - used
            if spare > 0:
                self.propose(("release", hid, held), "release_hotel_rooms", f"Release {spare} unused rooms at {h['name']}",
                             f"after {self.s['hotels']['release_after']} nobody needs them",
                             [{"id": f"rel-{hid}", "summary": f"release {spare} rooms", "cost_usd": 0}], ["M10"],
                             {"hotel_id": hid, "rooms": spare, "kind": "release", "option": {}})


def plan_care(ctx, memory: Memory | None = None, settings: dict | None = None) -> dict:
    s = settings or load_settings()
    memory = memory or Memory()
    events = ctx.read_events()
    sit = ctx.read_shared("situation_assessments")
    rec = ctx.read_shared("recovery_plans")
    c = Care(ctx, memory, s)
    res = c.run(events, sit, rec)
    key = (tuple(sorted((e["passenger_id"], e.get("hotel_status"), e.get("transport_status"), e.get("compensation"),
                         e.get("rebooking_advice")) for e in res["passengers"])), tuple((h["hotel_id"], h["held"], h["assigned"]) for h in res["hotels"]))
    wrote = False
    if key != memory.last_key:
        memory.version += 1
        row = {"version": memory.version, "planned_at": ctx.now, "situation_version": (sit or {}).get("version"),
               "recovery_plan_version": (rec or {}).get("version"), "passengers": res["passengers"], "hotels": res["hotels"],
               "costs": c.costs, "vulnerable": res["vulnerable"], "wheelchair_requests": res["wheelchair"],
               "rebooking_advice": res["advice"], "sources": c.sources, "data_as_of": ctx.now,
               "decision_ids": ["M01", "N01", "N02"] + (["J01"] if any(v["kind"] == "child_alone" for v in res["vulnerable"]) else [])}
        ctx.write("care_plans", row)
        ctx.record_event("care_plan_drafted", {"version": memory.version, "passengers": len(res["passengers"]),
                                               "committed_usd": c.costs["committed_usd"], "pending_usd": c.costs["pending_usd"]})
        memory.plans.append({**row, "planned_at": ctx.now.isoformat(), "data_as_of": ctx.now.isoformat()})
        memory.last_key, wrote = key, True
    ctx.heartbeat({"latest_version": memory.version})
    return {"proposals": c.proposals, "plan": res, "costs": c.costs, "wrote": wrote}
