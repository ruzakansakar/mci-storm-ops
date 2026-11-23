"""Minute-loop integration. Agents receive only published records via AgentContext.

Each simulated minute T: the Situation agent first (on its normal schedule: every few minutes, when a trigger event is delivered,
when a feed goes quiet or comes back), then the Communication agent (detect, read Situation's shared picture, propose fixes),
then the executor (rule check, then delivery through the channels' normal delays), then (co-pilot simulation only)
the simulated people who answer questions and approvals (backend/execution/simulated_human.py). The agents never call each other: they meet
only in the SharedStore, through their own contexts, under the shared-state rules (backend/contracts/shared_state.py).
"""
from copy import deepcopy
from datetime import datetime, timedelta

from backend.agents.communication.detect import Memory, load_settings as communication_settings, scan
from backend.agents.communication.fix import FixMemory, propose_fixes
from backend.agents.communication.messages import MessageMemory, check_world, plan_messages, sent, withdraw
from backend.agents.communication.situation_view import SituationView, escalate_holds, held_fields, read_situation
from backend.contracts.buffered import BufferedContext, SharedStore
from backend.execution.executor import check_only, execute


def serial(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [serial(v) for v in value]
    return value


class SituationRunner:
    """Runs the production Situation agent in the minute loop, on exactly the schedule of its standalone replay
    (backend/agents/situation/run.py wake_times): the same moments, the same inputs, so the same versions."""

    def __init__(self, store, schedule, system_health, feeds, until=None):
        from backend.agents.situation.assess import Memory as SituationMemory, load_settings
        from backend.agents.situation.state_at import parse_clock
        self.s = load_settings()
        self.context = BufferedContext(None, feeds=feeds, agent="situation", store=store,
                                       rows={"flights": deepcopy(schedule), "system_health": deepcopy(system_health)})
        self.memory = SituationMemory()
        r = self.s["run"]
        self.start, self.end = parse_clock(r["day_start"]), parse_clock(r["day_end"])
        self.every = timedelta(minutes=r["every_min"])
        self.triggers = set(r["trigger_events"])
        watchdog = timedelta(seconds=int(r["watchdog_after_sec"]))
        self.feed_times = {x for at in self.context.feeds.change_times() for x in (at, at + watchdog)}
        self.until = until                    # tests only: Situation stops running after this time (a missing run)
        self.errors = []

    def due(self, now) -> bool:
        if not self.start <= now <= self.end or (self.until is not None and now > self.until):
            return False
        if (now - self.start) % self.every == timedelta(0) or now in self.feed_times:
            return True
        return any((e.get("published_at") or e["occurred_at"]) == now and e["type"] in self.triggers for e in self.context.events)

    def tick(self, now):
        from backend.agents.situation.assess import assess
        from backend.agents.situation.run import _restore, _save
        if not self.due(now):
            return
        ctx = self.context
        ctx.now = now
        saved = _save(self.memory)
        try:
            assess(ctx, self.memory, settings=self.s)
        except Exception as err:              # as the standalone replay: no partial assessment, one agent_error with the reason
            _restore(self.memory, saved)
            ctx.record_event("agent_error", {"at": now.isoformat(), "error": f"{type(err).__name__}: {err}"})
            self.errors.append((now, str(err)))


class NetworkRunner:
    """The Network Recovery agent in the minute loop (Phase 4d, ADVISORY): after Situation, on its own schedule (every few
    minutes and when a trigger event is delivered). Each proposal goes through the rule check; the result (pass or block, and
    who would have to approve it) is recorded, and NOTHING is applied."""

    def __init__(self, store, rows, feeds):
        from backend.agents.network_recovery.plan import Memory, load_settings
        from backend.agents.situation.state_at import parse_clock
        self.s = load_settings()
        self.context = BufferedContext(None, feeds=feeds, agent="network_recovery", store=store, rows=deepcopy(rows))
        self.memory = Memory()
        r = self.s["run"]
        self.start, self.end = parse_clock(r["day_start"]), parse_clock(r["day_end"])
        self.every = timedelta(minutes=r["every_min"])
        self.triggers = set(r["trigger_events"])
        self.errors, self.verdicts = [], []

    def due(self, now) -> bool:
        if not self.start <= now <= self.end:
            return False
        if (now - self.start) % self.every == timedelta(0):
            return True
        return any((e.get("published_at") or e["occurred_at"]) == now and e["type"] in self.triggers for e in self.context.events)

    def tick(self, now):
        from backend.agents.network_recovery.plan import plan
        if not self.due(now):
            return
        ctx = self.context
        ctx.now = now
        saved = deepcopy(self.memory)
        try:
            res = plan(ctx, self.memory, self.s)
        except Exception as err:                 # no partial plan; one agent_error with the reason
            self.memory = saved
            ctx.record_event("agent_error", {"at": now.isoformat(), "error": f"{type(err).__name__}: {err}"})
            self.errors.append((now, f"{type(err).__name__}: {err}"))
            return
        world = {"now": now, "flights": {}}
        for p in res["proposals"]:
            verdict = check_only(p, world)
            rec = ctx.saved[("recommendations", p["id"])]
            rec["rule_check_ok"] = verdict["result"] == "pass"
            rec["rule_check_notes"], rec["rule_ids"] = verdict["reasons"], verdict["rule_ids"]
            rec["status"] = "pending" if verdict["result"] == "pass" else "blocked_by_rule_check"
            v = {"proposal": serial({k: x for k, x in p.items() if k != "option"}), "verdict": verdict, "advisory": True,
                 "applied": False, "would_need_approval_by": rec.get("approver_role"), "at": now.isoformat()}
            kind = "rule_check_passed" if verdict["result"] == "pass" else "rule_check_blocked"
            ctx.outputs.append((kind, v))
            ctx._audit_row(kind, "recommendation", p["id"], None, v)          # every pass and block is in the audit log too
            self.verdicts.append(v)


class Copilot:
    def __init__(self, now, schedule, passengers, feeds=None, situation_schedule=None, system_health=None, situation_until=None,
                 stations=None, humans=None, network_rows=None):
        self.store = SharedStore()
        self.context = BufferedContext(now, feeds=feeds, store=self.store,
                                       rows={"flights": deepcopy(schedule), "passengers": deepcopy(passengers),
                                             "stations": deepcopy(stations or [])})
        self.situation = SituationRunner(self.store, situation_schedule or schedule, system_health or [], self.context.feeds,
                                         situation_until)
        self.network = NetworkRunner(self.store, network_rows, self.context.feeds) if network_rows else None
        self.settings = communication_settings()
        self.detection = Memory()
        self.fixes = FixMemory()
        self.view = SituationView()           # what Communication knows from Situation (its working context)
        self.messages = MessageMemory()
        self.humans = humans                  # simulated people (backend/execution/simulated_human.py); None = nobody answers
        self.event_id = 0
        self.executions = []
        self.proposals = []
        self.message_options = {}             # recommendation id -> the message proposal
        self.asked_situation = set()          # Situation recommendation ids already shown to a person
        self.waits = []                       # approval waits (minutes), by kind
        self.voided = []                      # approvals that came too late: the truth had changed

    def _drain(self, ctx, agent):
        outputs, ctx.outputs = ctx.outputs, []
        audits, ctx.audit = ctx.audit, []
        return [(agent, k, p) for k, p in outputs], [{**a, "agent": agent} for a in audits]

    def _publish(self, now, type_, payload):
        """A record a real system would keep (a decision log), published like any other source event."""
        self.event_id += 1
        self.store.events.append({"id": self.event_id, "occurred_at": now, "published_at": None, "type": type_, "station": None,
                                  "payload": deepcopy(payload)})

    def _set_status(self, agent, rec_id, status):
        ctx = self.context if agent == "communication" else self.situation.context
        rec = ctx.saved.get(("recommendations", rec_id))
        if rec is not None:
            rec["status"] = status

    def tick(self, now, published, deliver):
        for event in published:
            self.event_id += 1
            self.store.events.append({**deepcopy(event), "id": self.event_id})
        human_out = []
        # 0. people answer what is due. A conflict answer is published first, so Situation sees it this very minute.
        approved_now = []
        for q in (self.humans.due(now) if self.humans else []):
            actor = f"simulated_human:{q.role}"
            if q.action == "resolve_source_conflict":
                for a in q.answer["conflicts"]:
                    if a.get("repeat"):
                        continue
                    payload = {"decision_id": "A09", "conflict_key": a["conflict_key"], "flight_id": a["flight_id"],
                               "answer_side": a["answer_side"], "decided_by_role": q.role, "answered_at": now.isoformat(),
                               "recommendation": q.rec_id, "asked_by": q.agent, "waited_min": q.answer["waited_min"]}
                    self._publish(now, "human_decision_recorded", payload)
                    human_out.append((actor, "human_decision_recorded", payload))
                self._set_status(q.agent, q.rec_id, "approved")
                self.waits.append(("conflict", q.answer["waited_min"]))
            elif q.action == "escalate_channel_problem":
                human_out.append((actor, "human_acknowledged", {"recommendation": q.rec_id, "agent": q.agent,
                                                                 "waited_min": q.answer["waited_min"]}))
                self._set_status(q.agent, q.rec_id, "approved")
            else:
                approved_now.append(q)
        # 1. Situation (its own schedule)
        self.situation.tick(now)
        outputs, audits = self._drain(self.situation.context, "situation")
        # 1b. Network Recovery (advisory: proposals are rule-checked and recorded, never applied)
        if self.network:
            self.network.tick(now)
            o, a = self._drain(self.network.context, "network_recovery")
            outputs, audits = outputs + o, audits + a
        # 2. Communication: detect, read Situation's picture, plan fixes and messages
        ctx = self.context
        ctx.now = now
        result = scan(ctx, self.detection, write=False)
        read_situation(ctx, self.view, self.settings)
        proposals = propose_fixes(ctx, result, self.fixes, self.view, self.settings)
        plan = plan_messages(ctx, result, self.view, self.messages, self.settings, self.detection.screens or {})
        questions = escalate_holds(ctx, self.view, self.settings)
        st = result["state"]
        world = self._world(ctx, st, now)

        def record(kind, payload):
            ctx.outputs.append((kind, serial(payload)))
            ctx._audit_row(kind, "recommendation", payload["proposal"]["id"], None, serial(payload))

        # 3. the executor: the rule check, then delivery (never a truth change)
        for proposal in proposals:
            outcome = execute(proposal, world, deliver, record)
            self._after(ctx, proposal, outcome, now)
            self.executions.append(outcome)
            self.proposals.append(serial(proposal))
        for rec in plan["withdrawn"]:
            withdraw(self.messages, rec, "the truth changed while it waited")
            if self.humans:
                self.humans.withdraw("communication", rec)
            self._set_status("communication", rec, "expired")
        for option in plan["proposals"]:
            self.message_options[option["id"]] = option
            outcome = execute(option, world, deliver, record)
            self._after(ctx, option, outcome, now)
            if outcome["status"] == "applied":
                sent(self.messages, option, now)
            elif outcome["verdict"]["result"] == "pass" and self.humans:
                self.humans.ask("communication", option["id"], option, "customer_service_supervisor", now, True)
            elif outcome["verdict"]["result"] != "pass":
                withdraw(self.messages, option["id"], "blocked by the rule check")
                if self.humans:
                    self.humans.ask("communication", option["id"], option, "customer_service_supervisor", now, False)
        for question in questions:
            outcome = execute(question, world, deliver, record)
            self._after(ctx, question, outcome, now)
            if self.humans:
                self.humans.ask("communication", question["id"], question, "network_operations_control", now,
                                outcome["verdict"]["result"] == "pass")
        # Situation's own questions for a person (A09 and the other proposals it makes): shown only after the rule check passed
        sctx = self.situation.context
        for (table, rid), rec in sorted(sctx.saved.items()):
            if table != "recommendations" or rid in self.asked_situation or rec.get("status") != "pending":
                continue
            self.asked_situation.add(rid)
            action = rec.get("_action")
            q = {"agent": "situation", "action": action, "decision_ids": rec["decision_ids"], "data_as_of": now, "id": rid,
                 "options": rec["options"], "requested_level": "needs_approval"}
            verdict = check_only(q, world)
            rec["rule_check_ok"], rec["rule_check_notes"], rec["rule_ids"] = verdict["result"] == "pass", verdict["reasons"], verdict["rule_ids"]
            if self.humans and rec.get("approver_role"):
                self.humans.ask("situation", rid, q, rec["approver_role"], now, verdict["result"] == "pass")
        # 4. approvals that arrived this minute: checked again against the truth NOW; out of date = void, redrafted next minute
        for q in approved_now:
            option = self.message_options.get(q.rec_id)
            if option is None or self.context.saved.get(("recommendations", q.rec_id), {}).get("status") != "pending":
                continue
            # every claim is checked again against the truth NOW, so the data behind it is as of now
            option = {**option, "data_as_of": now}
            verdict = check_only(option, world)
            actor = f"simulated_human:{q.role}"
            if verdict["result"] != "pass":
                human_out.append((actor, "human_approved", {"recommendation": q.rec_id, "action": q.action, "role": q.role,
                                                            "waited_min": q.answer["waited_min"], "void": verdict["reasons"][:3]}))
                withdraw(self.messages, q.rec_id, "approved too late: the truth had changed; redrafted")
                self._set_status("communication", q.rec_id, "expired")
                self.voided.append(q.rec_id)
                continue
            human_out.append((actor, "human_approved", {"recommendation": q.rec_id, "action": q.action, "role": q.role,
                                                        "waited_min": q.answer["waited_min"]}))
            receipt = deliver(option)
            if isinstance(receipt, dict) and receipt.get("accepted") is False:
                withdraw(self.messages, q.rec_id, f"not delivered: {receipt.get('reason')}")
                self._set_status("communication", q.rec_id, "expired")
                continue
            record("channel_fix_applied" if "messages" not in option else "message_sent",
                   {"proposal": option, "receipt": receipt, "approved_by": q.role, "waited_min": q.answer["waited_min"]})
            sent(self.messages, option, now, approved_by=q.role, waited_min=q.answer["waited_min"])
            self._set_status("communication", q.rec_id, "approved")
            self.waits.append((option["messages"][0]["type"] if option.get("messages") else q.action, q.answer["waited_min"]))
        out2, aud2 = self._drain(ctx, "communication")
        return outputs + out2 + human_out, audits + aud2

    def _after(self, ctx, proposal, outcome, now):
        saved = ctx.saved[("recommendations", proposal["id"])]
        saved["data_as_of"] = now
        saved["rule_check_ok"] = outcome["verdict"]["result"] == "pass"
        saved["rule_check_notes"] = outcome["verdict"]["reasons"]
        saved["rule_ids"] = outcome["verdict"]["rule_ids"]
        saved["status"] = "done_automatically" if outcome["status"] == "applied" else \
            "pending" if outcome["verdict"]["result"] == "pass" else "blocked_by_rule_check"

    def _world(self, ctx, st, now):
        trip_flights = {pid: set(row["itinerary"]) for pid, row in st.truth.items()}
        for (pid, _), shown in st.shown.items():
            trip_flights.setdefault(pid, set()).update(shown["itinerary"])
        held = {}
        for pid, fl in trip_flights.items():
            h = held_fields(self.view, fl, self.settings)
            if h:
                held[pid] = sorted(h[0])
        names = {}
        if ctx.scope:
            names = {r["id"]: (r["full_name"] or "").split(" ")[0] for r in
                     ctx.read("passengers", ["id", "full_name"], where={"id": sorted(ctx.scope)})}
        world = check_world(st, self.view, self.messages, names, ctx.scope)
        world.update({"now": now, "confirmed_trips": {pid: {k: serial(v) for k, v in row.items() if k not in ("at", "event")}
                                                      for pid, row in st.truth.items()},
                      "trip_flights": trip_flights,
                      "truth_feed_fresh": ctx.feed_age_sec("reservations") <= ctx.contract.max_data_age_sec,
                      "flight_feed_fresh": ctx.feed_age_sec("flight_ops") <= ctx.contract.max_data_age_sec,
                      # source conflicts: Situation's latest shared picture (if Situation is down, the last real one; fixes
                      # carry on because they only re-show confirmed truth). Only the conflicted details are held.
                      "conflicts_known": True, "open_conflicts": set(self.view.conflicts), "conflict_held_fields": held,
                      "conflicts_from": f"situation version {self.view.version}" if self.view.version else "no situation version yet",
                      "contact_types": {p["id"]: ctx.contact_channel_types(p["id"]) for p in ctx.rows["passengers"]},
                      "screens": ctx.passenger_screens()})
        return world

    def situation_summary(self) -> dict:
        """For the comparison page: what Communication knew from Situation, and why fixes were held."""
        return {"timeline": self.view.timeline, "cleared": self.view.cleared,
                "open_at_end": {f: {**{k: v for k, v in v.items() if k != "since"}, "since": v["since"].isoformat()}
                                for f, v in self.view.conflicts.items()},
                "situation_versions": len(self.store.records["situation_assessments"]),
                "situation_runs": len(self.store.heartbeats.get("situation", [])), "situation_errors": len(self.situation.errors)}
