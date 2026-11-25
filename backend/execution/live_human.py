"""The person in the loop (live human-in-the-loop mode).

The same interface as SimulatedHumans (ask, withdraw, due, summary), placed where the simulated people were: behind the one
approval queue (backend/orchestrator/queue.py). So nothing an agent or the orchestrator does changes; only who answers does.

  - Questions asked before live_from are still answered by the simulated people (the morning is already decided).
  - From live_from on, every question goes to the user, who answers for ALL roles (the roles chosen at the start only filter
    the default view). Each one is a LiveQuestion: target, deadline (the queue's own, from the decision catalog), priority,
    depends_on, group, why and trace (backend/execution/live_trace.py).
  - The duty manager (director_of_operations) is the only simulated person left: an escalation at the deadline (the queue's
    existing path) or a hand-off goes to it, and it answers after its usual response time (config/simulated_humans.yaml).
  - approve / reject(reason) / wait(10 or 15 min; refused while another open question depends on this one) / hand_off.
  - A first "no" with another plan available closes that option for good (the agent comes back with its next option); with no
    other plan, the same proposal comes back once as last_option with its consequence; a second "no" is final and the
    consequence happens through the existing fallback path (today's process). A target rejected for good is never asked again.
  - Every answer is an event with actor "human:<role>".
"""
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import median

import yaml

from backend.execution.live_trace import hhmm, minute
from backend.execution.simulated_human import Question
from backend.rules.rejections import key_of, target_of
from backend.settings import ROOT

OPEN = ("waiting", "snoozed")
CREW_ACTS = ("call_in_reserve_crew", "swap_crews", "deadhead_crew")


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "live.yaml").read_text(encoding="utf-8"))


class DecideError(Exception):
    def __init__(self, code, message, blocked_by=None):
        super().__init__(message)
        self.code, self.message, self.blocked_by = code, message, blocked_by or []


@dataclass
class LiveQuestion:
    id: int
    agent: str
    rec_id: int
    action: str
    role: str
    asked_at: object
    deadline_at: object
    urgent: bool
    proposal: dict
    question: Question
    target: dict
    flights: list
    passenger_ids: list
    title: str
    pax: int = 0
    safety_legal: list = field(default_factory=list)
    status: str = "waiting"      # waiting | snoozed | approved | rejected | rejected_once | handed_off | answered_by_duty_manager
    escalated: bool = False      #   | withdrawn | refused_rejected_before
    snoozed_until: object = None
    last_option: bool = False
    consequence: str | None = None
    previous: int | None = None
    group_key: str = ""
    base_group: str = ""
    depends_on: list = field(default_factory=list)
    priority_score: float = 0.0
    wrong_now_pax: int = 0
    why: dict = field(default_factory=dict)
    answer: dict = field(default_factory=dict)
    answered_at: object = None
    waited: list = field(default_factory=list)      # each wait: {at, minutes}
    handed_off: bool = False

    @property
    def asked(self):
        return hhmm(self.asked_at)

    @property
    def deadline(self):
        return hhmm(self.deadline_at)


