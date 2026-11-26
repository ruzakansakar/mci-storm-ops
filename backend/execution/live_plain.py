"""Plain language for airport employees (live mode): every queue item and trace in airline words, built only from what the
co-pilot recorded (the proposal, the agent's recommendation, the events it was given, the shared versions). Nothing invented:
a fact that is not on record is left out, never guessed. Southwest's word "Customers" is used throughout.

  headline        the action, e.g. "Keep WN1031 to Phoenix flying today"
  story           2-4 time-stamped sentences of what happened, in order
  recommendation  what the co-pilot suggests
  options         the recommended option first, then real alternatives the agent produced (one option if there is only one)
  effect          Customers, connections and flights touched, in one sentence
  if_declined     one sentence
  says            an agent's own first-person sentence for its trace node; labels: plain names with times
"""
import re
from datetime import datetime

from backend.execution.live_trace import hhmm
from backend.settings import TZ

SNAKE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
VERSION = re.compile(r"\bv\d+\b")
PLAN_NAMES = {"situation": "Weather update", "network_recovery": "Crew and flight plan", "passenger_care": "Customer care plan",
              "station_staff": "Station plan", "communication": "Message plan", "orchestrator": "Orchestrator"}
ROLE_NAMES = {"network_operations_control": "network operations control", "crew_scheduling": "crew scheduling",
              "customer_service_supervisor": "the customer service supervisor", "station_manager": "the station manager",
              "ramp_supervisor": "the ramp supervisor", "director_of_operations": "the duty manager", "captain": "the captain",
              "maintenance_control": "maintenance control"}
EVENTS = {   # what an event says, in plain words ({flight}, {time}, {end}, {to}, {reason} filled from its payload)
    "ground_stop_started": "The FAA stopped departures at MCI{end_text}.",
    "ground_stop_extended": "The FAA extended the ground stop{end_text}.",
    "ground_stop_ended": "The FAA lifted the ground stop.",
    "ground_delay_started": "The FAA started a ground delay program{end_text}.",
    "ramp_closed": "The ramp closed for lightning{end_text}.",
    "ramp_reopened": "The ramp reopened.",
    "flight_delayed": "{flight} was delayed{new_text}.",
    "flight_delay_updated": "{flight} is now expected to leave{new_text}.",
    "flight_cancelled": "{flight} was cancelled.",
    "flight_departed": "{flight} left the gate.",
    "flight_airborne": "{flight} took off.",
    "flight_holding": "{flight} is holding in the air.",
    "flight_diverted": "{flight} diverted{div_text}.",
    "flight_resumed": "{flight} is on its way again.",
    "flight_arrived": "{flight} arrived.",
    "tarmac_started": "{flight} is waiting on the tarmac.",
    "tarmac_ended": "{flight}'s tarmac wait ended.",
    "reserve_crew_assigned": "Reserve crew {crew} was assigned to {flight}.",
    "crew_reassigned": "Crew {crew} moved onto {flight}.",
    "crew_out_of_duty": "{flight}'s crew ran out of legal duty time.",
    "pilot_duty_extended": "{flight}'s pilots had their duty extended.",
    "weather_observed": "A new weather report came in.",
    "next_day_flight_created": "A flight for tomorrow was added ({flight}).",
    "floor_report": "A report from the floor: {text}",
}


def clean(text) -> str:
    """Never show code words: underscores become spaces, version tags go."""
    text = str(text or "")
    text = SNAKE.sub(lambda m: m.group(0).replace("_", " "), text)
    return VERSION.sub("", text).replace("  ", " ").strip()


