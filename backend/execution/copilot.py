"""Minute-loop integration. Agents receive only published records via AgentContext."""
from copy import deepcopy
from datetime import datetime

from backend.agents.communication.detect import Memory, scan
from backend.agents.communication.fix import FixMemory, conflicts_from_events, propose_fixes
from backend.contracts.buffered import BufferedContext
from backend.execution.executor import execute


def serial(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [serial(v) for v in value]
    return value


class Copilot:
    def __init__(self, now, schedule, passengers, feeds=None):
        self.context = BufferedContext(now, schedule, passengers, feeds)
        self.detection = Memory()
        self.fixes = FixMemory()
        self.cursor = 0
        self.event_id = 0
        self.executions = []
        self.proposals = []

    def tick(self, now, published, deliver):
        ctx = self.context
        ctx.now = now
        for event in published:
            self.event_id += 1
            ctx.events.append({**deepcopy(event), "id": self.event_id})
        result = scan(ctx, self.detection, write=False)
        proposals = propose_fixes(ctx, result, self.fixes, self.detection.events.values())
        st = result["state"]
        if not proposals:
            outputs, ctx.outputs = ctx.outputs, []
            audits, ctx.audit = ctx.audit, []
            return outputs, audits
        trip_flights = {pid: set(row["itinerary"]) for pid, row in st.truth.items()}
        for (pid, _), shown in st.shown.items():
            trip_flights.setdefault(pid, set()).update(shown["itinerary"])
        world = {"now": now, "confirmed_trips": {pid: {k: serial(v) for k, v in row.items() if k not in ("at", "event")}
                                                   for pid, row in st.truth.items()},
                 "trip_versions": {pid: row["at"].isoformat() for pid, row in st.truth.items()},
                 "trip_flights": trip_flights,
                 "truth_feed_fresh": ctx.feed_age_sec("reservations") <= ctx.contract.max_data_age_sec,
                 "flight_feed_fresh": ctx.feed_age_sec("flight_ops") <= ctx.contract.max_data_age_sec,
                 "conflicts_known": True, "open_conflicts": conflicts_from_events(self.detection.events.values()),
                 "contact_types": {p["id"]: ctx.contact_channel_types(p["id"]) for p in ctx.rows["passengers"]}}

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
        outputs, ctx.outputs = ctx.outputs, []
        audits, ctx.audit = ctx.audit, []
        return outputs, audits
