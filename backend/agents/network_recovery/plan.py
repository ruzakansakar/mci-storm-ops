"""The Network Recovery agent (Phase 4d, ADVISORY): it proposes; nothing it proposes is applied.

Every run (every few minutes, and when a trigger event is delivered) it rebuilds the network at T (state_at.py) and reads the
Situation agent's latest picture (shared state; the expected end stays a suspicion, with its confidence). Then:

  a. crew legality watch (D01 pilots / D08 flight attendants / D10 crews stuck at a diversion): when will each crew on a flight
     that is still waiting run out of legal duty, given the current delay and Situation's expected end (likely and worst case)?
     Warn at least warn_before_min ahead, and propose the best LEGAL option: a reserve (D03/D08), a swap with a crew whose own
     flight was cancelled (D11), or a deadhead (D04). Every option goes through the crew rule checks first; an option they
     reject is never proposed. A pilot extension (D02) is only ever a question to the captain and crew scheduling.
  b. delay / cancel plan (B02, B03, B04, B06): rank the departures in the stop window by a protection order and propose new times
     for the ones that wait and cancellations for the least protected, only where the passengers fit on later flights today.
  c. hold or divert (B09, B08 ask-only): for each airborne inbound, the latest safe time to leave for each alternate (spare fuel
     minus the extra flying, down to the fuel floor) against Situation's end range; warn early and list the alternates that pass.
  d. diverted aircraft (C04), maintenance deadlines (C02), curfews (B13) and the restart order when the stop lifts (B14/Q02).
  e. one version of the plan in recovery_plans (shared, append-only) whenever it changes.
Phase 4f: it reads Station & Staff's plan (ramp status, gate and staff shortages; shared state) and never proposes a departure
while the ramp is closed or in a 15-minute slot where the station has no ramp crew or gate agents left to work one more
departure: such a departure moves to the next slot that can work it, and the delay says why (station_check).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml

from backend.agents.network_recovery.state_at import FIELD, KIND, build_state, illegal_at, left_at, need_min
from backend.settings import ROOT, TZ

CREW_CHECKS = ["crew_duty_left", "crew_rest", "crew_available", "pilot_fdp_limit", "pilot_cumulative_limits",
               "pilot_not_fatigued", "fa_duty_limit", "fa_rest"]


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "agent_settings" / "network_recovery.yaml").read_text(encoding="utf-8"))


def hhmm(t) -> str:
    return t.astimezone(TZ).strftime("%H:%M") if t else "-"


@dataclass
class Memory:
    static: dict | None = None
    version: int = 0
    last_key: object = None
    proposed: dict = field(default_factory=dict)       # dedupe key -> when proposed
    warned_crew: dict = field(default_factory=dict)    # (flight, role) -> first warning {at, illegal_at, likely}
    warned_fuel: dict = field(default_factory=dict)    # flight -> first warning {at, leave_by}
    asked: set = field(default_factory=set)
    stop_started: datetime | None = None
    restart_done: set = field(default_factory=set)
    plans: list = field(default_factory=list)          # every version written (for pages)
    basis: dict = field(default_factory=dict)          # the upstream versions it last read (stamped on every version it writes)


def _static(ctx, memory):
    if memory.static is None:
        memory.static = {t: ctx.read(t) for t in ("flights", "aircraft", "crews", "stations", "later_capacity", "route_tiers",
                                                  "passenger_groups")}
        g = {}
        for r in memory.static["passenger_groups"]:
            g.setdefault(r["flight_id"], {})[r["segment"]] = r["count"]
        memory.static["groups"] = g
    return memory.static


def _end(pic, key):
    v = ((pic or {}).get("suspected") or {}).get(key) or {}
    val = v.get("value")
    return datetime.fromisoformat(val) if isinstance(val, str) else val


def _limits(ctx):
    from backend.rules.safety import limits_for
    return limits_for(ctx.rules)


def _out(title, why, now, options, decision_ids, flights, pax, reserves=(), sources=None):
    return {"title": title, "why": why, "data_as_of": now.isoformat(), "options": options, "decision_ids": decision_ids,
            "flights_affected": sorted(set(flights)), "passengers_affected": int(pax), "reserve_crews_needed": list(reserves),
            "sources": sources or []}


def _opt(oid, summary, flights, pax=0, score=0.0):
    return {"id": oid, "summary": summary[:500], "flights": list(flights), "passengers_disrupted": int(max(0, pax)), "cost_usd": 0,
            "network_score": float(score)}


ESTIMATE_METHOD = ("each cancelled flight's booked passengers go to the open seats later today to the same city (published later "
                   "capacity), in order of cancellation; whoever does not fit is counted as stranded overnight")


def estimate_stranded(cancelled, seats) -> dict:
    """ESTIMATE (a plan that is not applied can't be measured): cancelled = [(flight, booked passengers, destination)].
    The same method is used for the plan and for today's process, so the two can be compared."""
    left, stranded = dict(seats), 0
    for fid, pax, dest in cancelled:
        fit = min(pax, max(0, left.get(dest, 0)))
        left[dest] = left.get(dest, 0) - fit
        stranded += pax - fit
    return {"label": "estimate: the plan is not applied", "cancellations": len(cancelled), "passengers_stranded_overnight": stranded,
            "method": ESTIMATE_METHOD}


class Planner:
    def __init__(self, ctx, memory: Memory, s: dict):
        self.ctx, self.m, self.s, self.now = ctx, memory, s, ctx.now
        self.static = _static(ctx, memory)
        self.lim = _limits(ctx)
        self.buffer = self.lim["crew_duty_buffer_min"]
        self.proposals = []                            # for the (advisory) executor: what the rule check should look at
        self.station = None                            # Station and Staff plan (Phase 4f), read in plan()
        self.inv, self.held, self.guards_last = {}, {}, {}   # fix-seats: seat inventory at T and seats held / promised
        self.sources = [{"fact": "flight operations events", "source": "flight_ops", "as_of": ctx.feed_last("flight_ops").isoformat()},
                        {"fact": "published schedule, crew roster and fleet", "source": "plan published before the day",
                         "as_of": self.static["flights"][0]["sched_dep"].replace(hour=5, minute=0).isoformat()}]

    # ------------------------------------------------------------ helpers
    def propose(self, key, action, out, facts, recheck=True):
        """One proposal through AgentContext (it records it; the rule check decides; nothing is applied)."""
        if key in self.m.proposed and (not recheck or self.now - self.m.proposed[key] < timedelta(
                minutes=self.s["plan"]["recheck_proposal_min"])):
            return None
        if action in self.ctx.contract.required_checks:
            for name in self.ctx.contract.required_checks[action]:
                self.ctx.run_check(name, facts.get("option") or {})
        rid = self.ctx.propose(action, out)
        self.m.proposed[key] = self.now
        self.proposals.append({"agent": "network_recovery", "action": action, "decision_ids": out["decision_ids"],
                               "data_as_of": self.now, "id": rid, "requested_level": "needs_approval", **facts})
        return rid

    def ask(self, key, decision, facts, reason):
        if key in self.m.asked:
            return
        self.m.asked.add(key)
        self.ctx.ask_human(decision, facts, reason)

    def legal(self, option) -> list[str]:
        """The crew rule checks this action needs, on these facts. Empty = legal (the agent's own check; never final)."""
        out = []
        for name in CREW_CHECKS:
            why = self.ctx.run_check(name, option)
            if why:
                out.append(why)
        return out

    # ------------------------------------------------------------ the run
    def run(self, events, pic):
        st = build_state(self.static["flights"], self.static["crews"], events, self.now)
        self.st, self.pic = st, pic
        status = ((pic or {}).get("facts") or {}).get("status") or ""
        self.end_likely, self.end_latest = _end(pic, "expected_end_likely"), _end(pic, "expected_end_latest")
        self.active = status == "abnormal" and self.end_likely is not None
        if self.active and self.m.stop_started is None:
            self.m.stop_started = self.now
        restart = timedelta(minutes=self.s["crew"]["restart_after_end_min"])
        self.restart_likely = self.end_likely + restart if self.active else None
        self.restart_worst = (self.end_latest or self.end_likely) + restart if self.active else None
        self.reserved = set()
        crew = self.crew_watch()
        fuel = self.fuel_watch()
        plan = self.delay_cancel(crew)
        recovery = self.diverted_aircraft()
        restart_order = self.restart_order()
        return {"crew": crew, "fuel": fuel, **plan, "aircraft_recovery": recovery, "restart_order": restart_order}

    def projected(self, f):
        """(likely, worst) departure time: the published estimate, or after Situation's expected end if the stop is on."""
        likely = worst = f.est_dep
        if self.active and f.est_dep <= self.restart_worst + timedelta(hours=2):
            likely = max(likely, self.restart_likely)
            worst = max(worst, self.restart_worst)
        return likely, worst

    # ------------------------------------------------------------ a. crews
    def crew_watch(self):
        st, out = self.st, []
        today = [f for f in st.flights.values() if f.direction == "departure" and not f.next_day and f.waiting and f.crew_id]
        for f in sorted(today, key=lambda f: f.sched_dep):
            need = need_min(f, self.s, self.buffer)
            for role in ("pilot", "fa"):
                x = illegal_at(st, f, role, need)
                if x is None or x < f.sched_dep:
                    continue                               # short in the class data before the day starts: not today's problem
                likely, worst = self.projected(f)
                if worst <= x:
                    continue
                w = {"flight_id": f.id, "role": role, "crew_id": (f.reserves.get(role) or (f.crew_id,))[0],
                     "illegal_at": x.isoformat(), "projected_departure_likely": likely.isoformat(),
                     "projected_departure_worst": worst.isoformat(), "likelihood": "likely" if likely > x else "possible",
                     "minutes_ahead": int((x - self.now).total_seconds() // 60)}
                first = self.m.warned_crew.get((f.id, role))
                if first is None or (first["likelihood"] == "possible" and w["likelihood"] == "likely"):
                    self.m.warned_crew[(f.id, role)] = {**w, "at": self.now.isoformat(),
                                                        "first_at": (first or {}).get("first_at", self.now.isoformat())}
                    self.ctx.record_event("crew_warning", w)
                opts = self.crew_options(f, role, need, worst)
                w["options"] = [{k: v for k, v in o.items() if k != "facts"} for o in opts]
                w["best"] = next((o["kind"] for o in opts if o["legal"]), "none")
                out.append(w)
                self._propose_crew(f, role, w, opts, need)
        return out

    def crew_options(self, f, role, need, dep):
        """Reserve, swap or deadhead, each run through the crew checks (the illegal ones are kept only to say why not)."""
        st, s, out = self.st, self.s["crew"], []
        wait_until = max(dep, self.now)
        late = dep.astimezone(TZ).strftime("%H:%M") >= s["late_night_after"]
        region = {r["code"]: r["region"] for r in self.static["stations"]}.get(f.destination)
        for c in sorted(st.crews.values(), key=lambda c: (c.get("available_from") or self.now, c["id"])):
            if not c.get("is_reserve") or c["kind"] != KIND[role] or c["id"] in st.used_reserves or c["id"] in self.reserved:
                continue
            if c.get("reserve_status") not in s["usable_reserve_status"]:
                continue
            avail = max(c.get("available_from") or self.now, self.now)
            if avail > wait_until:
                continue
            restr = c.get("restrictions") or []
            if (late and "Late-night restriction" in restr) or (region == s["international_region"] and "International capable" not in restr):
                continue
            wait = int((wait_until - avail).total_seconds() // 60)
            left = (c.get(FIELD[role]) or 0) - wait
            facts = self._crew_facts(c, role, left, f, avail, wait)
            why = self.legal(facts)
            out.append({"kind": "reserve", "crew_id": c["id"], "duty_left_at_departure": left, "legal": not why, "why_not": why,
                        "action": "call_in_reserve_crew", "facts": facts})
        for g in st.flights.values():                    # swap: the crew of a flight that was cancelled (it is free now)
            if g.id == f.id or g.status not in self.s["crew"]["swap_from_status"] or not g.crew_id or g.next_day                     or g.crew_id in self.reserved:
                continue
            c = st.crews.get(g.crew_id)
            if c is None or c.get(FIELD[role]) is None:
                continue
            # cautious: the crew has been on duty since its own (cancelled) flight was due out, and stays on duty until this one
            wait = int((wait_until - self.now).total_seconds() // 60)
            left = c[FIELD[role]] - max(0, int((wait_until - g.sched_dep).total_seconds() // 60))
            facts = self._crew_facts(c, role, left, f, self.now, wait)
            why = self.legal(facts)
            out.append({"kind": "swap", "crew_id": c["id"], "from_flight": g.id, "duty_left_at_departure": left, "legal": not why,
                        "why_not": why, "action": "swap_crews", "facts": facts})
        for g in st.flights.values():                    # deadhead: a crew landing at MCI as passengers in time, with duty left
            if g.direction != "arrival" or g.status in ("arrived", "cancelled", "diverted") or not g.crew_id                     or g.crew_id in self.reserved:
                continue
            land = max(g.sched_arr, self.restart_likely or g.sched_arr)
            ready = land + timedelta(minutes=s["deadhead_after_landing_min"])
            if ready > wait_until:
                continue
            c = st.crews.get(g.crew_id)
            if c is None or c.get(FIELD[role]) is None:
                continue
            left = c[FIELD[role]] - g.block - int((wait_until - g.sched_dep).total_seconds() // 60 - g.block)
            facts = self._crew_facts(c, role, left, f, ready, 0)
            why = self.legal(facts)
            out.append({"kind": "deadhead", "crew_id": c["id"], "on_flight": g.id, "duty_left_at_departure": left, "legal": not why,
                        "why_not": why, "action": "deadhead_crew", "facts": facts})
        order = {"reserve": 0, "swap": 1, "deadhead": 2}
        out.sort(key=lambda o: (not o["legal"], order[o["kind"]], -o["duty_left_at_departure"]))
        return out[:6]

    def _crew_facts(self, c, role, left, f, start, wait):
        """The facts the crew rule checks read (backend/rules/crew.py, safety.py), for this crew flying this flight."""
        planned = f.block + self.s["crew"]["onward_leg_min"]
        rest = c.get("min_rest_hours")
        if rest is None and c.get("is_reserve"):
            rest = self.s["crew"]["reserve_rest_hours"]          # assumption (settings): a usable reserve is rested
        crew = {"duty_left_min": left, "rest_hours": rest, "availability": c.get("availability"),
                "fatigue_reported": bool(c.get("fatigue_reported"))}
        option = {"crew": crew, "planned_duty_min": planned, "flight_id": f.id}
        if c.get("is_reserve") and role == "pilot":        # a reserve's duty period starts when it is called in: Table B applies
            crew.update(planned_fdp_h=round((wait + planned) / 60, 2), fdp_start=start.astimezone(TZ).strftime("%H:%M"),
                        segments=2)
        if c.get("is_reserve") and role == "fa":
            option["fa"] = {"duty_h": round((wait + planned) / 60, 2), "extra_fas": 0}
        return option

    def _propose_crew(self, f, role, w, opts, need):
        legal = [o for o in opts if o["legal"]]
        who = "pilots" if role == "pilot" else "flight attendants"
        dec = ["D01"] if role == "pilot" else ["D08"]
        summary = (f"{f.id}'s {who} ({w['crew_id']}) run out of legal duty at {hhmm(datetime.fromisoformat(w['illegal_at']))}; "
                   f"departure expected {hhmm(datetime.fromisoformat(w['projected_departure_likely']))} "
                   f"(worst {hhmm(datetime.fromisoformat(w['projected_departure_worst']))}). Best legal option: "
                   + (f"{legal[0]['kind']} {legal[0]['crew_id']}" if legal else "none"))
        out = _out(f"Crew legality: {f.id} {who} ({w['likelihood']})", summary, self.now,
                   [_opt(f"{o['kind']}-{o['crew_id']}", f"{o['kind']} {o['crew_id']}: "
                         + ("legal" if o["legal"] else "not legal: " + "; ".join(o["why_not"][:2])), [f.id], f.pax)
                    for o in opts] or [_opt("none", "no reserve, swap or deadhead found", [f.id], f.pax)],
                   dec, [f.id], f.pax, sources=self.sources)
        self.propose(("warn", f.id, role, w["likelihood"], legal[0]["crew_id"] if legal else None), "flag_crew_legality", out,
                     {"flight_id": f.id, "option": {}}, recheck=False)
        due = self.now >= datetime.fromisoformat(w["illegal_at"]) - timedelta(minutes=self.s["crew"]["call_in_lead_min"])
        # fix-seats: a scarce crew is called in only when it is needed: not on a guess, and not before the lead time it takes to
        # approve and report (its duty clock starts when it is called; the warning still goes as early as before)
        if legal:
            self.reserved.add(legal[0]["crew_id"])     # earmarked for this flight in the plan now (never offered twice)
        if legal and self.not_a_guess(f) and due:
            best = legal[0]
            dec2 = {"reserve": ["D03"] if role == "pilot" else ["D08"], "swap": ["D11"], "deadhead": ["D04"]}[best["kind"]]
            title = {"reserve": f"Call in reserve {best['crew_id']} for {f.id} ({who})",
                     "swap": f"Swap crew {best['crew_id']} (from cancelled {best.get('from_flight')}) onto {f.id} ({who})",
                     "deadhead": f"Deadhead crew {best['crew_id']} on {best.get('on_flight')} for {f.id} ({who})"}[best["kind"]]
            out2 = _out(title, summary, self.now, [_opt(f"{best['kind']}-{best['crew_id']}", title, [f.id], 0, 1.0)], dec2, [f.id], f.pax,
                        reserves=[best["crew_id"]] if best["kind"] == "reserve" else [], sources=self.sources)
            self.propose(("act", f.id, role, best["crew_id"]), best["action"], out2,
                         {"flight_id": f.id, "option": best["facts"], "assign": {"crew_ids": [best["crew_id"]]},
                          "crew_id": best["crew_id"], "crew_role": role}, recheck=False)
        elif role == "pilot":
            self.ask(("D02", f.id), "D02", {"flight_id": f.id, "crew_id": w["crew_id"], "illegal_at": w["illegal_at"]},
                     "No legal reserve, swap or deadhead: only the captain and crew scheduling can decide a duty extension.")

    # ------------------------------------------------------------ c. fuel
    def fuel_watch(self):
        out = []
        if not self.active:
            return out
        floor = self.lim["fuel_buffer_min"]
        alts = self.s["fuel"]["alternates"]
        stations = {r["code"]: r for r in self.static["stations"]}
        for f in self.st.flights.values():
            if f.direction != "arrival" or f.status not in ("airborne", "holding") or f.fuel0 is None or f.airborne_at is None:
                continue
            over = f.airborne_at + timedelta(minutes=f.block)
            leave_by = {a: over + timedelta(minutes=f.fuel0 - x - floor) for a, x in alts.items()}
            last = max(leave_by.values())
            worst, likely = self.end_latest or self.end_likely, self.end_likely
            if worst <= last:
                continue
            fuel_now = max(0, f.fuel0 - max(0, int((self.now - over).total_seconds() // 60)))
            safe = []
            for a, x in sorted(alts.items(), key=lambda kv: kv[1]):
                opt = {"aircraft": {"fuel_minutes_extra": fuel_now}, "minutes_to_alternate": x,
                       "airport": a, "operation": "arrival", "local_time": (self.now + timedelta(minutes=x)).astimezone(TZ).strftime("%H:%M")}
                why = [w for w in (self.ctx.run_check("fuel_reserve", opt), self.ctx.run_check("airport_curfew", opt)) if w]
                if stations.get(a, {}).get("is_diversion_option") is False:
                    why.append(f"{a} is not a diversion airport")
                if not why:
                    safe.append({"airport": a, "leave_by": hhmm(leave_by[a]), "extra_flying_min": x})
            w = {"flight_id": f.id, "fuel_now_min": fuel_now, "over_mci_at": over.isoformat(), "must_leave_by": last.isoformat(),
                 "stop_end_likely": likely.isoformat(), "stop_end_worst": worst.isoformat(),
                 "likelihood": "likely" if likely > last else "possible", "safe_alternates": safe}
            if f.id not in self.m.warned_fuel:
                self.m.warned_fuel[f.id] = {"at": self.now.isoformat(), **w}
                self.ctx.record_event("fuel_warning", w)
            self.ask(("B09", f.id, w["likelihood"]), "B09", w,
                     f"{f.id} cannot hold until the stop's {'likely' if likely > last else 'latest'} end: it must leave for an "
                     f"alternate by {hhmm(last)}. Divert or keep holding is for the dispatcher and the captain.")
            out.append(w)
        return out

    # ------------------------------------------------------------ b. delays and cancellations
    def protection(self, f, seats_later):
        p = self.s["plan"]["protection"]
        groups = self.static["groups"].get(f.id, {})
        later = [g for g in self.st.flights.values() if g.direction == "departure" and g.destination == f.destination
                 and not g.next_day and g.sched_dep > f.sched_dep]
        region = {r["code"]: r["region"] for r in self.static["stations"]}.get(f.destination)
        score = p["priority"].get(f.priority, 0) + p["per_connecting_pax"] * groups.get("connecting", 0) + p["per_pax"] * f.pax
        score += p["last_flight_to_city"] if not later else 0
        score += p["international"] if region == self.s["crew"]["international_region"] or groups.get("international") else 0
        score += p["no_seats_later_today"] if seats_later.get(f.destination, 0) <= 0 else 0
        return round(score, 1)

    # ------------------------------------------------------------ seats (fix-seats: backend/rules/seats.py, shared with Care)
    def later_ids(self, f):
        """Later departures today to the same city that a cancelled flight's passengers could be moved to."""
        when = f.est_dep or f.sched_dep
        return [g.id for g in self.st.flights.values() if g.direction == "departure" and not g.next_day and g.id != f.id
                and g.destination == f.destination and g.waiting and (g.est_dep or g.sched_dep) > when]

    def seats_later(self):
        """destination -> free seats on later departures today (real seats now, minus what is held or promised)."""
        from backend.rules.seats import free
        out = {}
        for g in self.st.flights.values():
            if g.direction == "departure" and not g.next_day and g.waiting and (g.est_dep or g.sched_dep) > self.now:
                out[g.destination] = out.get(g.destination, 0) + free(g.id, self.inv, self.held)
        return out

    def not_a_guess(self, f):
        """Act (cancel, call in a crew) only if the flight is actually held or delayed by the incident, or Situation's end is at
        least medium confidence. Otherwise the stop may end sooner than feared and the scarce reserve or seat is wasted."""
        held = f.status == "delayed" or (f.est_dep is not None and f.est_dep > f.sched_dep)
        conf = (((self.pic or {}).get("suspected") or {}).get("expected_end_likely") or {}).get("confidence")
        return held or (conf is not None and conf >= self.s["plan"]["cancel_needs_end_confidence"])

    def no_alternative(self, f, stuck):
        """Crew or aircraft legality leaves no way to fly it: its crew will time out with no legal option, or its aircraft
        will not come (the inbound flight is cancelled)."""
        if f.id in stuck:
            return "its crew will run out of legal duty and no legal reserve, swap or deadhead exists"
        inb = [g for g in self.st.flights.values() if g.direction == "arrival" and g.aircraft_id == f.aircraft_id
               and g.sched_arr <= f.sched_dep and g.status == "cancelled"]
        return "its aircraft will not arrive (inbound cancelled)" if inb else None

    def delay_cancel(self, crew):
        from backend.rules.seats import stranded_if_cancelled, free
        st = self.st
        seats = self.seats_later()
        empty = {"delays": [], "cancels": [], "protection": [], "estimates": {}, "seats_by_destination": {}}
        if not self.active:
            return {**empty, "seats_by_destination": self._seats(seats, [])}
        window = [f for f in st.flights.values() if f.direction == "departure" and not f.next_day and f.waiting
                  and f.sched_dep <= self.restart_worst]
        ranked = sorted(window, key=lambda f: (-self.protection(f, seats), f.sched_dep, f.id))
        cancels, left, guards = [], dict(seats), {}
        stop_min = (self.end_latest - self.m.stop_started).total_seconds() / 60 if self.end_latest else 0
        stuck = {w["flight_id"] for w in crew if w["likelihood"] == "likely" and w["best"] == "none" and w["role"] == "fa"}
        if stop_min >= self.s["plan"]["cancel_only_if_stop_min"]:
            # Phase 5b: departures in this stop window already cancelled (by today's process or an approved plan) count too
            gone = [f for f in st.flights.values() if f.direction == "departure" and not f.next_day and f.cancelled_at
                    and f.cancelled_at >= self.m.stop_started and f.sched_dep <= self.restart_worst]
            budget = int(self.s["plan"]["max_cancel_fraction"] * (len(window) + len(gone))) - len(gone)
            conf = (((self.pic or {}).get("suspected") or {}).get("expected_end_likely") or {}).get("confidence")
            for f in [x for x in window if x.id in stuck] + list(reversed(ranked)):
                if len(cancels) >= budget or f in cancels:
                    continue
                no_alt = self.no_alternative(f, stuck)
                if not no_alt and not self.not_a_guess(f):
                    continue                    # no cancelling on a guess: not held by the incident, and the end is uncertain
                pax = (self.inv.get(f.id) or {}).get("booked", f.pax)
                later = self.later_ids(f)
                est = stranded_if_cancelled(pax, later, self.inv, self.held)
                alternative = 0                 # delayed or kept, its passengers still fly today
                if no_alt or est["stranded"] <= alternative:
                    cancels.append(f)
                    guards[f.id] = {"stranded": est["stranded"], "rebooked_today": est["rebooked_today"],
                                    "alternative": "none (" + no_alt + ")" if no_alt else "delay or keep as scheduled",
                                    "alternative_stranded": None if no_alt else alternative, "no_alternative": bool(no_alt)}
                    need = est["rebooked_today"]          # the seats its passengers would take are no longer free for others
                    for gid in sorted(later, key=lambda x: self.st.flights[x].est_dep or self.st.flights[x].sched_dep):
                        take = min(need, free(gid, self.inv, self.held))
                        self.held[gid] = self.held.get(gid, 0) + take
                        need -= take
                    left[f.destination] = max(0, left.get(f.destination, 0) - est["rebooked_today"])
        delays = []
        slot = self.restart_likely
        used = {}                                             # departures it already placed in each station-constrained slot
        for f in ranked:
            if f in cancels:
                continue
            new = max(f.est_dep, slot)
            fit, why = self.station_fit(new, used)
            if fit != new:                                    # the following departures continue from the slot it moved to
                slot = max(slot, fit)
                new = fit
            slot += timedelta(minutes=self.s["plan"]["restart_spacing_min"])
            if new > f.sched_dep:
                delays.append({"flight_id": f.id, "new_departure": new.isoformat(), "delay_min": int((new - f.sched_dep).total_seconds() // 60),
                               "protection": self.protection(f, seats), **({"station_check": why} if why else {})})
        for d in delays:
            f = st.flights[d["flight_id"]]
            need = need_min(f, self.s, self.buffer)
            new = datetime.fromisoformat(d["new_departure"])
            ok = all((left_at(st, f, r, new) or 10 ** 6) >= need for r in ("pilot", "fa"))
            d["crew_legal"] = ok
            facts = {"crew": {"duty_left_min": min(left_at(st, f, r, new) or 10 ** 6 for r in ("pilot", "fa"))},
                     "planned_duty_min": f.block + self.s["crew"]["onward_leg_min"]}
            if not ok:
                continue                                          # never propose a delay the crew rules reject (crew watch handles it)
            out = _out(f"Delay {f.id} to {hhmm(new)}", f"stop expected to end {hhmm(self.end_likely)}; protection {d['protection']}",
                       self.now, [_opt(f"delay-{f.id}", f"{f.id} to {hhmm(new)}", [f.id], f.pax, d["protection"])], ["B02"],
                       [f.id], f.pax, sources=self.sources)
            self.propose(("delay", f.id, hhmm(new)), "delay_flight", out,
                         {"flight_id": f.id, "option": facts, "new_departure": d["new_departure"]}, recheck=False)
        for f in cancels:
            out = _out(f"Cancel {f.id} (to {f.destination})", "least protected in the stop window; its passengers fit on later flights "
                       "today" if f.id not in stuck else "its flight attendants will run out of legal duty and no legal option exists",
                       self.now, [_opt(f"cancel-{f.id}", f"cancel {f.id}", [f.id], f.pax, -self.protection(f, seats))],
                       ["B03", "B04"], [f.id], f.pax, sources=self.sources)
            self.propose(("cancel", f.id), "cancel_flight", out, {"flight_id": f.id, "option": {"cancellation": guards[f.id]}},
                         recheck=False)
        order = [{"flight_id": f.id, "protection": self.protection(f, seats)} for f in ranked]
        key = tuple(o["flight_id"] for o in order[:20])
        if order:
            out = _out("Protection order for the stop window", "priority, connections, last flight to a city, international, load, "
                       "seats later today", self.now, [_opt("order", " > ".join(key), list(key), 0, 1.0)], ["B06"], list(key), 0,
                       sources=self.sources)
            self.propose(("order", key), "set_flight_protection_order", out, {"option": {}}, recheck=False)
        self.guards_last = guards
        est = self.estimate([f.id for f in cancels], seats)
        return {"delays": delays, "cancels": [{"flight_id": f.id, "pax": f.pax, "destination": f.destination,
                                               "why": "crew" if f.id in stuck else "least protected; seats later today",
                                               "stranded_estimate": guards[f.id]["stranded"],
                                               "alternative": guards[f.id]["alternative"],
                                               "alternative_stranded": guards[f.id]["alternative_stranded"]} for f in cancels],
                "protection": order, "estimates": est, "seats_by_destination": self._seats(left, cancels)}

    def station_fit(self, t, used):
        """Phase 4f: the first time from t when the station can work one more departure (ramp open; a ramp crew and gate agents
        left in that 15-minute slot after the arrivals' work). Slots the station reports no shortage for are not limited."""
        facts = (self.station or {}).get("facts") or {}
        if not facts:
            return t, None
        notes = []
        ramp = facts.get("ramp_status") or {}
        if ramp.get("open") is False and ramp.get("expected_reopen"):
            reopen = datetime.fromisoformat(ramp["expected_reopen"])
            if t < reopen:
                notes.append(f"ramp closed for lightning until about {hhmm(reopen)}")
                t = reopen
        caps, ends = {}, {}
        for s_ in facts.get("staff_shortages") or []:
            if s_.get("departures_workable") is None:
                continue
            a = datetime.fromisoformat(s_["from"])
            caps[a] = min(caps.get(a, 10 ** 6), int(s_["departures_workable"]))
            ends[a] = datetime.fromisoformat(s_["to"])
        for _ in range(len(caps) + 1):
            a = next((k for k in sorted(caps) if k <= t < ends[k]), None)
            if a is None or used.get(a, 0) < caps[a]:
                break
            notes.append(f"no ramp crew or gate agents free {hhmm(a)}-{hhmm(ends[a])} (station plan v{self.station.get('version')})")
            t = ends[a]
        a = next((k for k in caps if k <= t < ends[k]), None)
        if a is not None:
            used[a] = used.get(a, 0) + 1
        return t, "; ".join(notes) or None

    def estimate(self, cancelled_ids, seats):
        """ESTIMATE of passengers stranded by the planned cancellations, with real seats (fix-seats)."""
        return {"label": "estimate: the plan is not applied", "cancellations": len(cancelled_ids),
                "passengers_stranded_overnight": sum(self.guards_last.get(fid, {}).get("stranded", 0) for fid in cancelled_ids),
                "method": "each cancelled flight's booked passengers go to free seats on later flights today to the same city "
                          "(published seat inventory, minus seats held or promised); whoever does not fit is stranded"}

    def _seats(self, left, cancels):
        nxt = {}
        for f in self.st.flights.values():
            if f.next_day:
                nxt.setdefault(f.destination, []).append(f.id)
        return {d: {"open_seats_later_today": max(0, n), "flights_tomorrow_created": nxt.get(d, [])} for d, n in sorted(left.items())}

    # ------------------------------------------------------------ d. diverted aircraft, maintenance, curfews, restart
    def diverted_aircraft(self):
        out = []
        r = self.s["recovery"]
        fleet = {a["id"]: a for a in self.static["aircraft"]}
        for f in self.st.flights.values():
            if f.status != "diverted" or not f.aircraft_id:
                continue
            back = max(self.now, self.end_likely or self.now) + timedelta(minutes=r["turn_at_alternate_min"] + r["flight_back_min"])
            nxt = sorted((g for g in self.st.flights.values() if g.aircraft_id == f.aircraft_id and g.direction == "departure"
                          and g.waiting), key=lambda g: g.sched_dep)
            leg = nxt[0] if nxt else None
            item = {"flight_id": f.id, "aircraft_id": f.aircraft_id, "at": f.diverted_to, "back_at_mci_by": back.isoformat(),
                    "next_flight": leg.id if leg else None,
                    "next_flight_late_min": int((back - leg.sched_dep).total_seconds() // 60) if leg and back > leg.sched_dep else 0}
            out.append(item)
            out_ = _out(f"Bring {f.aircraft_id} back from {f.diverted_to}", f"back at MCI by about {hhmm(back)}"
                        + (f"; next flight {leg.id} would be {item['next_flight_late_min']} min late" if leg else ""), self.now,
                        [_opt(f"recover-{f.aircraft_id}", f"recover {f.aircraft_id}", [f.id] + ([leg.id] if leg else []), f.pax)],
                        ["C04"], [f.id], f.pax, sources=self.sources)
            self.propose(("recover", f.aircraft_id), "recover_diverted_aircraft", out_, {"flight_id": f.id, "option": {}}, recheck=False)
            ac = fleet.get(f.aircraft_id) or {}
            if leg and ac.get("maint_due_at"):
                why = self.ctx.run_check("maintenance_due", {"aircraft": {"maint_due_at": ac["maint_due_at"]},
                                                             "flight_end": back + timedelta(minutes=leg.block)})
                if why:
                    item["maintenance"] = why
                    o = _out(f"Maintenance deadline: {f.aircraft_id}", why, self.now, [_opt("flag", why, [leg.id], leg.pax)], ["C02"],
                             [leg.id], leg.pax, sources=self.sources)
                    self.propose(("maint", f.aircraft_id), "flag_maintenance_deadline", o, {"flight_id": leg.id, "option": {}}, recheck=False)
            if leg:
                arr = max(leg.sched_dep, back) + timedelta(minutes=leg.block)
                why = self.ctx.run_check("airport_curfew", {"airport": leg.destination, "operation": "arrival",
                                                            "local_time": arr.astimezone(TZ).strftime("%H:%M")})
                if why:
                    item["curfew"] = why
                    o = _out(f"Curfew risk: {leg.id} to {leg.destination}", why, self.now, [_opt("flag", why, [leg.id], leg.pax)],
                             ["B13"], [leg.id], leg.pax, sources=self.sources)
                    self.propose(("curfew", leg.id), "flag_curfew_risk", o, {"flight_id": leg.id, "option": {}}, recheck=False)
        return out

    def restart_order(self):
        """B14/Q02: when the stop lifts, the order departures should restart in (once per stop)."""
        status = ((self.pic or {}).get("facts") or {}).get("status")
        if status != "recovery" or self.m.stop_started is None or self.m.stop_started in self.m.restart_done:
            return []
        self.m.restart_done.add(self.m.stop_started)
        seats = {r["destination"]: r.get("open_seats_later") or 0 for r in self.static["later_capacity"]}
        waiting = [f for f in self.st.flights.values() if f.direction == "departure" and not f.next_day and f.waiting]
        ranked = sorted(waiting, key=lambda f: (-self.protection(f, seats), f.sched_dep, f.id))
        order = [{"flight_id": f.id, "slot": hhmm(self.now + timedelta(minutes=i * self.s["plan"]["restart_spacing_min"]))}
                 for i, f in enumerate(ranked)]
        if order:
            out = _out("Restart order now the stop has lifted", "highest protection first, one departure every "
                       f"{self.s['plan']['restart_spacing_min']} min", self.now,
                       [_opt("restart", " > ".join(o["flight_id"] for o in order[:20]), [o["flight_id"] for o in order])],
                       ["B14", "Q02"], [o["flight_id"] for o in order], 0, sources=self.sources)
            self.propose(("restart", self.m.stop_started), "set_flight_protection_order", out, {"option": {}}, recheck=False)
        return order


def plan(ctx, memory: Memory | None = None, settings: dict | None = None) -> dict:
    """One run as of ctx.now. Writes a new recovery plan version only if the plan changed. Returns the proposals for the
    (advisory) executor and the plan itself."""
    s = settings or load_settings()
    memory = memory or Memory()
    events = ctx.read_events()
    pic = ctx.read_shared("situation_assessments")
    p = Planner(ctx, memory, s)
    p.station = ctx.read_shared("station_plans") if "station_plans" in (ctx.contract.shared_reads or {}) else None
    from backend.rules.seats import held_by_advice, inventory
    p.inv = inventory(events)
    care = ctx.read_shared("care_plans") if "care_plans" in (ctx.contract.shared_reads or {}) else None
    p.held = held_by_advice(((care or {}).get("facts") or {}).get("rebooking_advice"))
    memory.basis = {"situation_version": (pic or {}).get("version"), "station_plan_version": (p.station or {}).get("version")}
    res = p.run(events, pic)
    warnings = [{"kind": "crew", **{k: w[k] for k in ("flight_id", "role", "illegal_at", "likelihood", "best")}} for w in res["crew"]] + \
               [{"kind": "fuel", **{k: w[k] for k in ("flight_id", "must_leave_by", "likelihood")}} for w in res["fuel"]]
    crew_actions = [{"flight_id": w["flight_id"], "role": w["role"], "best": w["best"],
                     "option": next((o for o in w["options"] if o["legal"]), None)} for w in res["crew"]]
    key = (tuple((d["flight_id"], d["new_departure"][:16]) for d in res["delays"]), tuple(c["flight_id"] for c in res["cancels"]),
           tuple((c["flight_id"], c["role"], c["best"], (c["option"] or {}).get("crew_id")) for c in crew_actions), tuple((w["kind"], w["flight_id"], w["likelihood"]) for w in warnings),
           tuple(a["aircraft_id"] for a in res["aircraft_recovery"]), bool(res["restart_order"]))
    wrote = False
    if key != memory.last_key:
        memory.version += 1
        row = {"version": memory.version, "planned_at": ctx.now, "situation_version": (pic or {}).get("version"),
               "station_plan_version": (p.station or {}).get("version"),
               "delays": res["delays"], "cancels": res["cancels"], "crew_actions": crew_actions, "approval_status": [],
               "hold_divert": res["fuel"], "aircraft_recovery": res["aircraft_recovery"], "restart_order": res["restart_order"],
               "seats_by_destination": res["seats_by_destination"], "warnings": warnings, "estimates": res["estimates"],
               "sources": p.sources, "data_as_of": ctx.now, "decision_ids": sorted({"B02", "B06", "D01"} | (
                   {"B09"} if res["fuel"] else set()))}
        ctx.write("recovery_plans", row)
        ctx.record_event("recovery_plan_drafted", {"version": memory.version, "delays": len(res["delays"]),
                                                   "cancels": len(res["cancels"]), "warnings": len(warnings)})
        memory.plans.append({**row, "planned_at": ctx.now.isoformat(), "data_as_of": ctx.now.isoformat()})
        memory.last_key, wrote = key, True
    ctx.heartbeat({"latest_version": memory.version})
    return {"proposals": p.proposals, "plan": res, "wrote": wrote}