def _t(v):
    if isinstance(v, int) and not isinstance(v, bool):           # the simulator's minutes after midnight
        from simulator.clock import at
        return at(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return v


class Plain:
    def __init__(self, copilot, settings):
        self.c, self.s = copilot, settings
        rows = copilot.context.rows
        self.cities = {x["code"]: x["name"] for x in rows.get("stations", [])}
        self.flights = {f["id"]: f for f in rows.get("flights", [])}
        net = getattr(copilot.network, "context", None)
        self.groups = {}
        for g in (net.rows.get("passenger_groups", []) if net is not None else []):
            self.groups.setdefault(g["flight_id"], {})[g["segment"]] = g["count"]

    # ------------------------------------------------------------ small pieces
    def city(self, fid):
        f = self.flights.get(fid) or {}
        dest = f.get("destination") if f.get("origin") == "MCI" else f.get("origin")
        return self.cities.get(dest, dest or "")

    def flight(self, fid):
        c = self.city(fid)
        f = self.flights.get(fid) or {}
        return f"{fid} {'to' if f.get('origin') == 'MCI' else 'from'} {c}" if c else (fid or "the flight")

    def connections(self, fids):
        return sum(int(self.groups.get(f, {}).get("connecting", 0)) for f in fids)

    def event(self, e):
        p = e.get("payload") or {}
        end = _t(p.get("expected_end"))
        new = _t(p.get("new_departure"))
        fill = {"flight": p.get("flight_id") or "a flight", "crew": p.get("crew_id") or p.get("reserve_id") or "",
                "end_text": f" until about {hhmm(end)}" if end else "", "new_text": f" to {hhmm(new)}" if new else "",
                "div_text": f" to {self.cities.get(p.get('diverted_to'), p.get('diverted_to'))}" if p.get("diverted_to") else "",
                "text": p.get("text") or (p.get("kind") or "").replace("_", " ")}
        tmpl = EVENTS.get(e["type"])
        return clean(tmpl.format(**fill) if tmpl else (e["type"].replace("_", " ").capitalize()
                                                       + (f": {p['flight_id']}" if p.get("flight_id") else "") + "."))

    # ------------------------------------------------------------ the headline
    def headline(self, q):
        p, a, f = q.proposal or {}, q.action, q.target.get("flight_id")
        n = len(q.passenger_ids) or q.pax
        msgs = p.get("messages") or []
        new = _t(p.get("new_departure"))
        who = "pilots" if p.get("crew_role") == "pilot" else "flight attendants"
        tasks = p.get("tasks") or []
        if a == "delay_flight":
            return f"Hold {self.flight(f)} until {hhmm(new)}" if new else f"Hold {self.flight(f)}"
        if a == "cancel_flight":
            return f"Cancel {self.flight(f)} today"
        if a == "call_in_reserve_crew":
            return f"Call in reserve {who} {p.get('crew_id')} so {self.flight(f)} can fly"
        if a == "swap_crews":
            return f"Move crew {p.get('crew_id')} onto {self.flight(f)} as its {who}"
        if a == "deadhead_crew":
            return f"Fly crew {p.get('crew_id')} in to work {self.flight(f)}"
        if a == "extend_crew_duty":
            return f"Keep {self.flight(f)} flying today: extend the pilots' duty"
        if a == "divert_flight":
            return f"Decide whether {self.flight(f)} diverts or keeps holding"
        if a == "rebook_passenger":
            to = ((self.c.care.context.saved.get(("recommendations", q.rec_id)) or {}).get("options") or [{}])[0].get("flight_id") \
                if self.c.care else None
            return f"Move {n} Customer{'s' if n != 1 else ''} onto {self.flight(to)}" if to else f"Rebook {n} Customers"
        if a == "issue_compensation" and p.get("human_only"):
            return f"Decide whether {f or 'the disruption'} was within our control (what Customers are owed)"
        if a == "issue_compensation":
            return f"Book a hotel room tonight for {n} Customer{'s' if n != 1 else ''}"
        if a == "arrange_ground_transport":
            return f"Arrange a ride for {n} Customer{'s' if n != 1 else ''}"
        if a == "offer_refund":
            return f"Offer {n} Customer{'s' if n != 1 else ''} a refund if they decline the new trip"
        if a in ("hold_hotel_rooms", "release_hotel_rooms"):
            return ("Hold" if a.startswith("hold") else "Release") + f" {p.get('rooms') or ''} hotel rooms".replace("  ", " ")
        if a == "assign_gates":
            return f"Put {self.flight(f)} at gate {p.get('gate')}" + (f" from {hhmm(p.get('from'))}" if p.get("from") else "")
        if a == "move_gate_agents":
            roles = sorted({t.get("role") for t in tasks if t.get("role")})
            gate = next((g["gate"] for g in self._gates() if g.get("flight_id") == f and g.get("gate")), None)
            phase = next((t.get("task") for t in tasks if t.get("task")), "work")
            words = {"gate_agent": "gate agent", "ramp": "ramp worker", "customer_service": "customer service agent",
                     "wheelchair": "wheelchair agent"}
            k = len(tasks)
            what = (f"a {words.get(roles[0], roles[0])}" if k == 1 and roles else f"{k} staff" if k else "staff")
            phase_word = {"departure": "departure", "arrival": "arrival", "boarding": "boarding"}.get(phase, phase)
            return (f"Move {what} to gate {gate} for {f}'s {phase_word}" if gate else f"Send {what} to {f}'s {phase_word}")                 if f else f"Move {what}"
        if a == "call_in_ground_staff":
            return f"Call in {p.get('staff_id') or 'ground staff'} early"
        if a == "request_wheelchair_help":
            return f"Send a wheelchair agent to {n or 'a'} Customer{'s' if n != 1 else ''}"
        if a == "supervise_child_alone":
            return "Name a staff member to look after a child flying alone"
        if a in ("arrange_food_water",):
            return f"Bring food and water to {self.flight(f)}"
        if a in ("start_deplaning", "arrange_diversion_deplaning"):
            return f"Offer {self.flight(f)}'s Customers the chance to get off"
        if a in ("send_mass_message", "send_status_update", "draft_message"):
            cause = self._cause()
            k = len({m.get("passenger_id") for m in msgs}) or n
            kinds = {m.get("type") for m in msgs}
            if kinds <= {"first_notice"} and cause:
                return f"Tell {k:,} Customers about the {cause}"
            return f"Send {k:,} Customer{'s' if k != 1 else ''} an update" + (f" about {f}" if f else "")
        if a == "send_staff_briefing":
            return "Brief gate and call-center staff on what to tell Customers"
        if a == "send_crew_message":
            crew = next((m.get("crew_id") for m in msgs if m.get("crew_id")), None)
            return f"Tell crew {crew} about their new assignment" if crew else "Send the crew their new assignment"
        if a == "classify_cause":
            return f"Confirm what is causing the disruption" + (f" (Situation thinks: {self._cause()})" if self._cause() else "")
        if a == "contact_partner":
            return "Check the ground stop's status with the FAA"
        if a == "resolve_source_conflict":
            return f"Settle which report about {', '.join(q.flights) or 'the flight'} is right"
        if a == "escalate_channel_problem":
            return "Look into a channel that is not updating Customers"
        if a in ("declare_recovery", "close_incident", "rank_incidents"):
            return {"declare_recovery": "Declare that the airport is recovering", "close_incident": "Close the incident",
                    "rank_incidents": "Decide which incident to handle first"}[a]
        if a in ("ramp_closure", "reopen_ramp", "stop_work_for_staff_safety"):
            return {"ramp_closure": "Close the ramp for lightning", "reopen_ramp": "Reopen the ramp",
                    "stop_work_for_staff_safety": "Stop ramp work to keep staff safe"}[a]
        if a in ("return_to_gate", "keep_on_board_exception"):
            return {"return_to_gate": f"Bring {self.flight(f)} back to a gate", "keep_on_board_exception":
                    f"Decide whether {self.flight(f)}'s Customers stay on board"}[a]
        return clean(q.title)

    def _cause(self):
        rows = self.c.store.records["situation_assessments"]
        if not rows:
            return None
        r = rows[-1]
        return {"weather": "ground stop", "faa_program": "FAA program", "it_outage": "systems outage", "security": "security event",
                "airport": "airport problem"}.get(r.get("type"))

    def _gates(self):
        rows = self.c.store.records["station_plans"]
        return (rows[-1].get("gates") or []) if rows else []

    # ------------------------------------------------------------ the rest of the item
    def story(self, q):
        out = []
        why = q.why or {}
        sit = next((i for i in why.get("inputs", []) if i["kind"] == "situation"), None)
        if sit is not None:
            row = next((r for r in self.c.store.records["situation_assessments"] if r["version"] == sit["version"]), None)
            if row is not None:
                out.append({"t": sit["t"], "text": self.situation_says(row)})
        for e in self._trigger_events(q)[-2:]:
            out.append({"t": hhmm(e["occurred_at"]), "text": self.event(e)})
        agent = {"network_recovery": "Network Recovery", "passenger_care": "Passenger Care", "station_staff": "Station & Staff",
                 "communication": "Communication", "situation": "Situation", "orchestrator": "The orchestrator"}.get(q.agent, "The co-pilot")
        verb = "asked a person to decide" if (q.proposal or {}).get("human_only") else "proposed this"
        out.append({"t": q.asked, "text": f"{agent} {verb}; it passed the rule check and went to "
                                          f"{ROLE_NAMES.get(q.role, (q.role or 'a person').replace('_', ' '))}."})
        out.sort(key=lambda x: x["t"])
        return [{"t": x["t"], "text": clean(x["text"])} for x in out[-4:]]

    def _trigger_events(self, q):
        evs = [e for e in self.c.store.events if e["id"] > 0 and e["occurred_at"] <= q.asked_at and e["type"] in EVENTS]
        mine = [e for e in evs if (e.get("payload") or {}).get("flight_id") in set(q.flights)] if q.flights else []
        return mine or [e for e in evs if e["type"].startswith(("ground_", "ramp_"))]

    def recommendation(self, q):
        p = q.proposal or {}
        if p.get("human_only"):
            return clean(f"This is your decision as {ROLE_NAMES.get(q.role, q.role)}; the co-pilot only shows the facts. "
                         + (p.get("reason") or ""))
        rec = self._rec(q)
        why = rec.get("why") or ""
        why = re.sub(r";?\s*protection [\d.]+", "", why)
        return clean(f"The co-pilot suggests: {self.headline(q)}." + (f" Why: {why}." if why else "")).replace("..", ".")

    def _rec(self, q):
        ctx = {"network_recovery": getattr(self.c.network, "context", None), "passenger_care": getattr(self.c.care, "context", None),
               "station_staff": getattr(self.c.station, "context", None), "communication": self.c.context,
               "situation": self.c.situation.context, "orchestrator": getattr(self.c, "octx", None)}.get(q.agent)
        return (ctx.saved.get(("recommendations", q.rec_id)) if ctx is not None else None) or {}

    def options(self, q):
        p = q.proposal or {}
        head = self.headline(q)
        first = {"id": "opt1", "label": head, "why": clean(self._rec(q).get("why") or p.get("reason") or "the agent's plan"),
                 "recommended": not p.get("human_only"), "effect": self.effect(q)["text"]}
        out = [first]
        if q.agent == "network_recovery" and q.action in ("call_in_reserve_crew", "swap_crews", "deadhead_crew"):
            for o in (q.why or {}).get("options_considered") or []:
                if not str(o.get("why_not", "")).startswith("legal"):
                    continue                                 # an illegal option is not an option
                kind, crew = (o["option"].split() + [""])[:2]
                out.append({"id": f"opt{len(out) + 1}", "label": f"Use {kind} {crew} instead", "crew_id": crew,
                            "action": {"reserve": "call_in_reserve_crew", "swap": "swap_crews", "deadhead": "deadhead_crew"}.get(kind),
                            "why": "legal too; the agent ranked it lower", "recommended": False,
                            "effect": f"{crew} flies {q.target.get('flight_id')} instead of {p.get('crew_id')}"})
        elif q.agent == "network_recovery" and q.action == "cancel_flight":
            g = ((p.get("option") or {}).get("cancellation") or {})
            if not g.get("no_alternative"):
                out.append({"id": "opt2", "label": f"Keep {self.flight(q.target.get('flight_id'))} and delay it instead",
                            "why": f"the agent compared it with this: about {g.get('alternative_stranded', 0)} Customers stranded "
                                   f"instead of {g.get('stranded', 0)}",
                            "recommended": False, "effect": "the cancellation is declined; Network Recovery plans a delay"})
        elif p.get("human_only") and q.action == "extend_crew_duty":
            first.update(label=f"Extend the pilots' duty so {q.target.get('flight_id')} can leave", recommended=False)
            out.append({"id": "opt2", "label": "Do not extend", "why": "today's process decides; the flight may be cancelled",
                        "recommended": False, "effect": q.consequence or ""})
        return [{**o, "label": clean(o["label"]), "why": clean(o["why"]), "effect": clean(o["effect"])} for o in out]

    def effect(self, q):
        fl = [f for f in q.flights if f]
        n = q.pax or len(q.passenger_ids)
        conn = self.connections(fl) if q.agent == "network_recovery" else 0
        bits = []
        if n:
            bits.append(f"{n:,} Customer{'s' if n != 1 else ''}")
        if conn:
            bits.append(f"{conn} connecting")
        if fl:
            bits.append(f"{len(fl)} flight{'s' if len(fl) != 1 else ''} ({', '.join(fl[:3])}{'...' if len(fl) > 3 else ''})")
        return {"customers": int(n), "connections": int(conn), "flights": len(fl), "text": ("Touches " + ", ".join(bits) + ".")
                if bits else "No Customers or flights are touched directly."}

    def if_declined(self, q):
        return clean((q.consequence or "the co-pilot does not do this; today's process handles it").rstrip(".") + ".").capitalize()

    def item(self, q):
        return {"headline": clean(self.headline(q)), "story": self.story(q), "recommendation": self.recommendation(q),
                "options": self.options(q), "effect": self.effect(q), "if_declined": self.if_declined(q)}

    # ------------------------------------------------------------ the trace in plain words
    def situation_says(self, row):
        cause = re.sub(r"\s*\(signals from[^)]*\)", "", (row.get("likely_cause") or "").replace("suspected: ", ""))
        cause = cause.replace(" / ", " and ")
        likely, latest = _t(row.get("expected_end_likely")), _t(row.get("expected_end_latest"))
        conf = row.get("end_confidence")
        bits = []
        if row.get("status") == "abnormal":
            bits.append(f"{cause[:1].upper() + cause[1:]}." if cause else "Something is disrupting MCI.")
            if likely:
                bits.append(f"FAA ground stop until about {hhmm(likely)}" + (f"; {int(round(conf * 100))}% sure it lifts by "
                                                                            f"{hhmm(latest)}." if conf and latest else "."))
        else:
            bits.append({"normal": "Operations at MCI look normal.", "recovery": "MCI is recovering."}.get(row.get("status"),
                                                                                                        "I'm watching MCI."))
        return clean(" ".join(bits))

    def says(self, kind, version):
        table = {"situation": "situation_assessments", "network_recovery": "recovery_plans", "passenger_care": "care_plans",
                 "station_staff": "station_plans"}.get(kind)
        row = next((r for r in self.c.store.records[table] if r["version"] == version), None) if table else None
        if row is None:
            return None
        if kind == "situation":
            return self.situation_says(row)
        if kind == "network_recovery":
            d, c, w = len(row.get("delays") or []), len(row.get("cancels") or []), len(row.get("warnings") or [])
            return clean(f"I plan {d} delay{'s' if d != 1 else ''} and {c} cancellation{'s' if c != 1 else ''}"
                         + (f", and I'm watching {w} crew or fuel risk{'s' if w != 1 else ''}." if w else "."))
        if kind == "passenger_care":
            pax = row.get("passengers") or []
            hotel = sum(1 for x in pax if "overnight" in (x.get("needs") or []))
            return clean(f"I'm looking after {len(pax)} Customer{'s' if len(pax) != 1 else ''}"
                         + (f"; {hotel} need{'s' if hotel == 1 else ''} somewhere to stay tonight." if hotel else "."))
        ramp = row.get("ramp_status") or {}
        short = [s_ for s_ in row.get("staff_shortages") or [] if s_.get("short_by")]
        return clean(("The ramp is closed for " + (ramp.get("reason") or "safety") + "." if ramp.get("open") is False else "The ramp is open.")
                     + (f" I'm short of {short[0]['role'].replace('_', ' ')} staff from {hhmm(short[0]['from'])}." if short else ""))

    def label(self, kind, version, t):
        return f"{PLAN_NAMES.get(kind, kind)} {t}".strip()

    def relabel(self, trace, q):
        """Plain names with times on every node (never "v4" or code words); each agent node says, in the first person, what
        its version said. Built from the node's own detail and the shared records."""
        names = {"network_recovery": "Network Recovery", "passenger_care": "Passenger Care", "station_staff": "Station & Staff",
                 "communication": "Communication", "situation": "Situation"}
        for n in trace["nodes"]:
            k, d, t = n["kind"], n.get("detail") or {}, n.get("t") or ""
            if k == "feed":
                text = self.event({"type": d.get("type", ""), "payload": d.get("payload") or {}}) if d.get("type") else clean(d.get("text"))
                n["label"] = clean(f"{text.rstrip('.')} {t}")
            elif k in PLAN_NAMES and d.get("version") is not None and k != "orchestrator":
                n["label"] = self.label(k, d["version"], t)
                says = self.says(k, d["version"])
                if says:
                    d["says"] = says
            elif k in names and "recommendation" in d:
                n["label"] = f"{names[k]} proposal {t}".strip()
                d["says"] = clean(f"I propose: {self.headline(q)}.")
            elif k == "rule_check":
                n["label"] = ("Rule check passed" if "passed" in n["label"] else "Rule check: blocked") + f" {t}".rstrip()
            elif k == "orchestrator":
                n["label"] = clean(f"Sent to {ROLE_NAMES.get(q.role, (q.role or '').replace('_', ' '))} {t}")
                d["says"] = clean(f"I sent this to {ROLE_NAMES.get(q.role, (q.role or '').replace('_', ' '))} at {t}"
                                  + (", in the " + q.bundle["title"] + " bundle." if q.bundle else "."))
            elif k == "question":
                n["label"] = clean(q.plain.get("headline") or self.headline(q))
            elif k == "human":
                by = str(d.get("by") or "")
                who = ("The co-pilot" if by.startswith("copilot:auto") else "The duty manager" if "director_of_operations" in by
                       else "You")
                verb = {"approved": "approved", "rejected": "said no", "acknowledged": "acknowledged"}.get(d.get("decision"), "answered")
                n["label"] = (f"Done automatically: {d['rule']}" if d.get("rule") else f"{who} {verb} {t}").strip()
            elif k in ("change", "communication", "channel", "staff"):
                n["label"] = clean(n["label"].replace("(fa)", "(flight attendants)").replace("(pilot)", "(pilots)")
                                   .replace("(fa,", "(flight attendants,").replace("(pilot,", "(pilots,"))
        return trace
