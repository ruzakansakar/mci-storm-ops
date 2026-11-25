"""The orchestrator (Phase 5a, part 1): plain code, no AI. It owns the co-pilot's minute loop. Coordination only: nothing it
does changes a flight, crew, booking or any other truth table.

Each minute T:
  0. the people's answers come from the ONE approval queue (backend/orchestrator/queue.py); its own questions (A10, Q07, T05)
     are handled here, the rest go back to the agent that asked;
  1. the agents run in the agreed order: Situation -> Network Recovery -> Passenger Care -> Station & Staff -> Communication, each
     through its own AgentContext (data up to T only). Before each one runs, the orchestrator notes the newest shared versions
     available, so the shared-state audit can tell whether it read them (backend/orchestrator/audit.py);
  2. every proposal is rule-checked (backend/rules/check.py) before any person sees it: the runners call the check and the queue
     refuses anything that did not pass (a block is recorded, never shown for approval and never overridden);
  3. incidents are opened, ranked, recovered and closed from Situation's picture (backend/orchestrator/incidents.py), and every
     proposal, queue item, shared plan version and message made while one is open is tagged with it;
  4. cross-agent conflicts are detected (backend/orchestrator/conflicts.py): both views logged, the newer waiting proposal
     blocked or a person asked;
  5. the queue escalates what nobody answered by its deadline (S06) and counts each role's questions per hour;
  6. one orchestrator_log row: who ran, the versions each read and wrote, proposals, blocks, conflicts, queue, incidents.
"""
from copy import deepcopy
from datetime import timedelta

import yaml

from backend.contracts.buffered import BufferedContext
from backend.contracts.shared_state import DISAGREE, SHARED
from backend.execution.copilot import Copilot, serial
from backend.execution.executor import check_only
from backend.orchestrator import audit as A
from backend.orchestrator import conflicts as C
from backend.orchestrator.incidents import Incidents
from backend.orchestrator.queue import Queue
from backend.settings import ROOT

APPLIES = ("delay_flight", "cancel_flight", "swap_crews", "call_in_reserve_crew", "deadhead_crew", "rebook_passenger")
AGENT_TABLE = {"situation": "situation_assessments", "network_recovery": "recovery_plans", "passenger_care": "care_plans",
               "station_staff": "station_plans"}


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "orchestrator.yaml").read_text(encoding="utf-8"))