class LiveHumans:
    def __init__(self, sim, live_from, settings: dict, rng):
        self.sim, self.live_from, self.s, self.rng = sim, live_from, settings, rng
        self.duty_role = sim.s.get("duty_manager")
        self.questions: list[LiveQuestion] = []
        self.by_id: dict = {}
        self.ready: list = []          # (Question, decision, extra): answers the user gave, handed to the queue at the next minute
        self.duty: list = []           # questions with the (simulated) duty manager
        self.final: dict = {}          # rejection key -> how many times rejected (a target the person said no to for good)
        self.events: list = []         # (actor, kind, payload, now) for the session to record (events + agents' feed)
        self.traces: list = []         # (question id, snapshot, trace) to save
        self.dirty = set()             # question ids changed since the last save
        self.tracer = self.queue = self.after = None
        self.wrong_pax = {}            # passenger -> wrong fields now (the channels' own count), set by the session
        self.now = None

    # ------------------------------------------------------------ the SimulatedHumans interface
    def ask(self, agent, rec_id, proposal, role, now, rule_check_ok: bool):
        self.now = now
        if not rule_check_ok or now < self.live_from:
            return self.sim.ask(agent, rec_id, proposal, role, now, rule_check_ok)
        if role == self.duty_role:                       # an escalation reached the duty manager (the queue's own path)
            return self._duty_ask(agent, rec_id, proposal, now)
        action = proposal.get("action")
        item = self.queue.by_rec.get((agent, rec_id)) if self.queue is not None else None
        ifacts = dict(item.facts) if item is not None else {}
        target, flights, pids = self.tracer.facts(agent, action, proposal, ifacts)
        q = Question(rec_id, agent, action, role, now, None, bool(proposal.get("urgent")), proposal)
        q.actor = f"human:{role}"
        lq = self._new(q, item, target, flights, pids, now)
        key = key_of(agent, action, target_of(target))
        if key in self.final:                            # the person already said no to exactly this, for good
            lq.status = "refused_rejected_before"
            lq.answer = {"decision": "rejected", "by": "human:" + role, "t": hhmm(now), "auto": True,
                         "reason": "already rejected for good; not asked again"}
            self.ready.append((q, "rejected", {"reason": lq.answer["reason"], "auto": True}))
            self.events.append(("system:live", "proposal_not_asked_rejected_before",
                                {"question_id": lq.id, "agent": agent, "action": action, "recommendation": rec_id,
                                 "target": target_of(target)}, now))
        return q

    def withdraw(self, agent, rec_id):
        self.sim.withdraw(agent, rec_id)
        for dq in self.duty:
            if dq.agent == agent and dq.rec_id == rec_id and dq.status == "waiting":
                dq.status = "withdrawn"
        for lq in self.questions:
            if lq.agent == agent and lq.rec_id == rec_id and lq.status in OPEN:
                lq.status = "withdrawn"
                self.dirty.add(lq.id)

    def due(self, now):
        out = list(self.sim.due(now))
        for q, decision, extra in self.ready:
            if q.status == "withdrawn":
                continue
            q.status = "answered"
            q.answer = {"role": q.role, "answered_at": now, "waited_min": round((now - q.asked_at).total_seconds() / 60, 1),
                        "decision": decision, **extra}
            self._fill(q, now)
            out.append(q)
        self.ready = []
        for dq in self.duty:
            if dq.status != "waiting" or dq.due_at > now:
                continue
            dq.status = "answered"
            dq.answer = {"role": dq.role, "answered_at": now, "waited_min": round((now - dq.asked_at).total_seconds() / 60, 1),
                         "decision": "approved"}
            self._fill(dq, now)
            for lq in self.questions:
                if lq.agent == dq.agent and lq.rec_id == dq.rec_id and (lq.status in OPEN or lq.status == "handed_off"):
                    lq.status = "answered_by_duty_manager"
                    lq.answered_at = now
                    lq.answer = {"decision": dq.answer["decision"], "by": dq.actor, "t": hhmm(now),
                                 "handed_off": lq.handed_off}
                    self.traces.append((lq.id, "answered", self.tracer.trace(lq, now, "answered")))
                    self.dirty.add(lq.id)
            out.append(dq)
        return out

    def summary(self) -> dict:
        return {**self.sim.summary(), "live": self.decisions()}

    # ------------------------------------------------------------ helpers
    def _fill(self, q, now):
        if q.action == "resolve_source_conflict" and q.answer["decision"] == "approved":
            q.answer["conflicts"] = self.sim._conflicts(q, now)   # the person confirms what they can see (as the simulated one)
        if q.action == "escalate_channel_problem" and q.answer["decision"] == "approved":
            q.answer["decision"] = "acknowledged"

    def _duty_ask(self, agent, rec_id, proposal, now):
        if proposal.get("human_only"):                   # a human-only decision: the duty manager never decides it for you
            return Question(rec_id, agent, proposal.get("action"), self.duty_role, now, None, False, proposal, status="not_handled")
        urgent = bool(proposal.get("urgent"))
        lo, hi = self.sim.s["response_min"][self.duty_role]["urgent" if urgent else "normal"]
        dq = Question(rec_id, agent, proposal.get("action"), self.duty_role, now, now + timedelta(minutes=self.rng.randint(lo, hi)),
                      urgent, proposal)
        dq.actor = f"simulated_human:{self.duty_role}"
        self.duty.append(dq)
        return dq

    def _new(self, q, item, target, flights, pids, now, **over):
        lq = LiveQuestion(len(self.questions) + 1, q.agent, q.rec_id, q.action, q.role, now,
                          item.deadline if item is not None else now + timedelta(minutes=30), q.urgent, q.proposal, q,
                          target, flights, pids, self.tracer.title(q.agent, q.action, q.proposal, item.facts if item else {}),
                          pax=item.pax if item is not None else len(pids), **over)
        lq.safety_legal = self.tracer.safety_legal(q.proposal, self.s)
        lq.why = self.tracer.why(lq, now)
        lq.consequence = lq.consequence or self.consequence(lq)
        lq.base_group = lq.group_key = f"{q.agent}:{q.action}:{minute(now)}"
        self.questions.append(lq)
        self.by_id[lq.id] = lq
        self.dirty.add(lq.id)
        self.refresh(now)
        trace = self.tracer.trace(lq, now, "asked")
        trace["preview"] = self.after.preview(lq, now) if self.after is not None else {"changes": [], "destinations": []}
        self.traces.append((lq.id, "asked", trace))
        return lq

    def consequence(self, lq):
        c = self.s["consequences"]
        p = lq.proposal or {}
        who = "pilots" if p.get("crew_role") == "pilot" else "flight attendants"
        key = lq.action
        if key == "cancel_flight" and not self.alternative(lq):
            key = "cancel_flight_no_alternative"
        text = c.get(key) or (c["human_only"] if p.get("human_only") else c["default"])
        return text.format(flight=lq.target.get("flight_id") or ", ".join(lq.flights) or "the flight",
                           n=len(lq.passenger_ids) or lq.pax, who=who, crew=lq.target.get("crew_id") or "")

    def alternative(self, lq):
        """Another plan the agent itself has for the same target (never invented): a crew action's next legal option, or for a
        cancellation the delay the cancellation guard compared it with (unless crew or aircraft legality leaves none)."""
        if lq.agent != "network_recovery":
            return None
        if lq.action in CREW_ACTS:
            gone = {k[2] for k in self.final if k[0] == "network_recovery" and k[1] in CREW_ACTS
                    and dict(k[2]).get("flight_id") == lq.target.get("flight_id")}
            gone = {dict(g).get("crew_id") for g in gone} | {lq.target.get("crew_id")}
            for o in lq.why.get("options_considered") or []:
                crew = o["option"].split()[-1] if o.get("option") else None
                if o.get("why_not", "").startswith("legal") and crew not in gone:
                    return o["option"]
            return None
        if lq.action == "cancel_flight":
            guard = ((lq.proposal or {}).get("option") or {}).get("cancellation") or {}
            return None if guard.get("no_alternative") else (guard.get("alternative") or "delay")
        return None

    def blockers(self, lq):
        return [q for q in self.questions if q is not lq and q.status in OPEN and lq.id in q.depends_on]

    def refresh(self, now):
        """Each minute: snoozes that ran out, escalations, deadlines, dependencies, groups and priority."""
        pr = self.s["priority"]
        open_ = [q for q in self.questions if q.status in OPEN]
        for q in open_:
            item = self.queue.by_rec.get((q.agent, q.rec_id)) if self.queue is not None else None
            if item is not None:
                q.escalated = bool(item.alerted)
                q.deadline_at = item.deadline
            if q.status == "snoozed" and q.snoozed_until is not None and q.snoozed_until <= now:
                q.status = "waiting"
                self.dirty.add(q.id)
            rules = self.s["depends_on"].get(q.action, [])
            built = self.tracer.built_on(q, now)
            deps = [p.id for p in open_ if p is not q and (
                (p.action in rules and set(p.flights) & set(q.flights))
                or (p.agent == "network_recovery" and p.rec_id in built))]
            if deps != q.depends_on:
                q.depends_on = deps
                self.dirty.add(q.id)
            left = max(0.0, (q.deadline_at - now).total_seconds() / 60)
            h = pr["deadline_horizon_min"]
            q.priority_score = round(pr["safety_legal"] * bool(q.safety_legal) + pr["deadline"] * (1 - min(left, h) / h)
                                     + pr["passengers"] * min(q.pax, pr["passengers_cap"]) / pr["passengers_cap"], 3)
            q.wrong_now_pax = sum(1 for p in q.passenger_ids if p in self.wrong_pax) if q.agent == "communication" else 0
        for q in open_:                                  # a group never holds two questions that depend on each other
            same = [p for p in open_ if p.base_group == q.base_group and p is not q]
            q.group_key = q.base_group + (f":{q.id}" if any(q.id in p.depends_on or p.id in q.depends_on for p in same) else "")

    # ------------------------------------------------------------ the user's answers
    def decide(self, ids, action, now, reason=None, minutes=None):
        results = []
        for qid in ids:
            lq = self.by_id.get(qid)
            try:
                if lq is None:
                    raise DecideError(404, f"no question {qid}")
                if lq.status not in OPEN:
                    raise DecideError(409, f"question {qid} is already {lq.status.replace('_', ' ')}")
                getattr(self, "_" + action)(lq, now, reason, minutes)
                results.append({"id": qid, "status": lq.status, "error": None})
            except DecideError as e:
                results.append({"id": qid, "status": lq.status if lq else None, "error": e.message, "code": e.code,
                                "blocked_by": [{"id": b.id, "title": b.title} for b in e.blocked_by]})
        return results

    def _event(self, lq, kind, now, **extra):
        payload = {"question_id": lq.id, "agent": lq.agent, "action": lq.action, "recommendation": lq.rec_id,
                   "target": target_of(lq.target), "role": lq.role, **extra}
        self.events.append((f"human:{lq.role}", kind, payload, now))
        self.dirty.add(lq.id)

    def _close(self, lq, decision, now, reason=None):
        lq.answered_at = now
        lq.answer = {"decision": decision, "by": f"human:{lq.role}", "t": hhmm(now), "reason": reason}
        self.traces.append((lq.id, "answered", self.tracer.trace(lq, now, "answered")))

    def _approve(self, lq, now, reason, minutes):
        lq.status = "approved"
        self._close(lq, "approved", now, reason)
        self.ready.append((lq.question, "approved", {}))
        self._event(lq, "human_approved", now)
        self._human_only_record(lq, "approved", now, reason)

    def _human_only_record(self, lq, answer, now, reason=None):
        """A human-only decision: your answer IS the decision. It is recorded and published (decisions feed) for whoever reads
        it; nothing in the co-pilot carries it out."""
        p = lq.proposal or {}
        if not p.get("human_only"):
            return
        self._event(lq, "human_decision_recorded", now, decision_id=(p.get("decision_ids") or [None])[0], answer=answer,
                    decided_by_role=lq.role, flight_id=p.get("flight_id"), crew_id=p.get("crew_id"),
                    answered_at=now.isoformat(), reason=reason)

    # ------------------------------------------------------------ human-only decisions (live mode only)
    def human_only(self, agent, payloads, now):
        """An agent handed a human-only decision to a person (ask_human: human_decision_needed). In live mode the user is that
        person: it becomes a queue item for the right role, rule-checked like everything else. The rule check's "the co-pilot
        can only show facts" block is expected here; any other block keeps it from the user (recorded, never shown)."""
        if now < self.live_from or self.queue is None:
            return
        from backend.contracts.decisions import load_decisions
        from backend.execution.executor import check_only
        cat = getattr(self, "_catalog", None) or load_decisions()
        self._catalog = cat
        for p in payloads:
            d = cat.get(p.get("decision_id")) or {}
            if not d:
                continue
            facts = p.get("facts") or {}
            role = (p.get("decided_by") or {}).get("role") or (d.get("decided_by") or {}).get("role")
            role = self.s["human_only"]["roles"].get(role, role)
            self.human_seq = getattr(self, "human_seq", 0) - 1        # its own ids (negative): never an agent's recommendation
            rung = d.get("safety_rung")
            flight = facts.get("flight_id")
            prop = {"id": self.human_seq, "agent": agent, "action": d["action"], "decision_ids": [d["id"]], "data_as_of": now,
                    "requested_level": "human_only", "human_only": True, "title": d["question"] + (f" ({flight})" if flight else ""),
                    "flight_id": flight, "crew_id": facts.get("crew_id"), "facts": facts, "reason": p.get("reason"),
                    "safety_rung": int(rung) if str(rung).isdigit() else None}
            verdict = check_only(prop, {"now": now, "flights": {}})
            expected = set(self.s["human_only"]["expected_blocks"]) | {f"action_{d['action']}"}
            ok = verdict["result"] == "pass" or set(verdict["rule_ids"]) <= expected
            prop["rule_check"] = {"ok": ok, "rules": list(verdict["rule_ids"]),
                                  "text": "; ".join(verdict["reasons"]) or "passed every rule it was checked against"}
            self.events.append(("system:live", "human_only_question", {"agent": agent, "decision_id": d["id"], "role": role,
                                                                       "recommendation": self.human_seq, "rule_check_ok": ok,
                                                                       "flight_id": flight}, now))
            self.queue.ask(agent, self.human_seq, prop, role, now, ok)

    def _reject(self, lq, now, reason, minutes):
        reason = (reason or "").strip()
        if not reason:
            raise DecideError(422, "a reason is required to reject")
        key = key_of(lq.agent, lq.action, target_of(lq.target))
        alt = None if lq.last_option else self.alternative(lq)
        if lq.last_option or alt:                        # final: the second no, or the agent has a different plan
            self.final[key] = self.final.get(key, 0) + 1
            lq.status = "rejected"
            self._close(lq, "rejected", now, reason)
            self.ready.append((lq.question, "rejected", {"reason": reason}))
            self._event(lq, "human_rejected", now, reason=reason, final=True, count=2 if lq.last_option else 1,
                        **({"consequence_applied": lq.consequence} if lq.last_option else {"different_plan": alt}))
            self._human_only_record(lq, "rejected", now, reason)
            return
        lq.status = "rejected_once"                      # no other plan: asked once more, as the last option, with its consequence
        self._close(lq, "rejected", now, reason)
        self._event(lq, "human_rejected", now, reason=reason, final=False, count=1, last_option_next=True,
                    consequence=lq.consequence)
        item = self.queue.by_rec.get((lq.agent, lq.rec_id)) if self.queue is not None else None
        nxt = self._new(lq.question, item, lq.target, lq.flights, lq.passenger_ids, now, last_option=True,
                        consequence=lq.consequence, previous=lq.id)
        nxt.question = lq.question

    def _wait(self, lq, now, reason, minutes):
        if minutes not in self.s["decisions"]["wait_choices_min"]:
            raise DecideError(422, f"wait takes {' or '.join(map(str, self.s['decisions']['wait_choices_min']))} minutes")
        self.refresh(now)
        blocked = self.blockers(lq)
        if blocked:
            raise DecideError(409, "another open question depends on this one", blocked)
        lq.status = "snoozed"
        lq.snoozed_until = now + timedelta(minutes=minutes)
        lq.waited.append({"at": hhmm(now), "minutes": minutes})
        self._event(lq, "human_waited", now, minutes=minutes, until=hhmm(lq.snoozed_until))

    def _hand_off(self, lq, now, reason, minutes):
        if (lq.proposal or {}).get("human_only"):
            raise DecideError(409, "a human-only decision: you are the named person, so it can't be handed to the duty manager")
        lq.status = "handed_off"
        lq.handed_off = True
        self._duty_ask(lq.agent, lq.rec_id, lq.proposal, now)
        self._event(lq, "human_handed_off", now, to_role=self.duty_role, reason=reason)

    # ------------------------------------------------------------ the user's record
    def decisions(self) -> dict:
        qs = self.questions
        answered = [q for q in qs if q.answered_at is not None and q.answer.get("by", "").startswith("human:")
                    and not q.answer.get("auto")]
        mins = [round((q.answered_at - q.asked_at).total_seconds() / 60, 1) for q in answered]
        return {"approved": sum(q.status == "approved" for q in qs),
                "rejected": sum(q.status in ("rejected", "rejected_once") for q in qs),
                "waited": sum(len(q.waited) for q in qs), "handed_off": sum(q.handed_off for q in qs),
                "escalated": sum(1 for q in qs if q.escalated or (q.status == "answered_by_duty_manager" and not q.handed_off)),
                "median_answer_min": median(mins) if mins else 0}

