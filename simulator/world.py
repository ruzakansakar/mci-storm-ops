"""The world: Southwest's real systems, the TRUTH.

Applies the day's events to flights, aircraft, stations, crews, bags and bookings, and follows
"today's" simple rules (no co-pilot): flights wait out the stop, a flight that would leave too late
is cancelled, a plane that runs low on fuel while holding diverts, and passengers on a cancelled
flight are put on the next flight to the same place that has room.

The world makes no smart decisions. Everything it does is either physics (time passes, fuel burns)
or one of the fixed rules in the "baseline" section of config/simulator.yaml.

Everything is kept in memory. Every change goes through _set(), which records it in the audit log
and remembers it so the writer can save it to the database in a batch.
"""
from collections import defaultdict

from simulator.clock import at, fmt, hhmm, minute_of

SEG_ORDER = ["child_alone", "wheelchair", "international", "connecting", "standard"]
TIME_FIELDS = {"est_dep", "est_arr", "edct", "tarmac_since", "sched_dep", "sched_arr"}

WAITING_DEP = ("scheduled", "ground_stop", "delayed", "held")      # a departure that has not left yet
NOT_DEPARTED_ARR = ("scheduled", "ground_stop", "delayed")          # an inbound still on the ground at its origin
DEPARTED_STATES = ("departed", "airborne", "diverted", "arrived")
SEVERITY = {"confirmed": 0, "delayed": 1, "rebooked": 2, "diverted": 3, "overnight": 4, "cancelled": 5}
CREW_ROLES = (("pilot", "pilot_duty_left_min", "pilot_pair"), ("fa", "fa_duty_left_min", "fa_team"))
ACTOR = "simulator"