class Orchestrator(Copilot):
    """The co-pilot's minute loop, owned by the orchestrator. The agents and their runners are the same; the orchestrator runs
    them in order and adds the queue, incidents, conflicts and the shared-state audit around them."""

    def __init__(self, *args, apply_port=None, pending_port=None, **kwargs):
        from backend.contracts.decisions import load_decisions
        from backend.rules import load_rules
        super().__init__(*args, **kwargs)
        self.apply_port = apply_port                    # Phase 5b: orchestrated mode carries out approved decisions
        self.pending_port = pending_port                # fix-seats: the world's thinning waits for these
        self.applied_log = []
        if self.care is not None:
            self.care.ask_advice = apply_port is not None   # rebooking advice becomes a decision a person approves
        self.os = load_settings()
        self.rules = load_rules()
        self.queue = Queue(self.humans, self.os, self.rules, load_decisions())
        self.people = self.humans                       # the simulated people behind the queue (None in some tests)
        self.humans = self.queue                        # every runner asks through the queue now
        if self.network:
            self.network.humans = self.queue
        self.octx = BufferedContext(None, feeds=self.context.feeds, agent="orchestrator", store=self.store, rows={})
        self.incidents = Incidents(self.octx, self.os)
        self.log, self.conflicts, self.findings, self.tags = [], [], [], []
        self.conflict_keys, self.own, self.blocked = set(), {}, []
        self.seen = {"messages": 0, "recovery": 0, "recs": {}}
        self.workload = {}                              # role -> {hour: count}
        self.rec_seen = {}                              # context -> the last saved row id already looked at
        self.flight_incident = {}                       # flight -> the incident that disrupted it (5b: tags follow the work)
        self.event_seen = 0
        self.directions = {f["id"]: f["direction"] for f in kwargs.get("network_rows", {}).get("flights", [])} \
            if kwargs.get("network_rows") else {}
        self.advice = {}                                # Passenger Care rebooking advice: recommendation id -> its facts

    # ------------------------------------------------------------ the minute
    def tick(self, now, published, deliver):
        self.receive(published)
        self.octx.now = now
        self.queue.incident_id = self.incidents.primary()
        due = self.queue.due(now)
        self.answer_own([q for q in due if q.agent == "orchestrator"], now)
        human_out, approved_now = self.answers(now, [q for q in due if q.agent != "orchestrator"])
        if self.apply_port is not None:
            self._apply_approved(now, human_out)
        outputs, audits, row = [], [], {"at": now.isoformat(), "ran": [], "read": {}, "wrote": {}, "proposals": 0, "blocks": 0,
                                         "conflicts": [], "stale": []}
        for name in self.os["run"]["agents"]:
            available = A.newest(self.store, now)
            before = {t: len(self.store.records[t]) for t in SHARED}
            self._tag_contexts()
            if name == "communication":
                o, a = self.communicate(now, deliver, approved_now, human_out)
            else:
                o, a = self.run_agent(name, now)
            if name == "situation":
                self._incidents(now)
                self._attribute_flights()
                self.queue.incident_id = self.incidents.primary()
                self._tag_contexts()
            ran = name == "communication" or any(k == "agent_heartbeat" for _, k, _ in o)
            read = A.reads_from(a)
            wrote = {t: [r for r in self.store.records[t][before[t]:]] for t in SHARED}
            mine = wrote.get(AGENT_TABLE.get(name), [])
            if ran:
                row["ran"].append(name)
            if read:
                row["read"][name] = read
            if mine:
                row["wrote"][name] = [r["version"] for r in mine]
            found = A.audit_agent(name, read, available, mine)
            row["stale"] += found
            self.findings += [{**f, "at": now.isoformat()} for f in found]

            for t, rows_ in wrote.items():
                for r in rows_:
                    for inc in self._incidents_of_row(t, r):
                        self.tags.append({"incident_id": inc, "kind": t, "ref": r[SHARED[t]["version"]], "at": now.isoformat()})
            outputs, audits = outputs + o, audits + a
        self._tag_messages(now)
        for c in self._contexts():                      # every new proposal: counted, its rule check result, its incident
            for rid, rec in self._new_recs(c):
                if c.agent == "passenger_care" and rec.get("_action") == "rebook_passenger":
                    self.advice[rid] = rec
                if self.incidents.primary() is not None:              # the incident its flights belong to (else the first)
                    rec["incident_id"] = self._incident_for(self._flights_in(rec)) or rec.get("incident_id") or self.incidents.primary()
                row["proposals"] += 1
                row["blocks"] += rec.get("rule_check_ok") is False
                self.tags.append({"incident_id": rec.get("incident_id"), "kind": "recommendation", "ref": f"{c.agent}:{rid}",
                                  "at": now.isoformat()})
        row["conflicts"] = self._conflicts(now)
        for item in self.queue.items[self.seen.setdefault("items", 0):]:   # queue items follow their proposal's flights (5b)
            if item.incident_id is not None:
                flights = [item.facts.get("flight_id")] if item.facts.get("flight_id") else self._flights_in(
                    {"title": item.facts.get("title"), "options": item.members[0][2] if item.members else None})
                item.incident_id = self._incident_for(flights) or item.incident_id
        self.seen["items"] = len(self.queue.items)
        self.queue.escalate(now, self.os["queue"]["escalation_chain"])
        counts = self.queue.workload(now, self.os["queue"]["workload_max_per_hour"])
        hour = now.strftime("%H:00")
        for role, n in counts.items():
            self.workload.setdefault(role, {})[hour] = max(self.workload.get(role, {}).get(hour, 0), n)
        self._close(now)
        for kind, payload in self.queue.events:
            self.octx.record_event(kind, payload)
        self.queue.events = []
        row.update(queue=len(self.queue.ordered()), incidents=[{"id": i.id, "kind": i.kind, "status": i.status, "rank": i.rank}
                                                                for i in self.incidents.all if i.status != "resolved"])
        self.log.append(row)
        if self.pending_port is not None:               # co-pilot delays / cancellations still waiting for a person
            day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
            pending = {}
            for i in self.queue.ordered():
                fid = i.facts.get("flight_id") or (i.members[0][2].get("flight_id") if i.members else None)
                if i.agent == "network_recovery" and i.action in ("delay_flight", "cancel_flight") and fid:
                    pending[fid] = max(pending.get(fid, 0), int((i.deadline - day0).total_seconds() // 60))
            self.pending_port(pending)
        self.octx.write("orchestrator_log", {"at": now, "row": serial(row)})
        o, a = self._drain(self.octx, "orchestrator")
        return outputs + o, audits + a

    # ------------------------------------------------------------ Phase 5b: approved decisions change the world
    def _apply_approved(self, now, human_out):
        """Every decision a person approved this minute that changes the world goes through the executor: rule-checked again
        now, carried out by the operational systems (or refused, with the reason), and logged in events and the audit log."""
        from backend.execution.executor import apply_approved
        for actor, kind, a in human_out:
            if kind == "network_approved":
                p = self.network.approvals.options.get(a["proposal_id"]) or {}
                facts = {"proposal_id": a["proposal_id"], "plan_version": a["plan_version"], "action": p.get("action"),
                         "flight_id": p.get("flight_id"), "new_departure": p.get("new_departure"), "crew_id": p.get("crew_id"),
                         "role": p.get("crew_role")}
                proposal = {"id": a["proposal_id"], "agent": "network_recovery", "action": p.get("action"),
                            "decision_ids": p.get("decision_ids") or ["B02"], "flight_id": p.get("flight_id"), "facts": facts}
            elif kind == "human_approved" and a.get("agent") == "passenger_care" and a.get("action") == "rebook_passenger":
                p = self.care.options.get(a["recommendation"]) or {}
                rec = self.care.context.saved.get(("recommendations", a["recommendation"])) or {}
                flight = ((rec.get("options") or [{}])[0]).get("flight_id")
                facts = {"passenger_ids": p.get("passenger_ids", []), "flight_id": flight, "recommendation": a["recommendation"]}
                proposal = {"id": a["recommendation"], "agent": "passenger_care", "action": "rebook_passenger",
                            "decision_ids": p.get("decision_ids") or ["K01"], "facts": facts}
            else:
                continue
            if not proposal["action"] or proposal["action"] not in APPLIES:
                continue
            out = apply_approved(proposal, now, self.apply_port)
            entry = {"at": now.isoformat(), "agent": proposal["agent"], "recommendation": proposal["id"], "action": proposal["action"],
                     "facts": serial(proposal["facts"]), "approved_by": a.get("role"), "applied": out["applied"],
                     "reason": out.get("reason"), "moved": out.get("moved")}
            self.applied_log.append(entry)
            kind_out = "decision_applied" if out["applied"] else "decision_refused"
            self.octx.outputs.append((kind_out, entry))
            self.octx._audit_row(kind_out, "recommendation", proposal["id"], None, entry)

    # ------------------------------------------------------------ incidents
    def _new_recs(self, ctx):  # noqa: D401
        """The recommendations this context saved since the last minute (row ids grow by one per insert)."""
        last = self.rec_seen.get(id(ctx), 0)
        out = []
        for key in reversed(ctx.saved):
            if key[1] <= last:
                break
            if key[0] == "recommendations":
                out.append((key[1], ctx.saved[key]))
        self.rec_seen[id(ctx)] = len(ctx.saved)
        return list(reversed(out))

    def _tag_contexts(self):
        inc = self.incidents.primary()
        for ctx in self._contexts():
            ctx.incident_id = inc

    def _contexts(self):
        return [self.situation.context, self.context] + [r.context for r in (self.network, self.care, self.station) if r]

    def _incidents(self, now):
        pic = self.octx.read_shared("situation_assessments")
        for kind, payload in self.incidents.update(now, pic, lambda *a: self._own(now, *a)):
            self.octx.record_event(kind, payload)

    def _own(self, now, decision, action, title, why, incident):
        """The orchestrator's own proposal (A10, Q07, T05): rule-checked, then into the queue for the named person."""
        out = {"title": title, "why": why, "data_as_of": now.isoformat(), "options": [{"id": decision, "summary": title}],
               "sources": [{"source": "situation_assessments", "as_of": now.isoformat()}], "decision_ids": [decision],
               "incident_status": {"monitoring": "monitoring"}.get(incident.status, "open"),
               "assigned_agents": ["situation"], "open_questions": [title]}
        self.octx.incident_id = incident.id
        rid = self.octx.propose(action, out)
        rec = self.octx.saved[("recommendations", rid)]
        p = {"id": rid, "agent": "orchestrator", "action": action, "decision_ids": [decision], "data_as_of": now, "title": title,
             "requested_level": "needs_approval"}
        verdict = check_only(p, {"now": now, "flights": {}})
        rec["rule_check_ok"], rec["rule_check_notes"], rec["rule_ids"] = verdict["result"] == "pass", verdict["reasons"], verdict["rule_ids"]
        kind = "rule_check_passed" if verdict["result"] == "pass" else "rule_check_blocked"
        v = {"proposal": serial(p), "verdict": verdict, "automatic": False, "would_need_approval_by": rec.get("approver_role"),
             "at": now.isoformat()}
        self.octx.outputs.append((kind, serial(v)))                    # in the events AND the audit log, as for every agent
        self.octx._audit_row(kind, "recommendation", rid, None, serial(v))
        self.own[rid] = (decision, incident.id)
        self.queue.incident_id = incident.id
        self.queue.ask("orchestrator", rid, p, rec["approver_role"], now, verdict["result"] == "pass")
        return rid

    def answer_own(self, due, now):
        for q in due:
            decision, inc = self.own.get(q.rec_id, (None, None))
            rec = self.octx.saved.get(("recommendations", q.rec_id))
            if q.answer.get("decision") == "rejected":          # live mode: a person said no; the incident does not move on
                if rec is not None:
                    rec["status"] = "rejected"
                self.octx.record_event("incident_updated", {"incident_id": inc, "decision": decision, "rejected_by": q.role,
                                                            "status": "unchanged"})
                continue
            if rec is not None:
                rec["status"] = "approved"
            for kind, payload in self.incidents.approved(decision, inc, now, self._close_contexts):
                self.octx.record_event(kind, {**payload, "approved_by": q.role, "waited_min": q.answer["waited_min"]})

    def _close_contexts(self):
        return sum(ctx.close_incident() for ctx in self._contexts() + [self.octx])

    def _close(self, now):
        def waiting(inc_id):          # something for it is still with a person, inside its (escalated) deadline
            return any(i.incident_id == inc_id and i.kind != "conflict" and i.agent != "orchestrator" and i.deadline > now
                       for i in self.queue.ordered())
        for i in self.incidents.ready_to_close(now, waiting):
            i.asked["Q07"] = self._own(now, "Q07", "close_incident", f"Close incident #{i.id} ({i.kind})",
                                       f"recovery since {i.recovery_at:%H:%M}; nothing left waiting for a person", i)
            self.octx.record_event("incident_updated", {"incident_id": i.id, "close_proposed": True, "decision": "Q07"})

    def _tag_messages(self, now):
        inc = self.incidents.primary()
        for m in self.messages.log[self.seen["messages"]:]:
            flights = list((m.get("trip_facts") or {}).get("itinerary") or [])
            self.tags.append({"incident_id": (self._incident_for(flights) or inc) if inc else None, "kind": "message",
                              "ref": m.get("id") or m.get("rec"), "at": now.isoformat()})
        self.seen["messages"] = len(self.messages.log)
        for d in self.recovery.history[self.seen["recovery"]:]:
            if d.get("status") == "drafted":
                self.tags.append({"incident_id": (self._incident_for([d["flight_id"]]) or inc) if inc else None,
                                  "kind": "recovery_draft", "ref": d["id"], "at": now.isoformat()})
        self.seen["recovery"] = len(self.recovery.history)

    # ------------------------------------------------------------ which incident a piece of work belongs to (5b)
    DISRUPTS = ("flight_delayed", "flight_delay_updated", "flight_cancelled", "flight_diverted", "flight_holding", "tarmac_started")

    def _attribute_flights(self):
        """Each flight belongs to the incident that disrupted it: of the incidents open when it was first disrupted, the most
        recently opened one whose cause can affect that kind of flight (config incidents.scope); else the first-ranked."""
        scope = self.os["incidents"]["scope"]
        events = self.store.events
        new = [e for e in events if e["id"] > 0][self.event_seen:] if self.event_seen else [e for e in events if e["id"] > 0]
        self.event_seen += len(new)
        open_ = sorted(self.incidents.open(), key=lambda i: i.opened_at, reverse=True)
        if not open_:
            return
        for e in new:
            fid = (e.get("payload") or {}).get("flight_id")
            if e["type"] not in self.DISRUPTS or not fid or fid in self.flight_incident:
                continue
            kind = self.directions.get(fid, "departure")
            match = next((i for i in open_ if i.opened_at <= e["occurred_at"] and kind in scope.get(i.kind, [kind])), None)
            self.flight_incident[fid] = (match or open_[-1]).id if match or open_ else None

    def _incident_for(self, flights):
        open_ids = {i.id for i in self.incidents.open()}
        for fid in flights or []:
            inc = self.flight_incident.get(fid)
            if inc is not None and inc in open_ids:          # never to an incident already closed
                return inc
        return None

    @staticmethod
    def _flights_in(rec):
        import re
        return sorted(set(re.findall(r"\bWN\d{3,4}\b", str(rec.get("title")) + str(rec.get("options")))))

    def _incidents_of_row(self, table, row):
        """A shared version belongs to the incidents of the flights it changes; Situation's picture to every open incident."""
        if not self.incidents.primary():
            return [None]
        if table == "situation_assessments":
            return [i.id for i in self.incidents.open()]
        import re
        flights = set(re.findall(r"\bWN\d{3,4}\b", str({k: row.get(k) for k in ("delays", "cancels", "crew_actions", "gates",
                                                                                     "passengers", "tarmac")})))
        open_ids = {i.id for i in self.incidents.open()}
        incs = {self.flight_incident[f] for f in flights if self.flight_incident.get(f) in open_ids}
        return sorted(incs) or [self.incidents.primary()]

    # ------------------------------------------------------------ conflicts
    def _latest(self, table):
        rows = [r for r in self.store.records[table] if r[SHARED[table]["time"]] <= self.octx.now]
        return max(rows, key=lambda r: r["version"]) if rows else None

    def facts(self):
        """Everything the conflict checks look at, from the shared records and the runners' waiting proposals."""
        plan, station = self._latest("recovery_plans"), self._latest("station_plans")
        out = {"plan_version": (plan or {}).get("version"), "cancels": {c["flight_id"]: plan["version"] for c in (plan or {}).get("cancels") or []},
               "delays": (plan or {}).get("delays") or [], "station": station, "advice": [], "drafts": [], "directives": {},
               "crew": [], "aircraft": [], "gates": [], "staff": []}
        for rid, rec in self.advice.items():
            if rec.get("status") == "pending":
                p = self.care.options.get(rid) if self.care else None
                opt = (rec.get("options") or [{}])[0]
                out["advice"].append({"agent": "passenger_care", "rec_id": rid, "flight_id": opt.get("flight_id"),
                                      "passenger_ids": (p or {}).get("passenger_ids", [])})
        from backend.rules.recovery_message import directives
        view = None
        if plan is not None:
            view = {"facts": {k: plan.get(k) for k in ("delays", "cancels", "crew_actions", "approval_status")}}
        out["directives"] = directives(view, (self.recovery.world or {}).get("recovery_flights")) if view else {}
        out["drafts"] = [d for d in self.recovery.drafts.values() if d["status"] in ("drafted", "held") and d.get("audience") != "staff"]
        for c in (plan or {}).get("crew_actions") or []:
            crew = (c.get("option") or {}).get("crew_id")
            if crew:
                out["crew"].append({"resource": crew, "flight_id": c["flight_id"], "from": None, "to": None, "agent": "network_recovery",
                                    "rec_id": c.get("proposal_id"), "applied": False, "task": c.get("role")})
        for a in (plan or {}).get("aircraft_recovery") or []:
            out["aircraft"].append({"resource": a["aircraft_id"], "flight_id": a["flight_id"], "from": None, "to": None,
                                    "agent": "network_recovery", "rec_id": None, "applied": False, "task": "recover"})
        if self.station:
            st = self.station
            for g in (station or {}).get("gates") or []:             # gates planned from now on (a parked plane is a fact)
                if g.get("gate") and g.get("status") in ("assigned", "pending") and g["to"] > self.octx.now.isoformat():
                    out["gates"].append({"resource": g["gate"], "flight_id": g["flight_id"], "from": g["from"], "to": g["to"],
                                         "agent": "station_staff", "rec_id": None, "applied": g["status"] != "pending"})
            from backend.execution.ledger import effective
            for w in effective(st.staff_ledger):
                if w["to"] > self.octx.now:
                    out["staff"].append({"resource": w["staff_id"], "flight_id": w["flight_id"] or w["passenger_id"], "task": w["task"],
                                         "from": w["from"], "to": w["to"], "agent": "station_staff", "rec_id": w["recommendation"],
                                         "applied": True})
            for rid, p in st.memory.pending.items():
                for t in p.get("tasks") or []:
                    out["staff"].append({"resource": t["staff_id"], "flight_id": t.get("flight_id") or t.get("passenger_id"),
                                         "task": t["task"], "from": t["from"], "to": t["to"], "agent": "station_staff", "rec_id": rid,
                                         "applied": False, "replaces": p.get("replaces")})
        return out

    def _conflicts(self, now):
        f = self.facts()
        found = (C.rebooking_on_cancelled(f["advice"], f["cancels"])
                 + C.departure_without_capacity(f["delays"], f["station"], f["plan_version"])
                 + C.draft_contradicts_plan(f["drafts"], f["directives"], f["plan_version"])
                 + C.resource_used_twice("crew", f["crew"]) + C.resource_used_twice("aircraft", f["aircraft"])
                 + C.resource_used_twice("gate", f["gates"])
                 + C.resource_used_twice("staff", [u for u in f["staff"] if not self._replacing(u, f["staff"])]))
        new = []
        for c in found:
            if c["key"] in self.conflict_keys:
                continue
            self.conflict_keys.add(c["key"])
            c = {**c, "at": now.isoformat(), "incident_id": self.incidents.primary()}
            self.octx.record_event(DISAGREE, {"topic": c["kind"], "detail": c["detail"], "views": serial(c["views"]),
                                              "resolved_by": "nobody yet: a person is asked" if not c["block"] else "the newer proposal is blocked"})
            for agent, rid in c["block"]:
                self._block(agent, rid, c, now)
            role = self.os["queue"]["conflict_roles"].get(c["kind"], "station_manager")
            item = self.queue.add_conflict(role, now, c, c["incident_id"])
            c["queue_item"], c["asked"] = item.id, role
            self.conflicts.append(serial(c))
            new.append(c["kind"])
        return new

    @staticmethod
    def _replacing(u, uses):
        """A waiting replacement and the assignment it replaces are the same work, not a double booking."""
        return any(v.get("replaces") == u["rec_id"] and v["resource"] == u["resource"] for v in uses if not v["applied"]) and u["applied"]

    def _block(self, agent, rid, conflict, now):
        """Block a waiting proposal the conflict makes impossible. Never one already applied; never an override."""
        if agent == "communication_draft":
            d = next((x for x in self.recovery.drafts.values() if x["id"] == rid), None)
            if d is not None and d["status"] in ("drafted", "held"):
                d["orchestrator_block"] = conflict["detail"]
            self.blocked.append({"agent": "communication", "draft": rid, "why": conflict["detail"], "at": now.isoformat()})
            return
        ctx = {"passenger_care": self.care and self.care.context, "station_staff": self.station and self.station.context,
               "network_recovery": self.network and self.network.context}.get(agent)
        rec = ctx.saved.get(("recommendations", rid)) if ctx else None
        if rec is None or rec.get("status") not in ("pending",):
            return
        rec["status"] = "blocked_by_orchestrator"
        rec["blocked_why"] = conflict["detail"]
        self.queue.withdraw(agent, rid)
        if agent == "station_staff" and self.station:
            self.station.memory.pending.pop(rid, None)
        self.blocked.append({"agent": agent, "recommendation": rid, "why": conflict["detail"], "at": now.isoformat()})
