"""The Station & Staff agent (Phase 4f): the people and the ground at MCI.

Every run (every few minutes and when a trigger event is delivered) it rebuilds, from the events up to T only, which planes are
where and when (arrivals, departures, diversions, tarmac waits), the ramp and the terminal, and what it already has applied (its
own gate and staff confirmations). It reads Situation's picture, Network Recovery's proposed departures and Passenger Care's help
requests (shared state, whitelisted fields). Then:

  a. tarmac care (F01, F02, F05, F06, F07, F08/F11, F09): for every plane waiting on the ground, the tarmac helper's deadlines (status
     announcement by 30 min, food and water by 2 h, the time to start returning before 3 h / 4 h) are met or flagged in time:
     food and water and deplaning are proposed; returning to a gate and the keep-on-board exception are ONLY questions for the
     captain, with the facts;
  b. gates (E03, E10, E11): a gate for every plane arriving within the horizon, a remote stand with stairs when the gates run out,
     and a gate shortage (plus a request to the airport) when even those are gone;
  c. staff (E04, E05, E06): gate agents, ramp crews and customer service to each flight's work, busiest flights first. Every person
     is on shift for the whole piece of work, has a planned break within the break interval and never two jobs at once, and every
     assignment carries the ground-staff limits for the rule check. When short, it proposes starting a later shift early (the
     station manager approves; shift, rest and days-in-a-row limits checked). No ramp work while the ramp is closed; work already
     assigned is paused (E09/F12) and staff safety (E12) is asked of the ramp supervisor;
  d. wheelchair help (E07, J04): a named wheelchair agent at arrival, connection and deplaning for every request in Passenger Care's
     plan, never more than 30 minutes unattended;
  e. a child flying alone (J01/J03): a named staff member supervises until the guardian handoff (station manager);
  f. terminal crowding (G01): a warning and a request to the airport near the capacity share in the rules;
  g. ramp closure and reopening (E01, E02): human only (ramp supervisor). It only asks, with the facts;
  h. one station_plans version (shared, append-only) whenever the plan changes, with the Situation, recovery-plan and care-plan
     versions it used.
It never edits a flight, booking, crew, staff or station record, and never sees a passenger's name.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml

from backend.rules.safety import limits_for
from backend.rules.tarmac import tarmac_timeline
from backend.settings import ROOT, SIM_DATE, TZ

DAY0 = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, tzinfo=TZ)
ROLES = ("gate_agent", "ramp", "customer_service", "wheelchair")
DEPARTURE_ROLES = ("ramp", "gate_agent")
MIN = timedelta(minutes=1)


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "agent_settings" / "station_staff.yaml").read_text(encoding="utf-8"))


def clock(text: str) -> datetime:
    """'13:00' or '06:30+1' on the simulated day (its own copy: agents never import each other's code)."""
    day = 0
    if "+" in text:
        text, plus = text.split("+")
        day = int(plus)
    h, m = text.split(":")
    return DAY0 + timedelta(days=day, hours=int(h), minutes=int(m))


def hhmm(t) -> str:
    return t.astimezone(TZ).strftime("%H:%M") if t else "-"


def _iso(t):
    return t.isoformat() if isinstance(t, datetime) else t


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def _overlap(a1, b1, a2, b2) -> bool:
    return a1 < b2 and a2 < b1


def _mins(td) -> int:
    return int(td.total_seconds() // 60)


@dataclass
class Memory:
    static: dict | None = None
    version: int = 0
    last_key: object = None
    proposed: dict = field(default_factory=dict)      # dedupe key -> recommendation id
    pending: dict = field(default_factory=dict)       # recommendation id -> its facts, until confirmed, blocked or withdrawn
    withdrawn: list = field(default_factory=list)     # recommendation ids it replaced before a person answered (runner withdraws)
    asked: set = field(default_factory=set)
    flagged: dict = field(default_factory=dict)       # (flight, deadline) -> when it was flagged
    plans: list = field(default_factory=list)


def _state(events, station, s):
    """What has happened by T, from the events only."""
    st = {"eta": {}, "etd": {}, "arrived": {}, "departed": {}, "cancelled": {}, "diverted": {}, "pushed": {}, "tarmac": {},
          "ramp": {"open": True, "closed_since": None, "expected_end": None, "closures": []}, "terminal": {},
          "gates": {}, "tasks": {}, "called_in": {}, "paused": [], "decided": {}}
    back = timedelta(minutes=s["diversions"]["flight_back_min"])
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        p, typ, at = e.get("payload") or {}, e["type"], e["occurred_at"]
        fid = p.get("flight_id")
        if typ == "flight_airborne" and fid and p.get("to") == station:
            st["eta"][fid] = ("airborne", p.get("delay_min") or 0)
        elif typ == "flight_holding" and fid and p.get("expected_landing"):
            st["eta"][fid] = ("at", clock(p["expected_landing"]))
        elif typ == "flight_diverted" and fid:
            st["diverted"][fid] = {"to": p.get("diverted_to"), "at": at}
            st["eta"][fid] = ("unknown", None)
        elif typ == "flight_resumed" and fid and p.get("to") == station:
            st["eta"][fid] = ("at", at + back)
        elif typ == "flight_arrived" and fid and e.get("station") == station:
            st["arrived"][fid] = at
        elif typ in ("flight_delayed", "flight_delay_updated") and fid and p.get("new_departure"):
            st["etd"][fid] = clock(p["new_departure"])
        elif typ == "flight_departed" and fid:
            st["departed"][fid] = at
        elif typ == "flight_cancelled" and fid:
            st["cancelled"][fid] = at
        elif typ == "tarmac_started" and fid:
            st["tarmac"][fid] = {"station": e.get("station"), "start": at, "end": None, "diverted": bool(p.get("diverted"))}
            if e.get("station") == station and not p.get("diverted") and not p.get("ramp_closed"):
                st["pushed"][fid] = at                       # a departure that pushed back has left its gate
        elif typ == "tarmac_ended" and fid and fid in st["tarmac"]:
            st["tarmac"][fid]["end"] = at
            st["tarmac"][fid]["how"] = p.get("how")
        elif typ == "ramp_closed":
            end = p.get("expected_end")
            st["ramp"].update(open=False, closed_since=at, reason=p.get("reason"),
                              expected_end=DAY0 + timedelta(minutes=int(end)) if end is not None else None)
        elif typ == "ramp_reopened":
            if st["ramp"]["closed_since"] is not None:
                st["ramp"]["closures"].append((st["ramp"]["closed_since"], at))
            st["ramp"].update(open=True, closed_since=None, expected_end=None, reopened_at=at)
        elif typ == "station_status" and e.get("station") == station:
            st["terminal"] = {"people": p.get("people_in_terminal"), "capacity": p.get("terminal_capacity"), "at": at}
        # its own applied records (the co-pilot's confirmations)
        elif typ == "gate_assigned":
            st["gates"][fid] = {"gate": p["gate"], "remote": bool(p.get("remote_stand")), "rid": p.get("recommendation"),
                                "approved_by": p.get("approved_by"), "from": _dt(p.get("from")), "to": _dt(p.get("to"))}
        elif typ == "gate_released" and st["gates"].get(fid, {}).get("rid") == p.get("recommendation"):
            st["gates"].pop(fid, None)
        elif typ == "staff_assigned":
            st["tasks"].setdefault(p["recommendation"], []).append(
                {"staff_id": p["staff_id"], "role": p["role"], "task": p["task"], "flight_id": fid, "passenger_id": p.get("passenger_id"),
                 "from": _dt(p["from"]), "to": _dt(p["to"]), "rid": p["recommendation"], "approved_by": p.get("approved_by")})
        elif typ == "staff_released":
            rows = st["tasks"].get(p.get("recommendation")) or []
            st["tasks"][p.get("recommendation")] = [r for r in rows if r["staff_id"] != p.get("staff_id")]
        elif typ == "staff_called_in":
            st["called_in"][p["staff_id"]] = {"from": _dt(p["from"]), "to": _dt(p["to"])}
        elif typ == "ramp_tasks_paused":
            st["paused"].append({"from": _dt(p["from"]), "staff_ids": list(p.get("staff_ids") or []), "rid": p.get("recommendation")})
        elif typ == "station_decision_recorded":
            st["decided"][p.get("recommendation")] = p
    return st


class Station:
    def __init__(self, ctx, memory: Memory, s: dict):
        self.ctx, self.m, self.s, self.now = ctx, memory, s, ctx.now
        self.code = s["run"]["station"]
        if memory.static is None:
            flights = {f["id"]: f for f in ctx.read("flights")}
            groups = {}
            for g in ctx.read("passenger_groups"):
                groups.setdefault(g["flight_id"], {})[g["segment"]] = g["count"]
            stations = {x["code"]: x for x in ctx.read("stations")}
            staff = {x["id"]: x for x in ctx.read("staff") if x["station"] == self.code}
            # which arrival brings the aircraft of each departure, and which departure takes it out again (published schedule)
            by_ac = {}
            for f in flights.values():
                if f.get("aircraft_id"):
                    by_ac.setdefault(f["aircraft_id"], []).append(f)
            nxt, prev = {}, {}
            for legs in by_ac.values():
                legs.sort(key=lambda f: f["sched_dep"])
                for i, f in enumerate(legs):
                    if f["destination"] == self.code:
                        later = [g for g in legs[i + 1:] if g["origin"] == self.code]
                        if later:
                            nxt[f["id"]], prev[later[0]["id"]] = later[0]["id"], f["id"]
            memory.static = {"flights": flights, "groups": groups, "stations": stations, "staff": staff, "next": nxt, "prev": prev}
        self.static = memory.static
        self.lim = limits_for(ctx.rules)
        self.proposals = []
        self.warnings = []
        self.sources = [{"fact": "flight, tarmac, ramp and terminal events", "source": "flight_ops / station_ops",
                         "as_of": ctx.feed_last("station_ops").isoformat()},
                        {"fact": "schedule, staff roster and station facts (published before the day)", "source": "station plan",
                         "as_of": DAY0.replace(hour=5).isoformat()}]

    # ------------------------------------------------------------ helpers
    def propose(self, key, action, title, why, decision_ids, facts, options=None, staff_ids=(), flight_id=None, short=None,
                wheel_over=0, breaks=()):
        if key in self.m.proposed:
            return self.m.proposed[key]
        out = {"title": title, "why": why, "data_as_of": self.now.isoformat(),
               "options": options or [{"id": key[0] if isinstance(key, tuple) else str(key), "summary": title}],
               "sources": self.sources, "decision_ids": decision_ids, "station": self.code, "staff_short": dict(short or {}),
               "wheelchair_waiting_over_limit": int(wheel_over), "breaks_due": list(breaks), "staff_ids": list(staff_ids)}
        if flight_id:
            out["flight_id"] = flight_id
        rid = self.ctx.propose(action, out)
        self.m.proposed[key] = rid
        self.m.pending[rid] = {"action": action, **facts}
        self.proposals.append({"agent": "station_staff", "action": action, "decision_ids": decision_ids, "data_as_of": self.now,
                               "id": rid, "flight_id": flight_id, **facts})
        return rid

    def withdraw(self, rid):
        if rid in self.m.pending:
            self.m.pending.pop(rid)
            self.m.withdrawn.append(rid)
            for k in [k for k, v in self.m.proposed.items() if v == rid]:
                self.m.proposed.pop(k)                   # the same work may be proposed again later, as a new recommendation

    def ask(self, key, decision, facts, reason):
        if key not in self.m.asked:
            self.m.asked.add(key)
            self.ctx.ask_human(decision, facts, reason)
            return True
        return False

    def flag(self, key, payload):
        if key not in self.m.flagged:
            self.m.flagged[key] = self.now
            self.ctx.record_event("tarmac_deadline_flagged", payload)

    # ------------------------------------------------------------ the picture of the day
    def times(self):
        """Expected arrival (eta) and departure (etd) at the station for every flight, from the events only."""
        st, F = self.st, self.static["flights"]
        self.eta, self.etd = {}, {}
        for fid, f in F.items():
            if f["destination"] == self.code:
                kind, v = st["eta"].get(fid, ("sched", None))
                self.eta[fid] = (st["arrived"].get(fid) or (f["sched_arr"] + timedelta(minutes=v) if kind == "airborne" else
                                 v if kind == "at" else None if kind == "unknown" else f["sched_arr"]))
                if fid in st["cancelled"]:
                    self.eta[fid] = None
            if f["origin"] == self.code:
                t = st["etd"].get(fid) or f["sched_dep"]
                if t < f["sched_dep"] - timedelta(hours=6):
                    t += timedelta(days=1)
                self.etd[fid] = st["departed"].get(fid) or t

    def gate_until(self, arr_id):
        """Until when the plane of this arrival holds a gate: its next departure from the station, or the end of the day."""
        st = self.st
        dep = self.static["next"].get(arr_id)
        buf = timedelta(minutes=self.s["gates"]["buffer_min"])
        if dep is None or dep in st["cancelled"]:
            return DAY0 + timedelta(hours=24)
        left = st["departed"].get(dep) or st["pushed"].get(dep)
        if left:
            return left + buf
        return max(self.etd.get(dep) or DAY0 + timedelta(hours=24), (self.eta.get(arr_id) or self.now) + timedelta(minutes=30)) + buf

    def occupancy(self):
        """Every plane that holds (or will hold) a gate today: [from, to) and its gate if known."""
        st, F = self.st, self.static["flights"]
        occ = []
        buf = timedelta(minutes=self.s["gates"]["buffer_min"])
        firsts = sorted((f for f in F.values() if f["origin"] == self.code and f["id"] not in self.static["prev"]),
                        key=lambda f: (f["sched_dep"], f["id"]))
        n = self.s["gates"]["southwest_gates"]
        for i, f in enumerate(firsts):                    # overnight planes: the published gate plan (assumption)
            if f["id"] in st["cancelled"] and f["id"] not in st["departed"]:
                end = DAY0 + timedelta(hours=24)
            else:
                end = (st["departed"].get(f["id"]) or st["pushed"].get(f["id"]) or self.etd[f["id"]]) + buf
            occ.append({"flight_id": f["id"], "gate": f"G{(i % n) + 1:02d}" if i < n else None, "from": DAY0, "to": end,
                        "kind": "overnight", "remote": False})
        for fid, f in F.items():
            if f["destination"] != self.code or self.eta.get(fid) is None:
                continue
            # the newest plan first: a replacement waiting for approval, else the gate applied (5b)
            g = next(({"gate": p["gate"], "remote": p.get("remote"), "pending": rid} for rid, p in self.m.pending.items()
                      if p.get("kind") == "gate" and p.get("flight_id") == fid), None) or st["gates"].get(fid)
            occ.append({"flight_id": fid, "gate": (g or {}).get("gate"), "remote": bool((g or {}).get("remote")),
                        "from": self.eta[fid] - timedelta(minutes=5), "to": self.gate_until(fid), "kind": "arrival",
                        "status": "pending" if g and g.get("pending") else "assigned" if fid in st["gates"] else "none"})
        return occ

    # ------------------------------------------------------------ the run
    def run(self, events, sit, rec, care):
        self.st = _state(events, self.code, self.s)
        self.sit, self.rec, self.care = sit, rec, care
        self.times()
        self.resolve_pending()
        self.ramp_closure()
        gates = self.gates()
        wheel = self.wheelchair()                         # vulnerable passengers get their staff first
        child = self.children()
        tasks = self.flight_work()
        shortages = self.shortages(tasks)
        self.call_in(shortages)
        tarmac = self.tarmac(gates)
        terminal = self.terminal()
        return {"gates": gates, "tasks": tasks, "wheelchair": wheel, "child": child, "staff_shortages": shortages,
                "tarmac": tarmac, "terminal": terminal}

    def resolve_pending(self):
        """A pending proposal is done once its confirmation (or a block) is on record."""
        for rid in list(self.m.pending):
            if rid in self.st["decided"]:
                self.m.pending.pop(rid)

    # ------------------------------------------------------------ staff availability
    def shift(self, sid):
        p = self.static["staff"][sid]
        c = self.st["called_in"].get(sid)
        return (c["from"] if c else p["shift_start"]), p["shift_end"]

    def breaks(self, sid):
        """Planned breaks: the latest moment within the break interval, then again (the plan; never later than the rule)."""
        g = self.lim["ground_staff"]
        start, end = self.shift(sid)
        last = self.static["staff"][sid].get("last_break") or start
        out, t = [], last
        lead = timedelta(minutes=self.s["staff"]["break_plan_lead_min"])
        length = timedelta(minutes=g["break_length_min"])
        while True:
            b = t + timedelta(minutes=g["break_interval_min"]) - lead
            if b + length >= end:
                break
            out.append((b, b + length))
            t = b + length
        return out

    def busy(self, exclude=()):
        """staff id -> [(from, to)] for work applied or proposed (not yet answered); `exclude`: assignments being re-planned."""
        out = {}
        for rid, rows in self.st["tasks"].items():
            if rid in exclude:
                continue
            for r in rows:
                out.setdefault(r["staff_id"], []).append((r["from"], r["to"]))
        for rid, p in self.m.pending.items():
            if rid in exclude:
                continue
            for t in p.get("tasks", []):
                out.setdefault(t["staff_id"], []).append((_dt(t["from"]), _dt(t["to"])))
        return out

    def limits_facts(self, sid, a, b):
        """The ground-staff facts the rule check needs for one person doing work from a to b."""
        start, end = self.shift(sid)
        R = self.s["staff"]["roster"]
        last = max([x[1] for x in self.breaks(sid) if x[1] <= a] + [self.static["staff"][sid].get("last_break") or start])
        gap = R["rest_before_today_min"]
        return {"shift_min": _mins(end - start), "min_since_break": _mins(b - last),
                "planned_break_min": self.lim["ground_staff"]["break_length_min"], "gap_min": gap,
                "consecutive_days": R["days_in_a_row"]}

    def free(self, role, a, b, n, busy, avoid=()):
        """Up to n people of this role on shift for all of [a, b], not on a break, not on other work, within every limit."""
        g = self.lim["ground_staff"]
        out = []
        load = {sid: sum(_mins(y - x) for x, y in busy.get(sid, [])) for sid in self.static["staff"]}
        for sid in sorted(self.static["staff"], key=lambda x: (load[x], x)):
            p = self.static["staff"][sid]
            if p["role"] != role or sid in avoid:
                continue
            start, end = self.shift(sid)
            if not (start <= a and b <= end):
                continue
            if any(_overlap(a, b, x, y) for x, y in self.breaks(sid) + busy.get(sid, [])):
                continue
            f = self.limits_facts(sid, a, b)
            if f["shift_min"] > g["max_shift_min"] or f["min_since_break"] > g["break_interval_min"]:
                continue
            out.append(sid)
            if len(out) == n:
                break
        return out

    def staff_option(self, work, needs_ramp=False):
        """The facts the rule check reads: the worst case of the people named, each over their own piece of work
        (work = [(staff id, from, to)]), and the ramp if any of it is outdoors."""
        facts = [self.limits_facts(sid, x, y) for sid, x, y in work]
        a, b = min(x for _, x, _ in work), max(y for _, _, y in work)
        opt = {}
        if facts:
            opt["staff"] = {"shift_min": max(f["shift_min"] for f in facts), "min_since_break": max(f["min_since_break"] for f in facts),
                            "planned_break_min": min(f["planned_break_min"] for f in facts), "gap_min": min(f["gap_min"] for f in facts),
                            "consecutive_days": max(f["consecutive_days"] for f in facts)}
        if needs_ramp:
            opt.update(needs_ramp=True, ramp_open=self.ramp_open_at(a, b))
        return opt

    def ramp_open_at(self, a, b) -> bool:
        r = self.st["ramp"]
        if r["open"]:
            return True
        end = r["expected_end"]
        return end is not None and a >= end

    def ramp_window(self, a, b):
        """Ramp work can't start while the ramp is closed: it waits for the expected reopening (None: can't plan it yet)."""
        r = self.st["ramp"]
        if r["open"]:
            return a, b
        if r["expected_end"] is None or b <= r["expected_end"]:
            return None
        return max(a, r["expected_end"]), b

    # ------------------------------------------------------------ b. gates
    def gates(self):
        S, st, F = self.s["gates"], self.st, self.static["flights"]
        horizon = self.now + timedelta(minutes=self.s["run"]["horizon_min"])
        occ = self.occupancy()
        names = [f"G{i + 1:02d}" for i in range(S["southwest_gates"])]
        stands = [f"R{i + 1}" for i in range(S["remote_stands"])]
        tol = timedelta(minutes=S["conflict_tolerance_min"])

        def holder(gate, a, b, me):
            return next((o["flight_id"] for o in occ if o["gate"] == gate and o["flight_id"] != me and _overlap(a, b, o["from"], o["to"])
                         and (o["kind"] == "overnight" or o["from"] <= a)), None)

        lock = timedelta(minutes=S["lock_before_arrival_min"])

        def taken_any(gate, a, b, me):
            # anyone at all (earlier or later) holding the gate then: a NEW gate must be free of everybody (5b)
            return any(o["gate"] == gate and o["flight_id"] != me and _overlap(a, b, o["from"], o["to"]) for o in occ)

        def taken(gate, a, b, me):
            # a plane that lands earlier (or was there overnight) keeps its gate; a later one moves
            return any(o["gate"] == gate and o["flight_id"] != me and _overlap(a, b, o["from"], o["to"])
                       and (o["kind"] == "overnight" or o["from"] <= a) for o in occ)

        out = []
        for o in sorted((o for o in occ if o["kind"] == "arrival"), key=lambda o: (o["from"], o["flight_id"])):
            fid = o["flight_id"]
            if fid in st["arrived"] and o["status"] == "assigned":
                out.append({"flight_id": fid, "gate": o["gate"], "from": _iso(o["from"]), "to": _iso(o["to"]), "status": "at gate"})
                continue
            if o["from"] > horizon or fid in st["arrived"]:
                continue
            old = st["gates"].get(fid)
            moved = old is not None and old.get("from") is not None and abs(old["from"] - o["from"]) > tol
            # gate stability (5a): a gate assigned for a plane landing soon changes only for a real conflict
            locked = old is not None and self.eta.get(fid) is not None and self.eta[fid] - self.now <= lock
            if locked and not taken(old["gate"], o["from"], o["to"], fid):
                out.append({"flight_id": fid, "gate": old["gate"], "from": _iso(o["from"]), "to": _iso(o["to"]),
                            "status": "assigned"})
                continue
            late_why = None
            if locked:
                late_why = f"{holder(old['gate'], o['from'], o['to'], fid)} is still at {old['gate']} when {fid} lands"
                if ("late", fid, old["gate"]) not in self.m.asked:
                    self.m.asked.add(("late", fid, old["gate"]))
                    self.ctx.record_event("gate_changed_late", {"flight_id": fid, "gate": old["gate"], "why": late_why,
                                                                "minutes_before_arrival": _mins(self.eta[fid] - self.now)})
            if o["gate"] and not taken(o["gate"], o["from"], o["to"], fid) and not moved:
                out.append({"flight_id": fid, "gate": o["gate"], "from": _iso(o["from"]), "to": _iso(o["to"]),
                            "status": o["status"]})
                continue
            waiting = next((rid for rid, p in self.m.pending.items() if p.get("kind") == "gate" and p.get("flight_id") == fid
                            and not taken(p["gate"], o["from"], o["to"], fid) and abs(_dt(p["from"]) - o["from"]) <= tol), None)
            if waiting is not None:
                p = self.m.pending[waiting]
                out.append({"flight_id": fid, "gate": p["gate"], "from": _iso(o["from"]), "to": _iso(o["to"]), "status": "pending"})
                continue
            keep = old["gate"] if moved and not taken(old["gate"], o["from"], o["to"], fid) else None
            pick = keep or next((g for g in names if not taken_any(g, o["from"], o["to"], fid)), None) or \
                next((g for g in names if not taken(g, o["from"], o["to"], fid)), None)
            remote = False
            if pick is None and self.ramp_open_at(o["from"], o["to"]):
                pick, remote = next((g for g in stands if not taken(g, o["from"], o["to"], fid)), None), True
            if pick is None:
                need = sum(1 for x in occ if _overlap(o["from"], o["from"] + MIN, x["from"], x["to"]))
                short = {"from": _iso(o["from"]), "to": _iso(o["from"] + tol), "needed": need, "gates": len(names) + len(stands),
                         "short_by": max(1, need - len(names) - len(stands)), "flights": [fid]}
                self.warnings.append({"kind": "gate_shortage", **short})
                self.ctx.record_event("gate_shortage_found", short) if ("gshort", fid) not in self.m.asked else None
                self.m.asked.add(("gshort", fid))
                self.propose(("airport", fid), "request_airport_resources", f"Ask the airport for a gate or hardstand for {fid}",
                             f"no Southwest gate and no remote stand is free at {hhmm(o['from'])}", ["E10", "E11"],
                             {"kind": "request", "flight_id": fid}, flight_id=fid)
                out.append({"flight_id": fid, "gate": None, "from": _iso(o["from"]), "to": _iso(o["to"]), "status": "no gate"})
                continue
            for rid, p in list(self.m.pending.items()):       # a pending gate that no longer fits is replaced
                if p.get("kind") == "gate" and p.get("flight_id") == fid:
                    self.withdraw(rid)
            o["gate"], o["remote"] = pick, remote
            stairs = {"stairs_available": True, "ramp_open": True} if remote else {}
            rid = self.propose(("gate", fid, pick, hhmm(o["from"])), "assign_gates",
                               f"{'Remote stand' if remote else 'Gate'} {pick} for {fid} ({hhmm(o['from'] + timedelta(minutes=5))})",
                               ("all Southwest gates are taken then: a remote stand with stairs" if remote else
                                "free from the plane's arrival until its next departure") +
                               ((f"; the same gate for its new time (was {hhmm(old['from'] + timedelta(minutes=5))})"
                                 if pick == old["gate"] else f"; replaces {old['gate']}, which is taken then") if old else "")
                               + (f"; late change, a real conflict: {late_why}" if late_why else ""),
                               ["E10" if remote or old else "E03"],
                               {"kind": "gate", "flight_id": fid, "gate": pick, "remote": remote, "from": _iso(o["from"]),
                                "to": _iso(o["to"]), "replaces": (old or {}).get("rid"),
                                "option": {"station": {"gates_free": 1}, **stairs}}, flight_id=fid)
            out.append({"flight_id": fid, "gate": pick, "from": _iso(o["from"]), "to": _iso(o["to"]),
                        "status": "pending" if rid in self.m.pending else "assigned"})
        self.occ = occ
        return out

    def gate_free_now(self):
        n = self.s["gates"]["southwest_gates"]
        used = {o["gate"] for o in self.occ if o["gate"] and o["from"] <= self.now < o["to"]}
        return n - len([g for g in used if g.startswith("G")])

    # ------------------------------------------------------------ c. staff for each flight's work
    def demands(self):
        """Each flight's work in the next horizon: (key, flight, phase, role, count, from, to, pax)."""
        st, F, S = self.st, self.static["flights"], self.s["staff"]
        out = []
        for fid, f in F.items():
            pax = f.get("pax_total") or 0
            if f["destination"] == self.code and self.eta.get(fid) is not None:
                t = self.eta[fid]
                for role, w in S["arrival"].items():
                    out.append((fid, "arrival", role, w["count"], t - timedelta(minutes=w["before_min"]), t + timedelta(minutes=w["after_min"]), pax))
                if fid in st["diverted"]:
                    for role, w in S["diverted_arrival"].items():
                        out.append((fid, "diverted_arrival", role, w["count"], t - timedelta(minutes=w["before_min"]),
                                    t + timedelta(minutes=w["after_min"]), pax))
            if f["origin"] == self.code:
                if fid in st["cancelled"]:
                    c = st["cancelled"][fid]
                    for role, w in S["cancelled"].items():
                        out.append((fid, "cancelled", role, w["count"], c, c + timedelta(minutes=w["after_min"]), pax))
                elif fid not in st["departed"] and fid not in st["pushed"]:
                    t = self.etd[fid]
                    for role, w in S["departure"].items():
                        out.append((fid, "departure", role, w["count"], t - timedelta(minutes=w["before_min"]), t + timedelta(minutes=w["after_min"]), pax))
        return out

    def assigned_for(self, fid, phase):
        """The applied assignment for one flight's work (recommendation id and its rows), if any."""
        for rid, rows in self.st["tasks"].items():
            if rows and rows[0]["flight_id"] == fid and rows[0]["task"] == phase:
                return rid, rows
        return None, []

    def flight_work(self):
        horizon = self.now + timedelta(minutes=self.s["run"]["horizon_min"])
        move = timedelta(minutes=self.s["staff"]["replan_after_move_min"])
        groups = {}
        for d in self.demands():
            groups.setdefault((d[0], d[1]), []).append(d)
        out = []
        # busiest flights first (most passengers), then the earliest work
        for (fid, phase), ds in sorted(groups.items(), key=lambda kv: (-kv[1][0][6], min(d[4] for d in kv[1]), kv[0])):
            a0, b0 = min(d[4] for d in ds), max(d[5] for d in ds)
            if b0 <= self.now or a0 > horizon:
                continue
            old_rid, old_rows = self.assigned_for(fid, phase)
            pend = next((rid for rid, p in self.m.pending.items() if p.get("kind") == "staff" and p.get("flight_id") == fid
                         and p.get("task") == phase), None)
            ref = (self.m.pending[pend]["tasks"][0] if pend and self.m.pending[pend]["tasks"] else None) or \
                (old_rows[0] if old_rows else None)
            if phase == "cancelled" and old_rid is None and pend is None:
                self.release_flight(fid)
            if ref is not None:
                start_now = max(a0, self.now)
                planned = _dt(ref["from"])
                if abs(planned - start_now) <= move or (planned <= self.now and a0 <= self.now):
                    out.append({"flight_id": fid, "task": phase, "status": "pending" if pend else "assigned",
                                "from": _iso(planned), "staff": sorted({r["staff_id"] for r in (self.m.pending[pend]["tasks"] if pend
                                                                                                 else old_rows)})})
                    continue
                if pend is not None:
                    self.withdraw(pend)
            busy = self.busy(exclude=(old_rid,) if old_rid else ())
            rows, short = [], {}
            for (_, _, role, n, a, b, _) in ds:
                a = max(a, self.now)
                if b <= a:
                    continue
                if role == "ramp":
                    w = self.ramp_window(a, b)
                    if w is None:
                        short["ramp (ramp closed)"] = n
                        continue
                    a, b = w
                sids = self.free(role, a, b, n, busy)
                for sid in sids:
                    rows.append({"staff_id": sid, "role": role, "task": phase, "flight_id": fid, "passenger_id": None,
                                 "from": _iso(a), "to": _iso(b)})
                    busy.setdefault(sid, []).append((a, b))
                if len(sids) < n:
                    short[role] = n - len(sids)
            if not rows:
                out.append({"flight_id": fid, "task": phase, "status": "no staff free", "short": short})
                continue
            sids = [r["staff_id"] for r in rows]
            a, b = min(_dt(r["from"]) for r in rows), max(_dt(r["to"]) for r in rows)
            ramp = any(r["role"] == "ramp" for r in rows)
            opt = self.staff_option([(r["staff_id"], _dt(r["from"]), _dt(r["to"])) for r in rows], needs_ramp=ramp)
            rid = self.propose(("staff", fid, phase, hhmm(a)), "move_gate_agents",
                               f"Staff for {fid} {phase.replace('_', ' ')} from {hhmm(a)}",
                               f"{len(sids)} people ({', '.join(sorted({r['role'] for r in rows}))}); busiest flights first"
                               + (f"; short: {short}" if short else "") + ("; replaces an earlier plan (times moved)" if old_rid else ""),
                               ["E04"] + (["E05"] if opt.get("staff", {}).get("min_since_break", 0) >
                                          self.lim["ground_staff"]["break_interval_min"] - 60 else []),
                               {"kind": "staff", "flight_id": fid, "task": phase, "tasks": rows, "replaces": old_rid, "option": opt},
                               staff_ids=sids, flight_id=fid, short=short)
            out.append({"flight_id": fid, "task": phase, "status": "pending", "from": _iso(a), "staff": sorted(set(sids)),
                        "short": short, "recommendation": rid})
        return out

    def release_flight(self, fid):
        """A cancelled departure no longer needs its boarding and ramp staff: give them back (station manager)."""
        rid, rows = self.assigned_for(fid, "departure")
        if rid is None or not rows:
            return
        self.propose(("release", fid, rid), "move_gate_agents", f"Release the staff of cancelled {fid}",
                     f"{len(rows)} people free for other flights", ["E04"],
                     {"kind": "release", "flight_id": fid, "replaces": rid, "tasks": [], "option": {}},
                     staff_ids=[r["staff_id"] for r in rows], flight_id=fid)

    # ------------------------------------------------------------ d. wheelchair help
    def help_steps(self, itinerary):
        """The steps a passenger needs help at the station: arrival (meet at the door, deplane) and boarding a connection."""
        W, F = self.s["staff"]["wheelchair"], self.static["flights"]
        steps = []
        for fid in itinerary or []:
            f = F.get(fid)
            if not f:
                continue
            if f["destination"] == self.code and self.eta.get(fid) is not None:
                steps.append(("arrival", fid, self.eta[fid] - timedelta(minutes=W["meet_before_arrival_min"]),
                              self.eta[fid] + timedelta(minutes=W["help_after_arrival_min"])))
            if f["origin"] == self.code and fid not in self.st["cancelled"] and fid not in self.st["departed"]:
                steps.append(("boarding", fid, self.etd[fid] - timedelta(minutes=W["board_before_departure_min"]), self.etd[fid]))
        steps.sort(key=lambda x: x[2])
        merged = []
        for s in steps:          # a connection: the same agent stays from the door to the next gate (never left alone in between)
            if merged:
                last = merged[-1]
                merged[-1] = ("connection", last[1] + ">" + s[1], last[2], max(last[3], s[3]))
                continue
            merged.append(s)
        return merged

    def wheelchair(self):
        reqs = (((self.care or {}).get("facts") or {}).get("wheelchair_requests")) or []
        horizon = self.now + timedelta(minutes=self.s["run"]["horizon_min"])
        move = timedelta(minutes=self.s["staff"]["replan_after_move_min"])
        out = []
        for r in reqs:
            pid = r["passenger_id"]
            itin = r.get("itinerary") or ([r["flight_id"]] if r.get("flight_id") else [])
            for phase, fid, a, b in self.help_steps(itin):
                entry = {"passenger_id": pid, "flight_id": fid, "step": phase, "from": _iso(a), "to": _iso(b),
                         "max_unattended_min": r.get("max_unattended_min")}
                if b <= self.now:
                    entry["status"] = "done"
                    out.append(entry)
                    continue
                if a > horizon:
                    entry["status"] = "planned later"
                    out.append(entry)
                    continue
                key_rows = [(rid, rows) for rid, rows in self.st["tasks"].items() if rows and rows[0]["passenger_id"] == pid
                            and rows[0]["task"] == f"wheelchair {phase}"]
                old_rid, old_rows = key_rows[-1] if key_rows else (None, [])
                pend = next((rid for rid, p in self.m.pending.items() if p.get("kind") == "wheelchair" and p.get("passenger_id") == pid
                             and p.get("task") == f"wheelchair {phase}"), None)
                ref = self.m.pending[pend]["tasks"][0] if pend else (old_rows[0] if old_rows else None)
                start_now = max(a, self.now)
                if ref is not None and (abs(_dt(ref["from"]) - start_now) <= move or _dt(ref["from"]) <= self.now) \
                        and _dt(ref["to"]) >= b - move:
                    entry.update(status="requested" if pend else "covered", staff_id=ref["staff_id"], covered_from=_iso(ref["from"]),
                                 covered_to=_iso(ref["to"]))
                    out.append(entry)
                    continue
                if pend is not None:
                    self.withdraw(pend)
                busy = self.busy(exclude=(old_rid,) if old_rid else ())
                sid = self.free("wheelchair", start_now, b, 1, busy)
                if not sid:
                    entry["status"] = "no wheelchair agent free"
                    self.warnings.append({"kind": "wheelchair", "passenger_id": pid, "flight_id": fid, "from": _iso(a)})
                    if ("wwarn", pid, phase, hhmm(a)) not in self.m.asked:
                        self.m.asked.add(("wwarn", pid, phase, hhmm(a)))
                        self.ctx.record_event("wheelchair_wait_warning", {"passenger_id": pid, "flight_id": fid, "from": _iso(a),
                                                                          "why": "no wheelchair agent free for the whole step"})
                    out.append(entry)
                    continue
                row = {"staff_id": sid[0], "role": "wheelchair", "task": f"wheelchair {phase}", "flight_id": fid, "passenger_id": pid,
                       "from": _iso(start_now), "to": _iso(b)}
                opt = {**self.staff_option([(sid[0], start_now, b)]), "pax": {"wheelchair": 1, "wheelchair_unattended_min": 0},
                       "station": {"wheelchair_staff": 1}}
                self.propose(("wheel", pid, phase, hhmm(start_now)), "request_wheelchair_help",
                             f"Wheelchair agent {sid[0]} for {pid} ({phase}, {fid}) from {hhmm(start_now)}",
                             "help at every step; never more than 30 minutes unattended (Passenger Care's request)",
                             ["E07", "J04"], {"kind": "wheelchair", "passenger_id": pid, "flight_id": fid, "task": f"wheelchair {phase}",
                                              "tasks": [row], "replaces": old_rid, "option": opt},
                             staff_ids=sid, flight_id=fid)
                entry.update(status="requested", staff_id=sid[0], covered_from=_iso(start_now), covered_to=_iso(b))
                out.append(entry)
        return out

    # ------------------------------------------------------------ e. a child flying alone
    def children(self):
        kids = [v for v in ((((self.care or {}).get("facts") or {}).get("vulnerable")) or []) if v.get("kind") == "child_alone"]
        C = self.s["staff"]["child"]
        move = timedelta(minutes=self.s["staff"]["replan_after_move_min"])
        out = []
        for v in kids:
            pid = v["passenger_id"]
            itin = v.get("itinerary") or []
            arr = next((fid for fid in itin if self.static["flights"].get(fid, {}).get("destination") == self.code), None)
            dep = next((fid for fid in itin if self.static["flights"].get(fid, {}).get("origin") == self.code), None)
            if arr and self.eta.get(arr) is not None:
                a = self.eta[arr] - timedelta(minutes=C["meet_before_arrival_min"])
                b = self.eta[arr] + timedelta(minutes=C["guardian_handoff_within_min"])
                fid = arr
            elif dep:
                a, b, fid = self.now, self.now + timedelta(minutes=C["guardian_handoff_within_min"]), dep
            else:
                held = [r for rows in self.st["tasks"].values() for r in rows if r["passenger_id"] == pid
                        and r["task"] == "child supervision"]
                out.append({"passenger_id": pid, "status": "supervisor assigned; waiting for the new arrival time" if held
                            else "waiting for the flight time", "staff_id": held[-1]["staff_id"] if held else None,
                            "approved_by": held[-1]["approved_by"] if held else None})
                continue
            entry = {"passenger_id": pid, "flight_id": fid, "from": _iso(a), "to": _iso(b), "care_decision": v.get("decision")}
            rows = [(rid, r) for rid, r in self.st["tasks"].items() if r and r[0]["passenger_id"] == pid and r[0]["task"] == "child supervision"]
            old_rid, old_rows = rows[-1] if rows else (None, [])
            pend = next((rid for rid, p in self.m.pending.items() if p.get("kind") == "child" and p.get("passenger_id") == pid), None)
            ref = self.m.pending[pend]["tasks"][0] if pend else (old_rows[0] if old_rows else None)
            start_now = max(a, self.now)
            if b <= self.now:
                entry["status"] = "handed over (window passed)"
                out.append({**entry, "staff_id": ref["staff_id"] if ref else None})
                continue
            if ref is not None and (abs(_dt(ref["from"]) - start_now) <= move or _dt(ref["from"]) <= self.now) and _dt(ref["to"]) >= b - move:
                entry.update(status="waiting for the station manager" if pend else "supervisor assigned", staff_id=ref["staff_id"],
                             approved_by=ref.get("approved_by"))
                out.append(entry)
                continue
            if pend is not None:
                self.withdraw(pend)
            busy = self.busy(exclude=(old_rid,) if old_rid else ())
            sid, role = [], None
            for role in C["supervisor_roles"]:
                sid = self.free(role, start_now, b, 1, busy)
                if sid:
                    break
            if not sid:
                entry["status"] = "no staff member free"
                self.warnings.append({"kind": "child_alone", "passenger_id": pid, "from": _iso(a)})
                out.append(entry)
                continue
            plan = f"staff member {sid[0]} supervises from {hhmm(start_now)} until the guardian handoff"
            row = {"staff_id": sid[0], "role": role, "task": "child supervision", "flight_id": fid, "passenger_id": pid,
                   "from": _iso(start_now), "to": _iso(b)}
            opt = {**self.staff_option([(sid[0], start_now, b)]), "pax": {"child_alone": 1}, "child_alone_plan": plan, "staff_supervised": True,
                   "destination_kind": "staff_supervision"}
            self.propose(("child", pid, hhmm(start_now)), "supervise_child_alone", f"Staff supervisor for a child flying alone ({pid})",
                         plan + " (Passenger Care's request)", ["J01"], {"kind": "child", "passenger_id": pid, "flight_id": fid,
                                                                         "task": "child supervision", "tasks": [row], "replaces": old_rid,
                                                                         "care_decision": v.get("decision"), "option": opt},
                         staff_ids=sid, flight_id=fid)
            entry.update(status="waiting for the station manager", staff_id=sid[0])
            out.append(entry)
        return out

    # ------------------------------------------------------------ staff shortages and calling people in
    def shortages(self, tasks):
        """Per role, 15-minute slots in the next horizon where the work needs more people than are on shift and not on a break."""
        G = self.static["groups"]
        W = self.s["staff"]["wheelchair"]
        demand_k = [(d[2], d[3], d[4], d[5], d[1]) for d in self.demands()]
        demand = [x[:4] for x in demand_k]
        for fid, f in self.static["flights"].items():               # every booked wheelchair passenger (counts only)
            n = (G.get(fid) or {}).get("wheelchair", 0)
            if not n:
                continue
            if f["destination"] == self.code and self.eta.get(fid) is not None and fid not in self.st["arrived"]:
                a, b = (self.eta[fid] - timedelta(minutes=W["meet_before_arrival_min"]),
                        self.eta[fid] + timedelta(minutes=W["help_after_arrival_min"]))
            elif f["origin"] == self.code and fid not in self.st["cancelled"] and fid not in self.st["departed"]:
                a, b = self.etd[fid] - timedelta(minutes=W["board_before_departure_min"]), self.etd[fid]
            else:
                continue
            agents = -(-n * W["agent_min_per_passenger"] // max(1, _mins(b - a)))          # agents busy over the window
            demand.append(("wheelchair", agents, a, b))
        slots = []
        t = self.now.replace(second=0, microsecond=0)
        t -= timedelta(minutes=t.minute % 15)
        end = self.now + timedelta(minutes=self.s["run"]["horizon_min"])
        while t < end:
            slots.append((t, t + timedelta(minutes=15)))
            t += timedelta(minutes=15)
        out = []
        for role in ROLES:
            people = [sid for sid, p in self.static["staff"].items() if p["role"] == role]
            for a, b in slots:
                need = sum(n for r, n, x, y in demand if r == role and _overlap(a, b, x, y))
                if role == "ramp" and not self.ramp_open_at(a, b):
                    need = 0                                       # no ramp work while the ramp is closed
                if not need:
                    continue
                on = sum(1 for sid in people if self.shift(sid)[0] <= a and b <= self.shift(sid)[1]
                         and not any(_overlap(a, b, x, y) for x, y in self.breaks(sid)))
                if need > on:
                    entry = {"role": role, "from": _iso(a), "to": _iso(b), "needed": need, "on_duty": on, "short_by": need - on}
                    if role in DEPARTURE_ROLES:            # after the arrivals' work: how many departures can still be worked here
                        arr = sum(n for r, n, x, y, k in demand_k if r == role and k == "arrival" and _overlap(a, b, x, y))
                        per = self.s["staff"]["departure"][role]
                        crews = max(0, on - arr) // per["count"]
                        entry["departures_workable"] = int(crews * 15 // max(15, per["before_min"] + per["after_min"]))
                    out.append(entry)
        for s_ in out:
            if ("gap", s_["role"], s_["from"]) not in self.m.asked:
                self.m.asked.add(("gap", s_["role"], s_["from"]))
                self.ctx.record_event("staffing_gap_found", s_)
        return out

    def call_in(self, shortages):
        """E06: start a later shift early for the role and window that is short (station manager). Every limit is checked."""
        g = self.lim["ground_staff"]
        early = timedelta(minutes=self.s["staff"]["call_in"]["start_early_max_min"])
        for s_ in shortages:
            a, b = _dt(s_["from"]), _dt(s_["to"])
            if a > self.now + timedelta(minutes=self.s["run"]["horizon_min"]):
                continue
            already = [p for p in self.m.pending.values() if p.get("kind") == "call_in" and p.get("role") == s_["role"]]
            done = [sid for sid in self.st["called_in"] if self.static["staff"][sid]["role"] == s_["role"]]
            want = s_["short_by"] - len(already)
            if want <= 0:
                continue
            for sid, p in sorted(self.static["staff"].items(), key=lambda kv: (kv[1]["shift_start"], kv[0])):
                if want <= 0:
                    break
                if p["role"] != s_["role"] or sid in done or any(x.get("staff_id") == sid for x in self.m.pending.values()):
                    continue
                start, end = p["shift_start"], p["shift_end"]
                if start <= a or start - a > early or start > b:
                    continue                                       # on shift already, or would start too early
                new_start = max(a, self.now)
                shift_min = _mins(end - new_start)
                if shift_min > g["max_shift_min"]:
                    continue
                facts = {"shift_min": shift_min, "min_since_break": 0, "planned_break_min": g["break_length_min"],
                         "gap_min": self.s["staff"]["roster"]["rest_before_today_min"],
                         "consecutive_days": self.s["staff"]["roster"]["days_in_a_row"]}
                self.propose(("callin", sid, hhmm(new_start)), "call_in_ground_staff",
                             f"Call in {sid} ({p['role']}) at {hhmm(new_start)} instead of {hhmm(start)}",
                             f"{s_['role']} short by {s_['short_by']} from {hhmm(a)} to {hhmm(b)}; shift becomes {shift_min} min",
                             ["E06"], {"kind": "call_in", "staff_id": sid, "role": p["role"], "from": _iso(new_start), "to": _iso(end),
                                       "option": {"staff": facts}}, staff_ids=[sid], short={s_["role"]: s_["short_by"]})
                want -= 1

    # ------------------------------------------------------------ ramp closure (pause work; ask, never decide)
    def ramp_closure(self):
        r, S = self.st["ramp"], self.s["ramp"]
        sit = (self.sit or {})
        status, kind = (sit.get("facts") or {}).get("status"), ((sit.get("suspected") or {}).get("type") or {}).get("value")
        if r["open"] and status == "abnormal" and kind in S["ask_close_when"]:
            self.ask(("E01", kind), "E01",
                     {"situation_version": sit.get("version"), "type": kind, "ramp_open": True,
                      "lightning_distance": "not in the co-pilot's feeds"},
                     "Thunderstorms are reported. Closing the ramp for lightning is the ramp supervisor's decision.")
        if r["open"]:
            return
        since, end = r["closed_since"], r["expected_end"]
        # E09/F12: ramp work already assigned that falls in the closure stops (automatic); E12 is the ramp supervisor's call
        hit = sorted({row["staff_id"] for rows in self.st["tasks"].values() for row in rows if row["role"] == "ramp"
                      and _overlap(max(row["from"], since), row["to"], since, end or since + timedelta(hours=24))})
        if hit:
            self.propose(("pause", hhmm(since)), "flag_ramp_service_impact", f"Pause ramp work: ramp closed for lightning at {hhmm(since)}",
                         f"{len(hit)} ramp staff stop outdoor work until the ramp reopens"
                         + (f" (expected {hhmm(end)})" if end else ""), ["E09", "F12"],
                         {"kind": "pause", "from": _iso(since), "to": _iso(end), "staff_ids": hit, "option": {}}, staff_ids=hit)
        for rid, p in list(self.m.pending.items()):              # pending ramp work inside the closure is withdrawn
            if any(t["role"] == "ramp" and _overlap(_dt(t["from"]), _dt(t["to"]), since, end or since + timedelta(hours=24))
                   for t in p.get("tasks", [])):
                self.withdraw(rid)
        self.ask(("E12", hhmm(since)), "E12", {"ramp_closed_since": _iso(since), "reason": r.get("reason"),
                                              "ramp_staff_paused": hit}, "Stopping outdoor work for staff safety is the ramp supervisor's call.")
        if end and self.now >= end - timedelta(minutes=S["ask_reopen_before_min"]):
            self.ask(("E02", hhmm(since)), "E02", {"closed_since": _iso(since), "expected_end": _iso(end), "reason": r.get("reason")},
                     "Reopening the ramp is the ramp supervisor's decision; the co-pilot only lists the facts.")

    # ------------------------------------------------------------ a. tarmac care
    def tarmac(self, gates):
        st, T = self.st, self.s["tarmac"]
        G, F = self.static["groups"], self.static["flights"]
        out = []
        wheel_free = len(self.free("wheelchair", self.now, self.now + timedelta(minutes=30), 99, self.busy()))
        staff_free = len(self.free("gate_agent", self.now, self.now + timedelta(minutes=30), 99, self.busy())) > 0
        for fid, t in sorted(st["tarmac"].items()):
            f = F.get(fid) or {}
            at_home = t["station"] == self.code
            kind = "diversion" if t["diverted"] else ("departure" if f.get("origin") == self.code else "arrival")
            start_m = _mins(t["start"] - DAY0)
            now_m = _mins((t["end"] or self.now) - DAY0)
            other = f.get("destination") if f.get("origin") == self.code else f.get("origin")
            intl = (self.static["stations"].get(other) or {}).get("region") == self.s.get("international_region", "Mexico / Caribbean")
            hhmm_now = (t["end"] or self.now).astimezone(TZ).strftime("%H:%M")
            if at_home:
                plane = {"gate_available": self.gate_free_now() > 0, "stairs_available": self.s["gates"]["stairs"] > 0,
                         "staff_available": staff_free, "ramp_open": st["ramp"]["open"],
                         "station_has_customs": (self.static["stations"].get(self.code) or {}).get("has_customs"),
                         "wheelchair_staff": wheel_free}
            else:
                staffed = self.ctx.run_check("station_staffed", {"airport": t["station"], "needs_station_staff": True,
                                                                 "local_time": hhmm_now}) is None
                plane = {"gate_available": False, "stairs_available": False, "staff_available": staffed, "ramp_open": True,
                         "station_has_customs": (self.static["stations"].get(t["station"]) or {}).get("has_customs"),
                         "wheelchair_staff": 0}
            plane.update(kind=kind, door_close=start_m, touchdown=start_m, now=now_m, international=intl,
                         wheelchair=(G.get(fid) or {}).get("wheelchair", 0), child_alone=(G.get(fid) or {}).get("child_alone", 0),
                         child_alone_staff=1 if at_home else 0)
            tl = tarmac_timeline(plane, self.ctx.rules)
            dl = {k: DAY0 + timedelta(minutes=v) for k, v in tl["deadlines"].items()}
            ended = t["end"]
            row = {"flight_id": fid, "station": t["station"], "started": _iso(t["start"]), "ended": _iso(ended),
                   "elapsed_min": tl["elapsed_min"], "kind": kind, "decision": tl["decision"], "blockers": tl["blockers"],
                   "partial_blockers": tl["partial_blockers"], "asked_captain": False, "status": "ended" if ended else "waiting"}
            facts = {"flight_id": fid, "station": t["station"], "tarmac_since": _iso(t["start"]), "elapsed_min": tl["elapsed_min"],
                     "deadlines": {k: _iso(v) for k, v in dl.items()}, "blockers": tl["blockers"], "decision": tl["decision"]}
            # F01: the on-board status announcement by 30 min (the crew announces; Communication tells passengers)
            row["status_announcement"] = self.deadline(fid, "status_announcement", dl, ended, facts,
                                                       "Tell passengers on board the status by 30 minutes (flight crew; Communication)")
            # F02: food and water by 2 h: proposed ahead of the deadline (station manager)
            fw = dl["food_and_water"]
            if not ended and self.now >= fw - timedelta(minutes=T["food_water_lead_min"]) and self.now < dl["limit"]:
                rid = self.propose(("food", fid), "arrange_food_water", f"Food and water for {fid} by {hhmm(fw)}",
                                   f"on the ground since {hhmm(t['start'])} at {t['station']}; the law requires it by 2 hours",
                                   ["F02"], {"kind": "request", "flight_id": fid,
                                             "option": {"tarmac_min": tl["elapsed_min"], "food_water_plan": True}}, flight_id=fid)
                self.m.flagged.setdefault((fid, "food_and_water"), self.now)
            frid = self.m.proposed.get(("food", fid))
            if frid is not None:
                d = st["decided"].get(frid) or {}
                row["food_and_water"] = {"due": _iso(fw), "status": "approved by " + d["approved_by"] if d.get("status") == "approved"
                                         else "proposed", "at": _iso(self.m.flagged.get((fid, "food_and_water"))), "recommendation": frid}
            else:
                row["food_and_water"] = self.deadline(fid, "food_and_water", dl, ended, facts, None)
            # F05 / F08 / F11: offer deplaning when possible; at a diversion airport plan getting off
            if not ended and tl["elapsed_min"] >= (T["offer_deplaning_after_min"] if at_home else T["diversion_deplaning_after_min"]):
                if at_home and tl["can_deplane"]:
                    self.propose(("deplane", fid), "start_deplaning", f"Offer {fid}'s passengers the chance to get off",
                                 "a gate and staff are free; passengers must be told each time deplaning is possible", ["F05"],
                                 {"kind": "request", "flight_id": fid, "option": {"tarmac_min": tl["elapsed_min"], "food_water_plan": True}},
                                 flight_id=fid)
                elif not at_home:
                    self.propose(("divdeplane", fid), "arrange_diversion_deplaning", f"Plan getting {fid}'s passengers off at {t['station']}",
                                 "; ".join(tl["blockers"]) or "staff, stairs, TSA and customs to arrange", ["F08", "F11"],
                                 {"kind": "request", "flight_id": fid, "option": {"airport": t["station"], "needs_station_staff": True,
                                                                                  "local_time": hhmm_now}}, flight_id=fid)
                if plane["wheelchair"]:
                    self.ctx.record_event("deplaning_options_drafted", {"flight_id": fid, "wheelchair": plane["wheelchair"],
                                                                        "wheelchair_staff_free": plane["wheelchair_staff"],
                                                                        "decision_ids": ["F09"]}) \
                        if ("F09", fid) not in self.m.asked else None
                    self.m.asked.add(("F09", fid))
            # F06 / F07: before the time to start returning, ONLY ask the captain (with the facts)
            ret = dl["start_return_by"]
            if not ended and self.now >= ret - timedelta(minutes=T["ask_captain_before_return_min"]):
                self.ask(("F06", fid), "F06", facts, f"{fid} must start returning to a gate by {hhmm(ret)}. Only the captain decides.")
                self.ask(("F07", fid), "F07", {**facts, "exceptions": tl["human_only_exceptions"]},
                         "Keeping people on board past the limit needs a safety or air-traffic exception: only the captain, and logged.")
                self.m.flagged.setdefault((fid, "start_return_by"), self.now)
            asked = self.m.flagged.get((fid, "start_return_by"))
            row["asked_captain"] = bool(asked)
            row["start_return_by"] = {"due": _iso(ret), "status": "asked the captain" if asked else
                                      ("met (left the tarmac)" if ended and ended < ret else "not yet"), "at": _iso(asked)}
            row["limit"] = _iso(dl["limit"])
            out.append(row)
        return out

    def deadline(self, fid, name, dl, ended, facts, text):
        due = dl[name]
        if ended and ended <= due and (fid, name) not in self.m.flagged:
            return {"due": _iso(due), "status": "met (left the tarmac)"}
        if text and (fid, name) not in self.m.flagged and self.now <= due:
            self.flag((fid, name), {**facts, "deadline": name, "due": _iso(due), "what": text, "decision_ids": ["F01"]})
        at = self.m.flagged.get((fid, name))
        return {"due": _iso(due), "status": "flagged" if at else ("not yet" if self.now < due else "missed"), "at": _iso(at)}

    # ------------------------------------------------------------ f. terminal crowding
    def terminal(self):
        t = self.st["terminal"]
        share = self.lim["airports"]["terminal_crowding_alert_share"]
        if not t or not t.get("capacity"):
            return {}
        used = t["people"] / t["capacity"]
        out = {"people": t["people"], "capacity": t["capacity"], "share": round(used, 3), "alert_share": share, "as_of": _iso(t["at"])}
        if used >= share:
            self.warnings.append({"kind": "crowding", **out})
            hour = hhmm(self.now)[:2]
            if ("crowd", hour) not in self.m.asked:
                self.m.asked.add(("crowd", hour))
                self.ctx.record_event("terminal_crowding_warning", out)
            self.propose(("crowd", hour), "request_airport_resources", "Ask the airport for more terminal space",
                         f"{t['people']} people in a terminal for {t['capacity']} ({used:.0%})", ["G01"], {"kind": "request"})
        return out


def plan_station(ctx, memory: Memory | None = None, settings: dict | None = None) -> dict:
    s = settings or load_settings()
    memory = memory or Memory()
    events = ctx.read_events()
    sit = ctx.read_shared("situation_assessments")
    rec = ctx.read_shared("recovery_plans")
    care = ctx.read_shared("care_plans")
    p = Station(ctx, memory, s)
    res = p.run(events, sit, rec, care)
    r = p.st["ramp"]
    ramp = {"open": r["open"], "closed_since": _iso(r["closed_since"]), "expected_reopen": _iso(r["expected_end"]),
            "reason": r.get("reason"), "closures": [[_iso(a), _iso(b)] for a, b in r["closures"]]}
    gate_short = [w for w in p.warnings if w["kind"] == "gate_shortage"]
    gate_shortages = [{k: w[k] for k in ("from", "to", "needed", "gates", "short_by", "flights")} for w in gate_short]
    breaks_due = sorted(sid for sid in p.static["staff"] for a, b in p.breaks(sid) if p.now <= a <= p.now + timedelta(minutes=30))
    for sid in breaks_due:
        if ("break", sid, p.now.date()) not in memory.asked:
            memory.asked.add(("break", sid, p.now.date()))
            ctx.record_event("break_due", {"staff_id": sid, "at": _iso(next(a for a, b in p.breaks(sid) if p.now <= a))})
    coverage = [{k: t.get(k) for k in ("flight_id", "task", "status", "from", "staff", "short")} for t in res["tasks"]]
    breaks = [{"staff_id": sid, "from": _iso(a), "to": _iso(b)} for sid in sorted(p.static["staff"]) for a, b in p.breaks(sid)]
    rec_plan = {"delays": [(d.get("flight_id"), d.get("new_departure")) for d in (((rec or {}).get("facts") or {}).get("delays") or [])]}
    key = (tuple((g["flight_id"], g["gate"], g["status"]) for g in res["gates"]),
           tuple((x["role"], x["from"], x["to"], x["short_by"]) for x in res["staff_shortages"]),
           tuple((t["flight_id"], t["status"], t["asked_captain"], t["food_and_water"]["status"]) for t in res["tarmac"]),
           tuple((w["passenger_id"], w["step"], w["status"], w.get("staff_id")) for w in res["wheelchair"]),
           tuple((c["passenger_id"], c.get("status"), c.get("staff_id")) for c in res["child"]),
           tuple((c["flight_id"], c["task"], c["status"]) for c in coverage), ramp["open"],
           tuple(sorted(w["kind"] for w in p.warnings)), tuple(gate_shortages and [g["from"] for g in gate_shortages]),
           tuple((b["staff_id"], b["from"]) for b in breaks))
    wrote = False
    if key != memory.last_key:
        memory.version += 1
        row = {"version": memory.version, "planned_at": ctx.now, "situation_version": (sit or {}).get("version"),
               "recovery_plan_version": (rec or {}).get("version"), "care_plan_version": (care or {}).get("version"),
               "ramp_status": ramp, "terminal": res["terminal"], "gates": res["gates"], "gate_shortages": gate_shortages,
               "staff_shortages": res["staff_shortages"], "staff_coverage": coverage, "tarmac": res["tarmac"],
               "wheelchair_coverage": res["wheelchair"], "child_coverage": res["child"], "warnings": p.warnings, "breaks": breaks,
               "sources": p.sources, "data_as_of": ctx.now,
               "decision_ids": sorted({"E03", "E04", "E07", "F01", "J04"} | ({"E06"} if res["staff_shortages"] else set())
                                      | ({"J01"} if res["child"] else set()) | ({"F02", "F06"} if res["tarmac"] else set()))}
        ctx.write("station_plans", row)
        ctx.record_event("station_plan_drafted", {"version": memory.version, "gates": len(res["gates"]),
                                                  "staff_shortages": len(res["staff_shortages"]), "tarmac": len(res["tarmac"])})
        memory.plans.append({**row, "planned_at": ctx.now.isoformat(), "data_as_of": ctx.now.isoformat(), "recovery_seen": rec_plan})
        memory.last_key, wrote = key, True
    ctx.heartbeat({"latest_version": memory.version})
    withdrawn, memory.withdrawn = memory.withdrawn, []
    return {"proposals": p.proposals, "plan": res, "withdrawn": withdrawn, "wrote": wrote}