class World:
    def __init__(self, data: dict, scenario, cfg: dict, start: int):
        self.cfg, self.sc, self.t = cfg, scenario, start
        self.start = start
        self.ph = cfg["physics"]
        self.base = cfg["baseline"]
        self.lim = cfg["limits"]                  # legal and safety numbers, read from config/rules/
        self.incident = scenario.incident

        # ---- load the truth from the seed rows (copies; the input is never changed)
        self.flights = {}
        for r in data["flights"]:
            f = dict(r)
            for k in ("sched_dep", "sched_arr"):
                f[k] = minute_of(f[k])
            f.update(as_of=start, status="scheduled", est_dep=f["sched_dep"], est_arr=f["sched_arr"], edct=None,
                     tarmac_since=None, cancel_reason=None, controllable=None, diverted_to=None,
                     _block=f["sched_arr"] - f["sched_dep"], _orig_pax=f["pax_total"], actual_dep=None,
                     actual_arr=None, _logged_delay=None, _holding=False, _resumed=False, _resume_ready=None,
                     _land_key=None, _tarmac_max=0, _inbound=None, _outbound=None)
            self.flights[f["id"]] = f
        self.arrivals = sorted((f for f in self.flights.values() if f["direction"] == "arrival"), key=lambda f: f["id"])
        self.departures = sorted((f for f in self.flights.values() if f["direction"] == "departure"), key=lambda f: f["id"])
        arr_by_ac = {f["aircraft_id"]: f for f in self.arrivals}
        for d in self.departures:
            inb = arr_by_ac.get(d["aircraft_id"])
            if inb:
                d["_inbound"], inb["_outbound"] = inb["id"], d["id"]

        self.aircraft = {}
        for r in data["aircraft"]:
            a = dict(r)
            a.update(as_of=start, airborne=False, fuel_minutes_extra=None, _fuel0=None, needs_inspection=False, _inspect_done=None,
                     _inspect_min=None)
            self.aircraft[a["id"]] = a
        self.crews = {r["id"]: {**r, "as_of": start} for r in data["crews"]}
        self.stations = {r["code"]: {**r, "as_of": start} for r in data["stations"]}
        self.mci = self.stations["MCI"]
        self.passengers = {r["id"]: {**r, "overnight": False} for r in data["passengers"]}
        self.bookings = defaultdict(list)
        for r in sorted(data["bookings"], key=lambda r: (r["passenger_id"], r["seq"])):
            self.bookings[r["passenger_id"]].append({**r, "status": "confirmed", "as_of": start, "_original": r["flight_id"]})
        self.bags = {r["tag"]: {**r, "status": "checked", "as_of": start, "routing": list(r["routing"])} for r in data["bags"]}
        self.bags_by_pax = defaultdict(list)
        for b in self.bags.values():
            self.bags_by_pax[b["passenger_id"]].append(b["tag"])
        self.groups = {(r["flight_id"], r["segment"]): {**r, "rebooked": 0, "as_of": start} for r in data["passenger_groups"]}
        # ---- ground staff on shift and gates (Phase 5b physics: the same in every mode)
        self.ground = self.ph["ground"]
        self.roster = [(r["role"], minute_of(r["shift_start"]), minute_of(r["shift_end"])) for r in data.get("staff", [])
                       if r.get("station") == "MCI"]
        self.worked = []                   # (minute, "departure" | "arrival") turns worked at MCI
        # ---- Phase 5b: in orchestrated mode, approved co-pilot decisions replace today's PROCESS decisions (never physics or
        # law). Where none was approved by the moment today's process would act, today's process acts (a logged fallback).
        self.orchestrated = False
        self.cp_delay = {}                 # departure -> approved new departure (minute): replaces the cancel cutoff
        self.cp_cancelled = set()          # departures the co-pilot's approved plan cancelled
        self.fallbacks = []                # {t, process, flight_id, what}: today's process acted (no approved decision in time)
        self.applied = []                  # {t, action, facts, applied, reason}: what the operational systems were asked to do
        self.crew_violations = []          # departures whose crew did not have the legal duty time for the flight (all modes)
        self.publish_seats = False         # fix-seats: the reservations system publishes seat inventory (co-pilot modes read it)
        self.pending_cp = {}               # departure -> deadline (minute) of a co-pilot proposal waiting for approval
        self.deferred_thin = {}            # departure -> (deadline, reason): thinning that waits for that proposal
        self.thin_waits = []               # {flight_id, until, outcome}: each wait and how it ended
        self.ground_waits = []             # (minute, flight, why): a plane that had to wait for staff or a gate
        self.orig_groups = {k: g["count"] for k, g in self.groups.items()}

        # ---- crews: how much duty a flight needs, which crews were legal at the start, and the reserve list
        crew_cfg = self.base["crew"]
        for d in self.departures:
            d["_crew_need"] = d["_block"] + crew_cfg["onward_leg_min"] + self.lim["crew_duty_buffer_min"]
        self.crew_checked = set()          # (crew id, role) that we check for running out of duty
        self.illegal_crews = {}            # crews that already break a rule in the class data (they still fly today)
        rest_rule = crew_cfg["source_data_rules"]
        issues = {i["item"]: i for i in data.get("data_issues", []) if i["category"] == "crew_rest"}
        for d in self.departures:
            d["_role_crew"] = {}           # role -> id of the reserve crew that took over that role
            d["reserve_crew_ids"] = None
            crew = self.crews.get(d["crew_id"]) if d["crew_id"] else None
            for role, field, _ in CREW_ROLES:
                if crew and crew[field] is not None and crew[field] >= d["_crew_need"]:
                    self.crew_checked.add((crew["id"], role))      # a crew already short in the class data is left as it is
                elif crew and crew[field] is not None:
                    self.illegal_crews.setdefault(crew["id"], {"flight_id": d["id"], "breaks": []})["breaks"].append(
                        {"rule": rest_rule["short_duty"], "role": role, "duty_left_min": crew[field], "needed_min": d["_crew_need"]})
            if crew and crew["min_rest_hours"] is not None and crew["min_rest_hours"] < self.lim["pilot_min_rest_hours"]:
                self.illegal_crews.setdefault(crew["id"], {"flight_id": d["id"], "breaks": []})["breaks"].append(
                    {"rule": rest_rule["short_rest"], "min_rest_hours": float(crew["min_rest_hours"]),
                     "required_hours": self.lim["pilot_min_rest_hours"]})
        for cid, info in self.illegal_crews.items():
            issue = issues.get(cid)
            info["data_issue"] = {"category": issue["category"], "item": issue["item"]} if issue else None
        self.reserves = sorted((c for c in self.crews.values() if c.get("is_reserve")),
                               key=lambda c: (minute_of(c["available_from"]), c["id"]))
        for c in self.reserves:
            c["_avail"] = minute_of(c["available_from"])
        self.thinned = set()               # flights cancelled to thin the schedule
        self.next_day = defaultdict(list)  # (origin, destination, direction) -> next-day flights we created
        self.new_flights = []              # created flights, waiting for the writer
        self.new_flights_all = []          # every flight created today
        self.overnight_pax = 0             # passengers (all, not just tracked) sent to a next-day flight
        self.pax_by_flight = defaultdict(set)
        for pid, bks in self.bookings.items():
            for b in bks:
                self.pax_by_flight[b["flight_id"]].add(pid)

        # ---- state of the day
        self.restr = None                  # {'program','start','end'} while a stop / ground delay is in effect
        self.ramp_until = None             # minute the ramp is expected to reopen, while it is closed
        self.surge = None                  # terminal crowd growth
        self.flagged_start = False
        self.extended = set()              # crews whose pilots already had a duty extension
        self.next_dep_slot = self.next_land_slot = start
        self.max_tarmac = 0
        self.events, self.audit_rows = [], []
        self._ev_cursor = self._au_cursor = 0
        self.dirty = defaultdict(set)
        self.dirty_flights, self.dirty_pax, self.rebooked_now = set(), set(), set()
        self._initial_state()
        self.truth = {pid: self.pax_truth(pid) for pid in sorted(self.passengers)}

    # ================================================================ recording

    def _emit(self, type_, station="MCI", payload=None, incident=False, actor_name="world"):
        self.events.append({"t": self.t, "type": type_, "actor": ACTOR, "actor_name": actor_name,
                            "station": station, "payload": payload or {}, "incident": incident})

    def _jsonable(self, k, v):
        return at(v).isoformat() if k in TIME_FIELDS and isinstance(v, int) else v

    def _set(self, table, obj, key, entity, why, **kv):
        """Change fields on a truth record; log before/after and remember to save it."""
        kv.pop("as_of", None)               # "as of" just follows any real change; it is not a change itself
        before, after = {}, {}
        for k, v in kv.items():
            if obj.get(k) != v:
                before[k], after[k] = self._jsonable(k, obj.get(k)), self._jsonable(k, v)
                obj[k] = v
        if not after:
            return False
        obj["as_of"] = self.t
        self.audit_rows.append({"t": self.t, "actor": ACTOR, "actor_name": "world", "what": why,
                                "entity_type": entity, "entity_id": str(key), "before": before, "after": after})
        self.dirty[table].add(key)
        if table == "flights" and {"status", "est_dep", "est_arr"} & set(after):
            self.dirty_flights.add(key)
        return True

    def drain(self):
        """New events and audit rows since the last call (the writer saves these)."""
        ev, au = self.events[self._ev_cursor:], self.audit_rows[self._au_cursor:]
        self._ev_cursor, self._au_cursor = len(self.events), len(self.audit_rows)
        return ev, au

    # ================================================================ start of day

    def _initial_state(self):
        """Flights that are already in the air (or landed) at the start of the day."""
        for f in self.arrivals:
            params = self.sc.flight_params.get(f["id"], {})
            if f["sched_arr"] <= self.start:
                f.update(status="arrived", actual_dep=f["sched_dep"], actual_arr=f["sched_arr"])
                self.aircraft[f["aircraft_id"]].update(current_station="MCI")
                self.dirty["aircraft"].add(f["aircraft_id"])
                self.dirty["flights"].add(f["id"])
            elif f["sched_dep"] < self.start:
                f.update(status="airborne", actual_dep=f["sched_dep"])
                self.aircraft[f["aircraft_id"]].update(airborne=True, fuel_minutes_extra=params.get("fuel_minutes_extra"),
                                                       _fuel0=params.get("fuel_minutes_extra"))
                self.dirty["aircraft"].add(f["aircraft_id"])
                self.dirty["flights"].add(f["id"])
        for f in self.flights.values():
            self.dirty["flights"].add(f["id"])      # est_dep / est_arr are filled in on the first save

    def check_story(self):
        """For the fixed class story: make sure the seed still has the numbers from the slides."""
        s = self.cfg["scenarios"].get(self.sc.name)
        if not s:
            return
        e, fid = s["expect"], s["flight"]
        got = {"pax_total": self.flights[fid]["pax_total"],
               "connecting": self.groups[(fid, "connecting")]["count"],
               "wheelchair": self.groups[(fid, "wheelchair")]["count"],
               "crew_duty_left_min": self.crews[self.flights[fid]["crew_id"]]["pilot_duty_left_min"]}
        if got != e:
            raise SystemExit(f"The seed data no longer matches the class story for {fid}: expected {e}, found {got}")

    # ================================================================ one minute

    def step(self, t: int):
        self.t = t
        if not self.flagged_start:
            self._flag_illegal_crews()
        for ev in self.sc.events_at(t):
            self._apply_event(ev)
        if self.deferred_thin:                     # fix-seats: thinning that waited for a pending recommendation (every minute)
            self._deferred_thinning()
        if t == self.start or t % self.ph["reproject_every_min"] == 0:
            self.reproject("regular update")
        self._tick_clocks()
        self._inbound_takeoffs()
        self._holding_start()
        self._landings()
        self._check_fuel()
        self._resume_from_alternate()
        self._departures()
        self._destination_arrivals()

    # ================================================================ scenario events

    def _apply_event(self, ev):
        t, type_, p = self.t, ev["type"], ev["payload"]
        self._emit(type_, ev["station"], p, incident=ev["incident"], actor_name="scenario")
        if type_.endswith("_started"):
            self.restr = {"program": p["program"], "start": t, "end": p["expected_end"]}
            self._start_tarmac_for_imminent()
            self._thin_schedule()
        elif type_.endswith("_extended"):
            self.restr["end"] = p["expected_end"]
            self._thin_schedule()
        elif type_.endswith("_ended"):
            self.restr = None
        elif type_ == "ramp_closed":
            self.ramp_until = p["expected_end"]
            self._set("stations", self.mci, "MCI", "station", "ramp closed", ramp_open=False, as_of=t)
            self._start_tarmac_for_imminent()
        elif type_ == "ramp_reopened":
            self.ramp_until = None
            self._set("stations", self.mci, "MCI", "station", "ramp reopened", ramp_open=True, as_of=t)
            for f in self.arrivals:
                if f["status"] == "arrived" and f["tarmac_since"] is not None:
                    self._end_tarmac(f, "ramp reopened")
        elif type_ == "aircraft_inspection_needed":
            ac = self.aircraft[p["aircraft_id"]]
            dep = self.flights.get(p.get("flight_id"))
            if dep and dep["status"] in WAITING_DEP:
                self._set("aircraft", ac, ac["id"], "aircraft", "inspection needed", needs_inspection=True)
                ac["_inspect_min"] = p["duration_min"]
                on_ground = ac["current_station"] == "MCI" and not ac["airborne"]
                ac["_inspect_done"] = t + p["duration_min"] if on_ground else None
        elif type_ == "crew_duty_low":
            crew = self.crews.get(p["crew_id"])
            if crew:
                self._set("crews", crew, crew["id"], "crew", "crew duty clock low",
                          pilot_duty_left_min=p["minutes_left"], as_of=t)
                self.crew_checked.add((crew["id"], "pilot"))
        elif type_ == "terminal_crowd_growth":
            self.surge = {"start": t, "extra": p["extra_people"], "ramp": p["ramp_up_min"], "decay": p["decay_min"]}
        self.reproject(f"event {type_}")

    # ================================================================ rules of the airfield

    def _stop_active(self):
        r = self.restr
        return bool(r and r["program"] == "ground_stop" and r["start"] <= self.t < r["end"])

    def _landing_open(self, minute):
        r = self.restr
        return not (r and r["program"] == "ground_stop" and minute < r["end"])

    def _departure_open(self, minute):
        if not self._landing_open(minute):
            return False
        return not (self.ramp_until is not None and minute < self.ramp_until)

    def _land_spacing(self, prev):
        r = self.restr
        if r and r["program"] == "ground_delay" and prev < r["end"]:
            return self.ph["ground_delay_spacing_min"]
        return self.ph["slot_spacing_min"]

    def _departures_blocked_now(self):
        return not self._departure_open(self.t)

    def _start_tarmac_for_imminent(self):
        """Flights about to leave have already pushed back. If the airfield shuts, they are stuck on the tarmac."""
        if not self._departures_blocked_now():
            return
        lead = self.ph["pushback_lead_min"]
        for f in self.departures:
            if (f["status"] in ("scheduled", "delayed") and self.t <= f["est_dep"] < self.t + lead
                    and f["tarmac_since"] is None and self._aircraft_present(f)):
                self._set("flights", f, f["id"], "flight", "pushed back, now waiting on the tarmac",
                          tarmac_since=self.t, as_of=self.t)
                self._emit("tarmac_started", "MCI", {"flight_id": f["id"]})
                self._refresh(f)

    def _aircraft_present(self, dep):
        inb = dep["_inbound"]
        return inb is None or self.flights[inb]["status"] == "arrived"

    def _end_tarmac(self, f, how):
        if f["tarmac_since"] is None:
            return
        minutes = self.t - f["tarmac_since"]
        f["_tarmac_max"] = max(f["_tarmac_max"], minutes)
        self.max_tarmac = max(self.max_tarmac, minutes)
        self._set("flights", f, f["id"], "flight", f"tarmac ended: {how}", tarmac_since=None, as_of=self.t)
        self._emit("tarmac_ended", "MCI", {"flight_id": f["id"], "minutes": minutes, "how": how})

    # ================================================================ working out the times

    def _ready_at(self, d):
        """Earliest minute a departure's aircraft (and inspection) allow it to leave. None = aircraft will not come."""
        r = 0
        if d["_inbound"]:
            i = self.flights[d["_inbound"]]
            if i["status"] == "cancelled":
                return None
            r = (i["actual_arr"] if i["status"] == "arrived" else i["est_arr"]) + self.ph["min_turn_min"]
        ac = self.aircraft[d["aircraft_id"]]
        if ac["needs_inspection"]:
            done = ac["_inspect_done"]
            r = max(r, done) if done is not None else r + ac["_inspect_min"]
        return r

    def reproject(self, why):
        """Re-work out every waiting flight's expected times, using today's fixed rules."""
        for _ in range(6):
            to_cancel = self._project_arrivals() + self._project_departures()
            if not to_cancel:
                break
            for f, reason in to_cancel:
                if f["status"] != "cancelled":
                    self._cancel_flight(f, reason)

    def _project_arrivals(self):
        t, limit = self.t, self.base["cancel_if_delay_over_min"]
        keyed = []
        for f in self.arrivals:
            s = f["status"]
            if s in NOT_DEPARTED_ARR:
                key = max(f["sched_arr"], t + f["_block"])
            elif s == "airborne":
                key = max(self._over_mci(f), t)
            elif s == "diverted":
                key = max(f["_land_key"] if f["_resumed"] else f["_resume_ready"], t)
            else:
                continue
            keyed.append((key, f["id"], f))
        keyed.sort(key=lambda k: k[:2])
        prev, cancels = None, []
        for key, _, f in keyed:
            slot = key if prev is None else max(key, prev + self._land_spacing(prev))
            while not self._landing_open(slot):
                slot += 1
            if f["status"] in NOT_DEPARTED_ARR and slot - f["sched_arr"] > limit:
                cancels.append((f, f"{self.sc.cause}: would arrive more than {limit} min late"))
                continue
            prev = slot
            if f["status"] in NOT_DEPARTED_ARR:
                est_dep = slot - f["_block"]
                self._set("flights", f, f["id"], "flight", "times re-worked out", est_arr=slot, est_dep=est_dep,
                          edct=est_dep if est_dep > f["sched_dep"] else None, as_of=t)
                self._refresh(f)
            else:
                self._set("flights", f, f["id"], "flight", "landing time re-worked out", est_arr=slot, as_of=t)
        return cancels

    def _project_departures(self):
        t, limit = self.t, self.base["cancel_if_delay_over_min"]
        keyed, cancels = [], []
        for d in self.departures:
            if d["status"] not in WAITING_DEP:
                continue
            ready = self._ready_at(d)
            if ready is None:
                cancels.append((d, "aircraft will not arrive (inbound flight cancelled)"))
                continue
            keyed.append((max(d["sched_dep"], ready, t, self.cp_delay.get(d["id"], 0)), d["id"], d))
        keyed.sort(key=lambda k: k[:2])
        prev = None
        for key, _, d in keyed:
            slot = key if prev is None else max(key, prev + self.ph["slot_spacing_min"])
            while not self._departure_open(slot):
                slot += 1
            if slot - d["sched_dep"] > limit and self.orchestrated and d["id"] in self.cp_delay:
                pass                                   # replaced: a person approved this delay (crew law still applies)
            elif slot - d["sched_dep"] > limit:
                self._fallback("cancel cutoff", d, f"would leave more than {limit} min late; no approved co-pilot delay")
                inb = self.flights[d["_inbound"]] if d["_inbound"] else None
                if inb and inb["diverted_to"]:
                    why = f"aircraft out of position after diverting to {inb['diverted_to']}: would leave more than {limit} min late"
                else:
                    why = f"{self.sc.cause}: would leave more than {limit} min late"
                cancels.append((d, why))
                continue
            prev = slot
            self._set("flights", d, d["id"], "flight", "times re-worked out", est_dep=slot,
                      est_arr=slot + d["_block"], as_of=t)
            self._refresh(d)
        return cancels

    def _refresh(self, f):
        """Set the status of a flight that is still on the ground, from its delay and where it is."""
        if f["status"] not in (WAITING_DEP if f["direction"] == "departure" else NOT_DEPARTED_ARR):
            return
        delay = f["est_dep"] - f["sched_dep"]
        if f["tarmac_since"] is not None:
            new = "held"
        elif delay >= self.ph["delay_flag_min"]:
            new = "ground_stop" if self.restr else "delayed"
        else:
            new = "scheduled"
        self._set("flights", f, f["id"], "flight", f"status {f['status']} -> {new}", status=new, as_of=self.t)
        if new == "scheduled":
            f["_logged_delay"] = None
            return
        if delay < self.ph["delay_flag_min"]:
            return
        self._mark_disrupted(f)
        logged = f["_logged_delay"]
        if logged is None or abs(delay - logged) >= self.ph["delay_update_min"]:
            f["_logged_delay"] = delay
            reason = self.sc.cause if self.restr else "knock-on delay"
            self._emit("flight_delayed" if logged is None else "flight_delay_updated", f["origin"],
                       {"flight_id": f["id"], "delay_min": delay, "new_departure": fmt(f["est_dep"]), "reason": reason,
                        "pax": f["pax_total"]})

    def _mark_disrupted(self, f):
        if f["controllable"] is None:
            self._set("flights", f, f["id"], "flight", "cause recorded", controllable=self.incident["controllable"])

    # ================================================================ flights moving

    def _inbound_takeoffs(self):
        t = self.t
        for f in self.arrivals:
            if f["status"] in NOT_DEPARTED_ARR and f["est_dep"] <= t:
                fuel = self.sc.flight_params.get(f["id"], {}).get("fuel_minutes_extra")
                self._set("flights", f, f["id"], "flight", "took off", status="airborne", actual_dep=t, as_of=t)
                ac = self.aircraft[f["aircraft_id"]]
                self._set("aircraft", ac, ac["id"], "aircraft", "airborne", airborne=True, fuel_minutes_extra=fuel, as_of=t)
                ac["_fuel0"] = fuel
                self._emit("flight_airborne", f["origin"], {"flight_id": f["id"], "to": "MCI", "fuel_minutes_extra": fuel,
                                                           "delay_min": t - f["sched_dep"]})

    def _holding_start(self):
        for f in self.arrivals:
            if f["status"] == "airborne" and not f["_holding"] and self.t >= self._over_mci(f) and f["est_arr"] > self.t:
                f["_holding"] = True
                self._emit("flight_holding", "MCI", {"flight_id": f["id"], "expected_landing": fmt(f["est_arr"]),
                                                    "fuel_minutes_extra": self.aircraft[f["aircraft_id"]]["fuel_minutes_extra"],
                                                    "pax": f["pax_total"]})

    # ---- ground physics (Phase 5b): staff and gates, the same in every mode
    def _on_shift(self, role, t):
        return sum(1 for r, a, b in self.roster if r == role and a <= t < b)

    def _staff_free(self, kind, t):
        """Can the ramp crew and gate agents needed for this piece of work be found now (people on shift minus the turns still
        being worked)? Same numbers as Station & Staff's planning."""
        g = self.ground
        for role in ("ramp", "gate_agent"):
            busy = 0
            for minute, k in self.worked:
                n, length = g[k][role]
                if minute <= t < minute + length if k == "arrival" else t - length < minute <= t:
                    busy += n
            if busy + g[kind][role][0] > self._on_shift(role, t):
                return False, role
        return True, None

    def _gates_in_use(self, t):
        tow = self.ground["tow_to_gate_before_min"]
        n = 0
        for d in self.departures:
            if d["status"] not in WAITING_DEP or d["tarmac_since"] is not None:
                continue
            inb = self.flights[d["_inbound"]] if d["_inbound"] else None
            if (inb and inb["status"] == "arrived") or (inb is None and t >= d["sched_dep"] - tow):
                n += 1
        for a in self.arrivals:                   # a plane whose next flight from MCI is gone stays at its gate
            if a["status"] == "arrived" and (not a["_outbound"] or self.flights[a["_outbound"]]["status"] == "cancelled"):
                n += 1
        return n

    def _gate_free(self, t):
        cap = self.ground["southwest_gates"] + (self.ground["remote_stands"] if self.ramp_until is None else 0)
        return self._gates_in_use(t) < cap

    def _ground_wait(self, f, why):
        if not self.ground_waits or self.ground_waits[-1][1:] != (f["id"], why):
            self.ground_waits.append((self.t, f["id"], why))

    def _landings(self):
        t = self.t
        if t < self.next_land_slot or not self._landing_open(t):
            return
        for f in sorted((f for f in self.arrivals if f["status"] == "airborne" or (f["status"] == "diverted" and f["_resumed"])),
                        key=lambda f: (f["est_arr"], f["id"])):
            if f["est_arr"] > t:
                break
            if not self._gate_free(t):                               # physics: no gate (or stand) free to take the plane
                self._ground_wait(f, "no gate free")
                break
            self.next_land_slot = t + self._land_spacing(t)
            self.worked.append((t, "arrival"))
            self._land(f)
            break                                                    # one landing per slot

    def _land(self, f):
        t = self.t
        late = t - f["sched_arr"]
        was_diverted = f["status"] == "diverted"
        self._set("flights", f, f["id"], "flight", "landed", status="arrived", actual_arr=t, est_arr=t, as_of=t)
        ac = self.aircraft[f["aircraft_id"]]
        self._set("aircraft", ac, ac["id"], "aircraft", "landed at MCI", airborne=False, current_station="MCI",
                  fuel_minutes_extra=None, as_of=t)
        if ac["needs_inspection"] and ac["_inspect_done"] is None:
            ac["_inspect_done"] = t + ac["_inspect_min"]
        if self.ramp_until is not None:
            self._set("flights", f, f["id"], "flight", "landed, ramp closed: waiting on the tarmac", tarmac_since=t)
            self._emit("tarmac_started", "MCI", {"flight_id": f["id"], "ramp_closed": True})
        self._emit("flight_arrived", "MCI", {"flight_id": f["id"], "delay_min": late, "after_diversion": was_diverted})
        self._bags_arrive(f)
        self._mark_pax(f)
        self.reproject("a flight landed")

    def _resume_from_alternate(self):
        back = self.ph["diversion"]["flight_back_min"]
        for f in self.arrivals:
            if f["status"] == "diverted" and not f["_resumed"] and self.t >= f["est_arr"] - back:
                f["_resumed"], f["_land_key"] = True, self.t + back
                self._end_tarmac(f, "left the alternate airport")
                ac = self.aircraft[f["aircraft_id"]]
                self._set("aircraft", ac, ac["id"], "aircraft", "left the alternate airport", airborne=True, as_of=self.t)
                self._emit("flight_resumed", f["diverted_to"], {"flight_id": f["id"], "to": "MCI"})

    def _departures(self):
        t = self.t
        if t < self.next_dep_slot or not self._departure_open(t):
            return
        for d in sorted((d for d in self.departures if d["status"] in WAITING_DEP), key=lambda d: (d["est_dep"], d["id"])):
            if d["est_dep"] > t:
                break
            ready = self._ready_at(d)
            if ready is None or ready > t:
                continue
            ok, short = self._staff_free("departure", t)
            if not ok:                                               # physics: no ramp crew / gate agent free to work it
                self._ground_wait(d, f"no {short.replace('_', ' ')} free")
                continue
            self.worked.append((t, "departure"))
            self._depart(d)
            self.next_dep_slot = t + self.ph["slot_spacing_min"]
            break                                                     # one takeoff per slot

    def _depart(self, d):
        t = self.t
        for role, field, _ in CREW_ROLES:                    # the law, measured in every mode: duty time must cover the flight
            crew = self._crew_for(d, role)
            if crew and crew[field] is not None and crew[field] < d["_block"] and (crew["id"], role) in self.crew_checked:
                self.crew_violations.append({"t": t, "flight_id": d["id"], "crew_id": crew["id"], "role": role,
                                             "duty_left_min": crew[field], "needed_min": d["_block"]})
        self._end_tarmac(d, "took off")
        self._set("flights", d, d["id"], "flight", "departed", status="departed", actual_dep=t, est_dep=t,
                  est_arr=t + d["_block"], as_of=t)
        ac = self.aircraft[d["aircraft_id"]]
        self._set("aircraft", ac, ac["id"], "aircraft", "departed MCI", airborne=True, current_station=None, as_of=t)
        d["_logged_delay"] = None
        self._emit("flight_departed", "MCI", {"flight_id": d["id"], "to": d["destination"], "delay_min": t - d["sched_dep"]})
        for tag in self._bags_on(d["id"]):
            self._set("bags", self.bags[tag], tag, "bag", "bag loaded, flight departed", status="in_transit", as_of=t)
        self._missed_connections(d)
        self._mark_pax(d)

    def _destination_arrivals(self):
        t = self.t
        for d in self.departures:
            if d["status"] == "departed" and d["est_arr"] <= t:
                self._set("flights", d, d["id"], "flight", "arrived at destination", status="arrived", actual_arr=t, as_of=t)
                ac = self.aircraft[d["aircraft_id"]]
                self._set("aircraft", ac, ac["id"], "aircraft", "landed at destination", airborne=False,
                          current_station=d["destination"], as_of=t)
                self._emit("flight_arrived", d["destination"], {"flight_id": d["id"], "delay_min": t - d["sched_arr"]})
                self._bags_arrive(d)
                self._mark_pax(d)

    # ================================================================ clocks that tick every few minutes

    def _tick_clocks(self):
        t, n = self.t, self.ph["clock_update_every_min"]
        if t % n:
            return
        for f in self.arrivals:
            if f["status"] == "airborne" and t > self._over_mci(f) and f["est_arr"] > t - n:   # holding over MCI
                self._burn_duty(f, min(n, t - self._over_mci(f)))
            elif f["status"] == "diverted" and not f["_resumed"]:
                self._burn_duty(f, n)
        for d in self.departures:
            if d["status"] in WAITING_DEP and t > d["sched_dep"]:             # waiting uses up the crew's duty time
                self._burn_duty(d, min(n, t - d["sched_dep"]))
        self._check_crews()
        limit = self.lim["tarmac_domestic_min"]
        for f in self.flights.values():
            if f["tarmac_since"] is not None and t - f["tarmac_since"] >= limit:
                self._end_tarmac(f, f"passengers let off after {limit} min")
                self._refresh(f)
        for ac in self.aircraft.values():
            if ac["needs_inspection"] and ac["_inspect_done"] is not None and ac["_inspect_done"] <= t:
                self._set("aircraft", ac, ac["id"], "aircraft", "inspection finished", needs_inspection=False, as_of=t)
                ac["_inspect_done"] = None
                self._emit("inspection_completed", "MCI", {"aircraft_id": ac["id"]})
                self.reproject("inspection finished")
        self._update_crowd()

    def _over_mci(self, f):
        """The minute an airborne inbound flight reaches MCI airspace (it starts holding from here if it cannot land)."""
        return f["actual_dep"] + f["_block"]

    def _holding_fuel(self, f):
        """Spare holding fuel right now: what it took off with, minus the minutes spent holding over MCI."""
        ac = self.aircraft[f["aircraft_id"]]
        if ac["_fuel0"] is None:
            return None
        return max(0, ac["_fuel0"] - max(0, self.t - self._over_mci(f)))

    def _check_fuel(self):
        """Every minute: a holding plane diverts once it could only just reach its alternate.

        Same sum as the fuel check in the rules: spare fuel - extra flying to the alternate must stay above the floor.
        """
        t, floor = self.t, self.lim["fuel_buffer_min"]
        for f in self.arrivals:
            if f["status"] != "airborne" or t < self._over_mci(f):    # still airborne after the landing step = still holding
                continue
            fuel = self._holding_fuel(f)
            if fuel is None:
                continue
            ac = self.aircraft[f["aircraft_id"]]
            extra = self.sc.flight_params[f["id"]]["alternate_extra_min"]
            if fuel - extra <= floor:
                self._set("aircraft", ac, ac["id"], "aircraft", "holding: fuel burned", fuel_minutes_extra=fuel)
                self._divert(f, extra)
            elif t % self.ph["clock_update_every_min"] == 0:
                self._set("aircraft", ac, ac["id"], "aircraft", "holding: fuel burned", fuel_minutes_extra=fuel)

    def _check_crews(self):
        """A crew that no longer has enough duty left for its flight (plus buffer) is replaced by a reserve crew, or the
        flight is cancelled. This is today's process: it only reacts, it does not plan ahead."""
        for d in sorted(self.departures, key=lambda d: (d["sched_dep"], d["id"])):
            if d["status"] not in WAITING_DEP or not d["crew_id"]:
                continue
            for role, field, _ in CREW_ROLES:
                crew = self._crew_for(d, role)
                left = crew[field] if crew else None
                if (crew["id"], role) not in self.crew_checked or left is None or left >= d["_crew_need"]:
                    continue
                self._fallback("reserve choice", d, f"{role} crew {crew['id']} short; no approved co-pilot crew change in time")
                reserve = self._find_reserve(role, d)
                if reserve:
                    self._assign_reserve(d, crew, role, field, reserve)
                elif self._extend_pilot_duty(d, crew, role, field, left):
                    continue
                else:
                    self._emit("crew_out_of_duty", "MCI", {"flight_id": d["id"], "crew_id": crew["id"], "role": role,
                                                          "duty_left_min": left, "needed_min": d["_crew_need"],
                                                          "reserve_found": False}, incident=True)
                    self._cancel_flight(d, "crew out of legal duty")
                    break

    def _extend_pilot_duty(self, d, crew, role, field, left):
        """No reserve is free and the pilots are only a little short: the captain and the airline extend the duty period
        (14 CFR 117.19 allows up to 2 h; we use a small one, and once per crew). A person decides this; the world just applies it."""
        ext = self.base["crew"]["pilot_extension_min"]
        if role != "pilot" or crew["id"] in self.extended or d["_crew_need"] - left > ext:
            return False
        self.extended.add(crew["id"])
        self._set("crews", crew, crew["id"], "crew", f"captain and airline extended the pilots' duty for {d['id']}", **{field: left + ext})
        self._emit("pilot_duty_extended", "MCI", {"flight_id": d["id"], "crew_id": crew["id"], "extension_min": ext,
                                                  "duty_left_before_min": left, "decision": "human",
                                                  "decided_by": "captain and crew scheduling", "decision_id": "D02",
                                                  "legal_basis": "pilot_fdp_extension_before_takeoff_max_h"})
        return True

    def _find_reserve(self, role, d):
        """First reserve crew of the right kind that is free, available in time, long enough on duty and allowed to fly it."""
        crew_cfg = self.base["crew"]
        kind = next(k for r, _, k in CREW_ROLES if r == role)
        field = next(f for r, f, _ in CREW_ROLES if r == role)
        late = d["est_dep"] % 1440 >= hhmm(crew_cfg["late_night_after"])
        international = self.stations[d["destination"]].get("region") == crew_cfg["international_region"]
        for c in self.reserves:
            if c["kind"] != kind or c.get("_used") or c["reserve_status"] not in crew_cfg["reserve_usable_status"]:
                continue
            restr = c["restrictions"] or []
            wait = max(0, d["est_dep"] - self.t) if crew_cfg["reserve_must_cover_wait"] else 0   # the reserve's duty also has to cover the wait
            if c["_avail"] > max(self.t, d["est_dep"]) or c[field] is None or c[field] < d["_crew_need"] + wait:
                continue
            if (late and "Late-night restriction" in restr) or (international and "International capable" not in restr):
                continue
            return c
        return None

    def _assign_reserve(self, d, crew, role, field, reserve):
        reserve["_used"] = True
        """The reserve takes over this role on the flight: the flight records it, the reserve is marked used, and from now on
        it is the reserve's own duty clock that runs down."""
        self._set("crews", reserve, reserve["id"], "crew", f"reserve crew assigned to {d['id']}", reserve_status="Assigned")
        d["_role_crew"][role] = reserve["id"]
        self.crew_checked.add((reserve["id"], role))
        self._set("flights", d, d["id"], "flight", f"reserve {role} crew {reserve['id']} replaced {crew['id']}",
                  reserve_crew_ids=sorted(set(d["_role_crew"].values())))
        self._emit("reserve_crew_assigned", "MCI", {"flight_id": d["id"], "crew_id": crew["id"], "role": role,
                                                    "reserve_id": reserve["id"], "reserve_duty_left_min": reserve[field]})

    def _thin_schedule(self):
        """Today's process when a long ground stop is announced: cancel some of the lowest-priority departures."""
        th, r = self.base["thinning"], self.restr
        if not r or r["program"] != "ground_stop" or r["end"] - r["start"] < th["min_stop_min"]:
            return
        lo, hi = r["start"], r["end"] + th["window_after_min"]
        in_window = [d for d in self.departures if lo <= d["sched_dep"] <= hi]
        room = min(int(th["max_fraction_of_window"] * len(in_window)) - len(self.thinned), th["max_per_announcement"],
                   th["max_flights"] - len(self.thinned))
        pool = [d for d in in_window if d["status"] in WAITING_DEP and d["priority"] in th["priorities"]
                and d["tarmac_since"] is None]
        pool.sort(key=lambda d: (th["priorities"].index(d["priority"]), d["sched_dep"], d["id"]))
        if self.orchestrated and (self.cp_cancelled or any(d["id"] in self.cp_delay for d in in_window)):
            return                                     # replaced: the approved co-pilot plan already decided this window
        for d in pool[:max(0, room)]:
            wait = self.cfg.get("orchestrated", {}).get("wait_for_pending_recommendation")
            if self.orchestrated and wait and self.pending_cp.get(d["id"], -1) > self.t:
                self.thinned.add(d["id"])         # the ops team waits for the pending recommendation, until its deadline
                self.deferred_thin[d["id"]] = (self.pending_cp[d["id"]], f"schedule thinning: {r['end'] - r['start']} min ground stop announced")
                self.thin_waits.append({"flight_id": d["id"], "from": self.t, "until": self.pending_cp[d["id"]], "outcome": None})
                continue
            self._fallback("schedule thinning", d, "no approved co-pilot plan for the stop window yet")
            self.thinned.add(d["id"])
            self._cancel_flight(d, f"schedule thinning: {r['end'] - r['start']} min ground stop announced")

    def _seat_event(self, f):
        if not self.publish_seats or f.get("direction") != "departure":
            return
        seats = f["_seats"] if f.get("_seats") else self.aircraft[f["aircraft_id"]]["seats"]
        self._emit("seats_updated", f["origin"], {"flight_id": f["id"], "seats": seats, "booked": f["pax_total"],
                                                  "sellable": self._room(f)})

    def publish_all_seats(self):
        for d in self.departures:
            self._seat_event(d)

    def set_pending(self, pending: dict):
        """The orchestrator: co-pilot proposals waiting for a person, flight -> deadline minute."""
        self.pending_cp = dict(pending)

    def _deferred_thinning(self):
        for fid, (until, reason) in list(self.deferred_thin.items()):
            d = self.flights[fid]
            w = next(x for x in reversed(self.thin_waits) if x["flight_id"] == fid)
            if d["status"] not in WAITING_DEP or fid in self.cp_cancelled or fid in self.cp_delay:
                w["outcome"] = "replaced by the approved co-pilot decision" if (fid in self.cp_cancelled or fid in self.cp_delay) \
                    else f"no longer waiting ({d['status']})"
                del self.deferred_thin[fid]
            elif self.t >= until:
                w["outcome"] = "not approved by its deadline: today's thinning acted"
                del self.deferred_thin[fid]
                self._fallback("schedule thinning", d, "the pending recommendation was not approved by its deadline")
                self._cancel_flight(d, reason)

    def _fallback(self, process, f, what):
        if self.orchestrated:
            self.fallbacks.append({"t": self.t, "process": process, "flight_id": f["id"], "what": what})
            self._emit("process_fallback", "MCI", {"process": process, "flight_id": f["id"], "what": what})

    def apply_decision(self, action: str, facts: dict) -> dict:
        """The operational systems carry out an APPROVED co-pilot decision (orchestrated mode only). Physics and law still
        hold: it is refused, with the reason, if the facts changed. A done change is confirmed as recovery_action_applied."""
        t = self.t
        out = self._apply(action, facts)
        self.applied.append({"t": t, "action": action, "facts": dict(facts), **out})
        if out["applied"] and action != "rebook_passenger":
            self._emit("recovery_action_applied", "MCI", {k: facts.get(k) for k in ("proposal_id", "plan_version", "action",
                                                                                   "flight_id", "new_departure", "crew_id", "role")})
        return out

    def _apply(self, action, facts):
        t = self.t
        f = self.flights.get(facts.get("flight_id"))
        if action in ("delay_flight", "cancel_flight", "swap_crews", "call_in_reserve_crew", "deadhead_crew"):
            if f is None or f["status"] not in WAITING_DEP:
                return {"applied": False, "reason": f"{facts.get('flight_id')} is no longer waiting ({f and f['status']})"}
        if action == "delay_flight":
            from datetime import datetime as _dt
            m = minute_of(_dt.fromisoformat(facts["new_departure"]))
            if m < t:
                return {"applied": False, "reason": "the new departure time has already passed"}
            self.cp_delay[f["id"]] = m
            self.reproject(f"co-pilot delay applied to {f['id']}")
            return {"applied": True}
        if action == "cancel_flight":
            self.cp_cancelled.add(f["id"])
            self._cancel_flight(f, "co-pilot: approved cancellation (B03/B04)")
            return {"applied": True}
        if action in ("swap_crews", "call_in_reserve_crew", "deadhead_crew"):
            role = facts.get("role")
            field = next((fld for r, fld, _ in CREW_ROLES if r == role), None)
            crew = self.crews.get(facts.get("crew_id"))
            if field is None or crew is None:
                return {"applied": False, "reason": "unknown crew or role"}
            busy = [g["id"] for g in self.flights.values() if g["id"] != f["id"] and g.get("direction") == "departure"
                    and g["status"] in WAITING_DEP + ("departed",) and self._crew_for(g, role) is crew]
            if busy:
                return {"applied": False, "reason": f"crew {crew['id']} is still on {busy[0]}"}
            if crew.get("is_reserve") and (crew.get("_used") or crew["reserve_status"] not in self.base["crew"]["reserve_usable_status"]
                                           or crew["_avail"] > max(t, f["est_dep"])):
                return {"applied": False, "reason": f"reserve {crew['id']} is not available"}
            wait = max(0, f["est_dep"] - t)
            if crew[field] is None or crew[field] < f["_crew_need"] + wait:
                return {"applied": False, "reason": f"crew {crew['id']} would not be legal for {f['id']} "
                                                    f"({crew[field]} min left, {f['_crew_need'] + wait} needed)"}
            if crew.get("is_reserve"):
                crew["_used"] = True
                self._set("crews", crew, crew["id"], "crew", f"reserve crew assigned to {f['id']} (approved co-pilot decision)",
                          reserve_status="Assigned")
            f["_role_crew"][role] = crew["id"]
            self.crew_checked.add((crew["id"], role))
            self._set("flights", f, f["id"], "flight", f"{role} crew {crew['id']} assigned (approved co-pilot decision)",
                      reserve_crew_ids=sorted(set(f["_role_crew"].values())))
            self._emit("reserve_crew_assigned" if crew.get("is_reserve") else "crew_reassigned", "MCI",
                       {"flight_id": f["id"], "crew_id": crew["id"], "role": role, "reserve_duty_left_min": crew[field],
                        **({"reserve_id": crew["id"]} if crew.get("is_reserve") else {})})
            return {"applied": True}
        if action == "rebook_passenger":
            to = self.flights.get(facts.get("flight_id"))
            if to is None or to["status"] not in WAITING_DEP or to["est_dep"] <= t:
                return {"applied": False, "reason": f"{facts.get('flight_id')} is no longer open for rebooking"}
            moved = []
            for pid in facts.get("passenger_ids") or []:
                b = next((b for b in self.bookings.get(pid, []) if b["status"] != "cancelled"
                          and (self.flights[b["flight_id"]].get("_next_day") or self.flights[b["flight_id"]]["status"] == "cancelled")
                          and self.flights[b["flight_id"]]["destination"] == to["destination"]), None)
                if b is None or self._room(to) < 1:
                    continue
                old = self.flights[b["flight_id"]]
                seg = self.passengers[pid]["segment"]
                if self.groups.get((old["id"], seg), {}).get("count", 0) > 0:
                    self._move_count(old, to, seg, 1)
                self._rebook_booking(pid, b, to)
                if old.get("_next_day"):
                    self._set("passengers", self.passengers[pid], pid, "passenger_overnight_flag",
                              "back on a flight today (approved co-pilot rebooking)", overnight=False)
                moved.append(pid)
            if not moved:
                return {"applied": False, "reason": "no passenger could be moved (seats or bookings changed)"}
            return {"applied": True, "moved": moved}
        return {"applied": False, "reason": f"'{action}' is not applied to the world in this phase"}

    def _crew_for(self, f, role):
        """The crew doing this role on this flight: the reserve that took over, or the flight's own crew."""
        cid = f.get("_role_crew", {}).get(role) or f["crew_id"]
        return self.crews.get(cid) if cid else None

    def _burn_duty(self, f, minutes):
        if minutes <= 0:
            return
        by_crew = {}
        for role, field, _ in CREW_ROLES:
            crew = self._crew_for(f, role)
            if crew and crew[field] is not None:
                by_crew.setdefault(crew["id"], {})[field] = max(0, crew[field] - minutes)
        for cid, kv in by_crew.items():
            self._set("crews", self.crews[cid], cid, "crew", "waiting uses up duty time", **kv)

    def _flag_illegal_crews(self):
        """At the start of the day: one event for each crew that already breaks a rule in the class data.
        Today's process lets them fly anyway; the event lets the co-pilot flag them later."""
        self.flagged_start = True
        rest_rule = self.base["crew"]["source_data_rules"]["short_rest"]
        for cid in sorted(self.illegal_crews):
            info = self.illegal_crews[cid]
            rest = [b for b in info["breaks"] if b["rule"] == rest_rule]
            short = [b for b in info["breaks"] if b["rule"] != rest_rule]
            if rest:       # a real legal problem: rest below the legal minimum
                self._emit("crew_illegal_rest", "MCI",
                           {"crew_id": cid, "flight_id": info["flight_id"], "rule": rest_rule, "legal_problem": True,
                            "min_rest_hours": rest[0]["min_rest_hours"], "required_hours": rest[0]["required_hours"],
                            "data_issue": info["data_issue"]})
            if short:      # only short for OUR planning estimate (flight + one more leg + buffer); not illegal
                self._emit("crew_short_for_plan", "MCI",
                           {"crew_id": cid, "flight_id": info["flight_id"], "rule": short[0]["rule"], "legal_problem": False,
                            "note": "short only for our planning estimate (flight + one more leg + buffer), not a legal limit",
                            "short": short, "data_issue": None})

    def _update_crowd(self):
        t, term = self.t, self.ph["terminal"]
        waiting = sum(f["pax_total"] for f in self.departures
                      if f["status"] in WAITING_DEP and t >= f["sched_dep"] - term["arrive_before_departure_min"])
        people = term["base_people"] + round(waiting * term["show_up_fraction"])
        s = self.surge
        if s and t >= s["start"]:
            grown = min(1.0, (t - s["start"]) / max(1, s["ramp"]))
            if self.restr is None and t >= self.sc.end:
                grown = min(grown, max(0.0, 1 - (t - self.sc.end) / max(1, s["decay"])))
            people += round(s["extra"] * grown)
        self._set("stations", self.mci, "MCI", "station", "people in the terminal changed", people_in_terminal=people)
        if t % self.ph["station_status_every_min"] == 0:      # what the station ops board publishes (a report, not a change)
            self._emit("station_status", "MCI", {"people_in_terminal": people, "terminal_capacity": self.mci.get("terminal_capacity"),
                                                 "ramp_open": self.mci["ramp_open"]})

    # ================================================================ diversion and cancellation

    def _divert(self, f, extra_flying_min):
        t, d = self.t, self.ph["diversion"]
        alt = self.sc.flight_params[f["id"]]["alternate"]
        self._set("flights", f, f["id"], "flight", "diverted: spare fuel too low to keep holding", status="diverted",
                  diverted_to=alt, tarmac_since=t, as_of=t)
        f["_resume_ready"] = t + d["refuel_turn_min"] + d["flight_back_min"]
        f["_holding"] = False
        self._mark_disrupted(f)
        ac = self.aircraft[f["aircraft_id"]]
        self._set("aircraft", ac, ac["id"], "aircraft", "diverted", airborne=False, current_station=alt, as_of=t)
        self._emit("flight_diverted", alt, {"flight_id": f["id"], "diverted_to": alt, "pax": f["pax_total"],
                                            "reason": "spare fuel too low to keep holding",
                                            "fuel_minutes_extra": self.aircraft[f["aircraft_id"]]["fuel_minutes_extra"],
                                            "extra_flying_min": extra_flying_min}, incident=True)
        self._emit("tarmac_started", alt, {"flight_id": f["id"], "diverted": True})
        self._mark_pax(f)
        self.reproject("a flight diverted")

    def _cancel_flight(self, f, reason):
        t = self.t
        self._end_tarmac(f, "flight cancelled")
        self._set("flights", f, f["id"], "flight", "cancelled", status="cancelled", cancel_reason=reason, as_of=t)
        self._mark_disrupted(f)
        self._emit("flight_cancelled", f["origin"], {"flight_id": f["id"], "reason": reason, "pax": f["pax_total"]},
                   incident=True)
        self._rebook_flight(f)
        if f["direction"] == "arrival" and f["_outbound"]:
            nxt = self.flights[f["_outbound"]]
            if nxt["status"] in WAITING_DEP:
                self._cancel_flight(nxt, "aircraft will not arrive (inbound flight cancelled)")

    # ================================================================ passengers: rebooking

    def _mark_pax(self, f):
        self.dirty_flights.add(f["id"])

    def _candidates(self, f):
        """Later same-day flights on the same route that have not left yet."""
        out = [g for g in self.flights.values()
               if g["id"] != f["id"] and not g.get("_next_day") and g["origin"] == f["origin"]
               and g["destination"] == f["destination"] and g["direction"] == f["direction"]
               and g["status"] in (WAITING_DEP if g["direction"] == "departure" else NOT_DEPARTED_ARR)
               and g["est_dep"] > self.t]
        return sorted(out, key=lambda g: (g["est_dep"], g["id"]))

    def _room(self, g):
        """Seats left that today's process is willing to sell (it never fills a plane past max_load_factor)."""
        seats = g["_seats"] if g.get("_seats") else self.aircraft[g["aircraft_id"]]["seats"]
        return max(0, int(seats * self.base["max_load_factor"]) - g["pax_total"])

    def _next_day_flight(self, f):
        """A next-day (7 Oct) flight on the same route with a free seat. Creates one if needed, up to a limit."""
        nd = self.base["next_day"]
        key = (f["origin"], f["destination"], f["direction"])
        for g in self.next_day[key]:
            if self._room(g) > 0:
                return g
        if len(self.next_day[key]) >= nd["max_flights_per_route"]:
            return None
        k = len(self.next_day[key])
        dep = 1440 + hhmm(nd["first_departure"]) + k * nd["spacing_min"]
        fid = f"WN3{sum(len(v) for v in self.next_day.values()) + 1:03d}"
        g = {"id": fid, "direction": f["direction"], "schedule_no": None, "origin": f["origin"],
             "destination": f["destination"], "sched_dep": dep, "sched_arr": dep + f["_block"], "est_dep": dep,
             "est_arr": dep + f["_block"], "edct": None, "aircraft_id": None, "crew_id": None,
             "next_destination": None, "priority": f["priority"], "status": "scheduled", "diverted_to": None,
             "tarmac_since": None, "cancel_reason": None, "controllable": None, "pax_total": 0,
             "source": "simulator", "as_of": self.t, "_next_day": True, "_seats": nd["seats"], "_block": f["_block"],
             "_orig_pax": 0, "actual_dep": None, "actual_arr": None, "_logged_delay": None, "_holding": False,
             "_resumed": False, "_resume_ready": None, "_land_key": None, "_tarmac_max": 0, "_inbound": None,
             "_outbound": None}
        self.flights[fid] = g
        for seg in SEG_ORDER:
            self.groups[(fid, seg)] = {"flight_id": fid, "segment": seg, "count": 0, "rebooked": 0, "refunded": 0,
                                       "need_hotel": 0, "source": "simulator", "as_of": self.t}
            self.orig_groups[(fid, seg)] = 0
        self.next_day[key].append(g)
        self.new_flights.append(fid)
        self.new_flights_all.append(fid)
        self._emit("next_day_flight_created", f["origin"], {"flight_id": fid, "route": f"{f['origin']}-{f['destination']}",
                                                            "departs": fmt(dep)})
        return g

    def _ready_for(self, pid, b):
        """Earliest minute this passenger can be at the airport for this booking (after their earlier leg), or None."""
        bks = self.bookings[pid]
        i = bks.index(b)
        if i == 0 or bks[i - 1]["status"] == "cancelled":
            return None
        pf = self.flights[bks[i - 1]["flight_id"]]
        arrives = pf["actual_arr"] if pf["status"] == "arrived" else pf["est_arr"]
        return arrives + self.lim["min_connection_min"]

    def _move_count(self, src, dst, seg, n):
        t = self.t
        gs, gd = self.groups[(src["id"], seg)], self.groups[(dst["id"], seg)]
        self._set("passenger_groups", gs, (src["id"], seg), "passenger_group", f"{n} rebooked to {dst['id']}",
                  count=gs["count"] - n, rebooked=gs["rebooked"] + n, as_of=t)
        self._set("passenger_groups", gd, (dst["id"], seg), "passenger_group", f"{n} rebooked from {src['id']}",
                  count=gd["count"] + n, as_of=t)
        self._set("flights", src, src["id"], "flight", "passengers rebooked away", pax_total=src["pax_total"] - n)
        self._set("flights", dst, dst["id"], "flight", "passengers rebooked onto flight", pax_total=dst["pax_total"] + n)
        self._seat_event(src)
        self._seat_event(dst)

    def _rebook_flight(self, f):
        """Everyone on a cancelled flight goes to the next flights to the same place that have room.
        If no seat is left today, they go on a next-day flight (7 Oct) and are marked as stranded overnight."""
        if not self.base["rebook_to_next_flight_with_room"]:
            return
        self._fallback("rebooking", f, "passengers rebooked by today's process (Passenger Care's approved advice moves them later)")
        cands = self._candidates(f)
        chunks = defaultdict(list)                                    # segment -> [[flight, seats left to hand out]]
        for seg in SEG_ORDER:
            left = self.groups[(f["id"], seg)]["count"]
            for g in cands:
                if left <= 0:
                    break
                take = min(left, self._room(g))
                if take > 0:
                    self._move_count(f, g, seg, take)
                    chunks[seg].append([g, take])
                    left -= take
            while left > 0:                                           # nothing left today: next day
                g = self._next_day_flight(f)
                if g is None:
                    break
                take = min(left, self._room(g))
                self._move_count(f, g, seg, take)
                self.overnight_pax += take
                chunks[seg].append([g, take])
                left -= take
        for pid in sorted(self.pax_by_flight[f["id"]], key=lambda p: (SEG_ORDER.index(self.passengers[p]["segment"]), p)):
            for b in self.bookings[pid]:
                if b["flight_id"] != f["id"] or b["status"] == "cancelled":
                    continue
                ready = self._ready_for(pid, b)
                seg_chunks = chunks[self.passengers[pid]["segment"]]
                target = next((c for c in seg_chunks if c[1] > 0 and (ready is None or c[0]["est_dep"] >= ready)), None)
                if target:
                    target[1] -= 1
                    self._rebook_booking(pid, b, target[0])
                else:                                                 # they can't make any of today's seats
                    nd = self._next_day_flight(f)
                    if nd:
                        self._rebook_booking(pid, b, nd)
                    else:
                        self._strand_booking(pid, b)

    def _rebook_booking(self, pid, b, new_flight):
        old = b["flight_id"]
        self._set("bookings", b, (pid, b["seq"]), "booking", f"rebooked from {old} to {new_flight['id']}",
                  flight_id=new_flight["id"], status="rebooked", as_of=self.t)
        self.pax_by_flight[old].discard(pid)
        self.pax_by_flight[new_flight["id"]].add(pid)
        self.dirty_pax.add(pid)
        self.rebooked_now.add(pid)
        if new_flight.get("_next_day"):
            person = self.passengers[pid]
            self._set("passengers", person, pid, "passenger_overnight_flag", "stranded overnight: rebooked on a next-day flight",
                      overnight=True)
            self._emit("passenger_stranded_overnight", "MCI",
                       {"passenger_id": pid, "lives_locally": person["lives_locally"], "from_flight": old,
                        "rebooked_to": new_flight["id"], "departs": fmt(new_flight["sched_dep"])}, incident=True)
        for tag in self.bags_by_pax[pid]:
            bag = self.bags[tag]
            self._set("bags", bag, tag, "bag", "bag re-tagged after rebooking",
                      routing=[new_flight["id"] if x == old else x for x in bag["routing"]], status="checked", as_of=self.t)

    def _strand_booking(self, pid, b):
        self._set("bookings", b, (pid, b["seq"]), "booking", "cancelled: no seat found on a later flight",
                  status="cancelled", as_of=self.t)
        self.pax_by_flight[b["flight_id"]].discard(pid)
        self.dirty_pax.add(pid)
        for tag in self.bags_by_pax[pid]:
            self._set("bags", self.bags[tag], tag, "bag", "bag held: passenger has no flight", status="held", as_of=self.t)

    def _missed_connections(self, d):
        """A flight leaves on time for itself; connecting passengers whose first leg is not in yet are rebooked,
        on a later flight they can reach today or, if there is none, on a next-day flight."""
        t = self.t
        for pid in sorted(self.pax_by_flight[d["id"]]):
            bks = self.bookings[pid]
            for i, b in enumerate(bks):
                if b["flight_id"] != d["id"] or i == 0 or b["status"] == "cancelled":
                    continue
                prev = bks[i - 1]
                pf = self.flights[prev["flight_id"]]
                made_it = prev["status"] != "cancelled" and pf["status"] == "arrived" \
                    and pf["actual_arr"] + self.lim["min_connection_min"] <= t
                if made_it:
                    continue
                ready = self._ready_for(pid, b)
                seg = self.passengers[pid]["segment"]
                target = next((g for g in self._candidates(d)
                               if self._room(g) > 0 and (ready is None or g["est_dep"] >= ready)), None)
                if target is None:
                    target = self._next_day_flight(d)
                if target is not None:
                    self._move_count(d, target, seg, 1)
                    if target.get("_next_day"):
                        self.overnight_pax += 1
                    self._rebook_booking(pid, b, target)
                else:
                    gs = self.groups[(d["id"], seg)]
                    self._set("passenger_groups", gs, (d["id"], seg), "passenger_group", "missed connection",
                              count=gs["count"] - 1, as_of=t)
                    self._set("flights", d, d["id"], "flight", "passenger did not make the flight", pax_total=d["pax_total"] - 1)
                    self._strand_booking(pid, b)
                self._emit("missed_connection", "MCI", {"passenger_id": pid, "flight_id": d["id"],
                                                        "rebooked_to": b["flight_id"] if b["status"] == "rebooked" else None})

    # ================================================================ bags

    def _bags_on(self, flight_id):
        return [tag for pid in sorted(self.pax_by_flight[flight_id]) for tag in self.bags_by_pax[pid]
                if flight_id in self.bags[tag]["routing"]]

    def _bags_arrive(self, f):
        for tag in self._bags_on(f["id"]):
            bag = self.bags[tag]
            last = bag["routing"][-1] == f["id"]
            self._set("bags", bag, tag, "bag", "bag arrived", current_location=f["destination"],
                      status="delivered" if last else "checked", as_of=self.t)

    # ================================================================ what each passenger's truth is

    def _leg_status(self, f, b):
        if b["status"] == "rebooked":
            base = "rebooked"
        else:
            base = "confirmed"
        if f.get("_next_day"):
            return "overnight"
        if f["status"] == "diverted":
            return "diverted"
        if f["direction"] == "arrival" and f["status"] == "airborne":
            late = f["est_arr"] - f["sched_arr"] >= self.ph["delay_flag_min"]
        else:
            late = f["status"] in ("ground_stop", "delayed", "held") and f["est_dep"] - f["sched_dep"] >= self.ph["delay_flag_min"]
        return "delayed" if late and base == "confirmed" else base

    def _leg_done(self, f):
        return f["status"] == "arrived" or (f["direction"] == "departure" and f["status"] == "departed")

    def pax_truth(self, pid) -> dict:
        """What is really true for this passenger right now (this is what every channel should show)."""
        bks = self.bookings[pid]
        live = [b for b in bks if b["status"] != "cancelled"]
        bag = self.bags[self.bags_by_pax[pid][0]]["routing"] if self.bags_by_pax[pid] else []
        status, message, worst = "confirmed", "Your trip is complete.", -1
        for b in live:
            f = self.flights[b["flight_id"]]
            if self._leg_done(f):
                continue
            s = self._leg_status(f, b)
            if SEVERITY[s] > worst:
                worst, status = SEVERITY[s], s
                when = fmt(f["est_arr"]) if f["direction"] == "arrival" else fmt(f["est_dep"])
                verb = "expected to land" if f["direction"] == "arrival" else "departing"
                if s == "delayed":
                    message = f"Flight {f['id']} is delayed, {verb} {when}."
                elif s == "rebooked":
                    message = f"You have been rebooked on flight {f['id']}, {verb} {when}."
                elif s == "overnight":
                    message = f"You are stranded overnight. You are rebooked on flight {f['id']} tomorrow, departing {fmt(f['est_dep'])}."
                elif s == "diverted":
                    message = f"Flight {f['id']} diverted to {f['diverted_to']}. We will update you soon."
                else:
                    message = f"Flight {f['id']} is on schedule, {verb} {when}."
        if len(live) != len(bks):
            status, message = "cancelled", "Your flight was cancelled and no seat was found yet. Please see an agent."
        return {"itinerary": tuple(b["flight_id"] for b in live), "status": status,
                "destination": self.passengers[pid]["final_destination"], "bag_routing": tuple(bag), "message": message}

    def collect_truth_changes(self):
        """Passengers whose truth changed this minute: (passenger_id, old, new, was_rebooked)."""
        for fid in self.dirty_flights:
            self.dirty_pax |= self.pax_by_flight[fid]
        self.dirty_flights.clear()
        changes = []
        for pid in sorted(self.dirty_pax):
            old, new = self.truth[pid], self.pax_truth(pid)
            key = lambda x: (x["itinerary"], x["status"], x["destination"], x["bag_routing"])   # noqa: E731
            if key(old) != key(new):
                self.audit_rows.append({"t": self.t, "actor": ACTOR, "actor_name": "world", "what": "passenger truth changed",
                                        "entity_type": "passenger", "entity_id": pid,
                                        "before": _plain(old), "after": _plain(new)})
                changes.append((pid, old, new, pid in self.rebooked_now))
            self.truth[pid] = new
        self.dirty_pax.clear()
        self.rebooked_now.clear()
        return changes

    # ================================================================ numbers for charts

    def status_counts(self) -> dict:
        out = defaultdict(int)
        for f in self.flights.values():
            if not f.get("_next_day"):
                out[f["status"]] += 1
        return dict(out)

    def open_tarmac_max(self) -> int:
        open_ = [self.t - f["tarmac_since"] for f in self.flights.values() if f["tarmac_since"] is not None]
        return max([self.max_tarmac] + open_)


def _plain(v: dict) -> dict:
    return {k: (list(x) if isinstance(x, tuple) else x) for k, x in v.items()}
