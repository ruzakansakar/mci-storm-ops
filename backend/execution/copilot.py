"""Minute-loop integration. Agents receive only published records via AgentContext.

Each simulated minute T: the Situation agent first (on its normal schedule: every few minutes, when a trigger event is delivered,
when a feed goes quiet or comes back), then the Communication agent (detect, read Situation's shared picture, propose fixes),
then the executor (rule check, then delivery through the channels' normal delays). The agents never call each other: they meet
only in the SharedStore, through their own contexts, under the shared-state rules (backend/contracts/shared_state.py).
"""
from copy import deepcopy
from datetime import datetime, timedelta

from backend.agents.communication.detect import Memory, load_settings as communication_settings, scan
from backend.agents.communication.fix import FixMemory, propose_fixes
from backend.agents.communication.situation_view import SituationView, read_situation
from backend.contracts.buffered import BufferedContext, SharedStore
from backend.execution.executor import execute


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


class Copilot:
    def __init__(self, now, schedule, passengers, feeds=None, situation_schedule=None, system_health=None, situation_until=None):
        self.store = SharedStore()
        self.context = BufferedContext(now, schedule, passengers, feeds, store=self.store)
        self.situation = SituationRunner(self.store, situation_schedule or schedule, system_health or [], self.context.feeds,
                                         situation_until)
        self.settings = communication_settings()
        self.detection = Memory()
        self.fixes = FixMemory()
        self.view = SituationView()           # what Communication knows from Situation (its working context)
        self.event_id = 0
        self.executions = []
        self.proposals = []

    def _drain(self, ctx, agent):
        outputs, ctx.outputs = ctx.outputs, []
        audits, ctx.audit = ctx.audit, []
        return [(agent, k, p) for k, p in outputs], [{**a, "agent": agent} for a in audits]

    def tick(self, now, published, deliver):
        for event in published:
            self.event_id += 1
            self.store.events.append({**deepcopy(event), "id": self.event_id})
        # 1. Situation (its own schedule)
        self.situation.tick(now)
        outputs, audits = self._drain(self.situation.context, "situation")
        # 2. Communication
        ctx = self.context
        ctx.now = now
        result = scan(ctx, self.detection, write=False)
        read_situation(ctx, self.view, self.settings)
        proposals = propose_fixes(ctx, result, self.fixes, self.view, self.settings)
        st = result["state"]
        if proposals:
            # 3. the executor: the rule check, then delivery (never a truth change)
            trip_flights = {pid: set(row["itinerary"]) for pid, row in st.truth.items()}
            for (pid, _), shown in st.shown.items():
                trip_flights.setdefault(pid, set()).update(shown["itinerary"])
            world = {"now": now, "confirmed_trips": {pid: {k: serial(v) for k, v in row.items() if k not in ("at", "event")}
                                                       for pid, row in st.truth.items()},
                     "trip_versions": {pid: row["at"].isoformat() for pid, row in st.truth.items()},
                     "trip_flights": trip_flights,
                     "truth_feed_fresh": ctx.feed_age_sec("reservations") <= ctx.contract.max_data_age_sec,
                     "flight_feed_fresh": ctx.feed_age_sec("flight_ops") <= ctx.contract.max_data_age_sec,
                     # source conflicts: Situation's latest shared picture (if Situation is down, the last real one; fixes
                     # carry on because they only re-show confirmed truth)
                     "conflicts_known": True, "open_conflicts": set(self.view.conflicts),
                     "conflicts_from": f"situation version {self.view.version}" if self.view.version else "no situation version yet",
                     "contact_types": {p["id"]: ctx.contact_channel_types(p["id"]) for p in ctx.rows["passengers"]},
                     "screens": ctx.passenger_screens()}

            def record(kind, payload):
                ctx.outputs.append((kind, serial(payload)))
                ctx._audit_row(kind, "recommendation", payload["proposal"]["id"], None, serial(payload))

            for proposal in proposals:
                outcome = execute(proposal, world, deliver, record)
                saved = ctx.saved[("recommendations", proposal["id"])]
                saved["data_as_of"] = now
                saved["rule_check_ok"] = outcome["verdict"]["result"] == "pass"
                saved["rule_check_notes"] = outcome["verdict"]["reasons"]
                saved["rule_ids"] = outcome["verdict"]["rule_ids"]
                saved["status"] = "done_automatically" if outcome["status"] == "applied" else "pending"
                self.executions.append(outcome)
                self.proposals.append(serial(proposal))
        out2, aud2 = self._drain(ctx, "communication")
        return outputs + out2, audits + aud2

    def situation_summary(self) -> dict:
        """For the comparison page: what Communication knew from Situation, and why fixes were held."""
        return {"timeline": self.view.timeline, "cleared": self.view.cleared,
                "open_at_end": {f: {**v, "since": v["since"].isoformat()} for f, v in self.view.conflicts.items()},
                "situation_versions": len(self.store.records["situation_assessments"]),
                "situation_runs": len(self.store.heartbeats.get("situation", [])), "situation_errors": len(self.situation.errors)}
