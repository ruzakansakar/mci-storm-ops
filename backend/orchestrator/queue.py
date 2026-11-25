"""One human approval queue across all agents (Phase 5a).

Every question an agent (or the orchestrator) has for a person comes here, only after the rule check passed it (a blocked
proposal is recorded as refused and never shown). The queue:

  - orders items by safety rung, then deadline, then passengers affected (most first);
  - merges duplicates (the same agent asking the same thing about the same flight while the first is still open);
  - batches routine approvals of the same kind asked within a window into ONE item (config/orchestrator.yaml queue.batch);
  - escalates an item nobody answered by its deadline to the next role (S06; human_answer_deadline_min in the rules, or the
    decision's own deadline if sooner). Escalation alerts the next role; an answer from the person first asked still counts;
  - counts items per role per hour and flags a role over the workload limit.

The (simulated) people answer from here: due(now) returns the answered questions, one per proposal (a batch or a merged
duplicate is answered once and the answer goes to each of its proposals). The rule check and who may decide are never changed
here: the queue can't approve, can't override a block and can't answer anything itself.
"""
import re
from copy import copy
from dataclasses import dataclass, field
from datetime import timedelta

PRIORITY_NONE = 9


@dataclass
class Item:
    id: int
    agent: str
    rec_id: int
    action: str
    role: str
    asked_at: object
    deadline: object
    rung: int
    pax: int
    decision_ids: list
    facts: dict
    incident_id: int | None = None
    status: str = "waiting"             # waiting | answered | withdrawn | not_handled | refused_blocked
    members: list = field(default_factory=list)    # (agent, rec_id, proposal, asked_at) answered with this item
    kind: str = "single"                # single | batch | conflict
    escalations: list = field(default_factory=list)
    alerted: list = field(default_factory=list)    # roles that now also see it
    answered_at: object = None
    answered_by: str | None = None
    merged: int = 0


def own_deadline_min(decision: dict | None):
    """A decision's own deadline in minutes, when its text says 'within N min' / 'within N h' (else None)."""
    text = (decision or {}).get("deadline") or ""
    m = re.search(r"within (\d+) (min|h)", text)
    if m:
        return int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    return None


def deadline_for(decision: dict | None, now, s: dict):
    """When a person must have answered (Phase 5b): the decision's own deadline in config/decisions.yaml ('within N min',
    'immediately', 'before HH:MM'); a deadline tied to an event we can't time here gets the default (settings)."""
    text = ((decision or {}).get("deadline") or "").strip()
    mins = own_deadline_min(decision)
    if mins is not None:
        return now + timedelta(minutes=mins)
    if text.startswith("immediately"):
        return now + timedelta(minutes=s["immediate_min"])
    m = re.match(r"before (\d{1,2}):(\d{2})", text)
    if m:
        at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if at > now:
            return at
    return now + timedelta(minutes=s["default_deadline_min"])


class Queue:
    def __init__(self, humans, settings: dict, rules: dict, decisions: dict):
        self.humans, self.s, self.decisions = humans, settings["queue"], decisions
        self.answer_min = rules["rule_check"]["human_answer_deadline_min"] if "rule_check" in rules else 10
        self.items: list[Item] = []
        self.by_rec: dict = {}              # (agent, rec_id) -> item
        self.events: list = []              # (kind, payload) for the orchestrator to record
        self.incident_id = None             # the incident new items belong to (set by the orchestrator each minute)
        self.flagged = set()

    # ------------------------------------------------------------ helpers
    def _deadline(self, now, decision_ids):
        """The soonest of the decisions' own deadlines (Phase 5b; routine items are not all 10 minutes)."""
        ds = [deadline_for(self.decisions.get(d), now, self.s["deadlines"]) for d in decision_ids or []]
        return min(ds) if ds else now + timedelta(minutes=self.s["deadlines"]["default_deadline_min"])

    @staticmethod
    def _facts(proposal):
        keep = ("title", "flight_id", "passenger_ids", "hotel_id", "rooms", "gate", "staff_id", "kind", "task", "new_departure")
        out = {k: proposal[k] for k in keep if proposal.get(k) not in (None, [], "")}
        if proposal.get("messages"):
            out["messages"] = len(proposal["messages"])
        return out

    @staticmethod
    def _pax(proposal):
        if proposal.get("passengers_affected") is not None:
            return int(proposal["passengers_affected"])
        if proposal.get("messages"):
            return len({m.get("passenger_id") for m in proposal["messages"]})
        return len(proposal.get("passenger_ids") or [])

    def ordered(self, status=("waiting",)):
        """The queue as a person sees it: safety rung, then deadline, then passengers affected."""
        return sorted((i for i in self.items if i.status in status), key=lambda i: (i.rung, i.deadline, -i.pax, i.id))

    @staticmethod
    def _key(agent, action, proposal):
        """Two questions are duplicates only if everything they ask is the same (all but the id and the time)."""
        import json
        same = {k: v for k, v in proposal.items() if k not in ("id", "data_as_of")}
        return (agent, action, json.dumps(same, sort_keys=True, default=str))

    # ------------------------------------------------------------ the SimulatedHumans interface the agents' runners use
    def ask(self, agent, rec_id, proposal, role, now, rule_check_ok: bool):
        action = proposal.get("action")
        ids = list(proposal.get("decision_ids") or [])
        if not rule_check_ok:                                # a block is recorded, never shown to anyone for approval
            item = self._new(agent, rec_id, action, role, now, ids, proposal, status="refused_blocked")
            if self.humans:
                self.humans.ask(agent, rec_id, proposal, role, now, False)
            return item
        # a batch of routine approvals of the same kind
        if action in (self.s["batch"]["kinds"].get(agent) or []):
            window = timedelta(minutes=self.s["batch"]["window_min"])
            lead = next((i for i in reversed(self.items) if i.kind == "batch" and i.agent == agent and i.action == action
                         and i.role == role and i.status == "waiting" and now - i.asked_at <= window), None)
            if lead is not None:
                lead.members.append((agent, rec_id, proposal, now))
                lead.pax += self._pax(proposal)
                self.by_rec[(agent, rec_id)] = lead
                self.events.append(("queue_batched", {"queue_item": lead.id, "recommendation": rec_id, "agent": agent,
                                                      "action": action, "batch_size": len(lead.members)}))
                return lead
            item = self._new(agent, rec_id, action, role, now, ids, proposal, kind="batch")
        else:
            # a duplicate: the same agent asking the same thing while the first is still open
            key = self._key(agent, action, proposal)
            dup = next((i for i in self.items if i.status == "waiting" and i.kind == "single" and
                        self._key(i.agent, i.action, i.members[0][2]) == key), None)
            if dup is not None:
                dup.members.append((agent, rec_id, proposal, now))
                dup.merged += 1
                self.by_rec[(agent, rec_id)] = dup
                self.events.append(("queue_merged", {"queue_item": dup.id, "recommendation": rec_id, "agent": agent}))
                return dup
            item = self._new(agent, rec_id, action, role, now, ids, proposal)
        q = self.humans.ask(agent, rec_id, proposal, role, now, True) if self.humans else None
        if q is not None and q.status == "not_handled":     # nobody simulated answers it: it waits, and escalates
            item.facts["simulated"] = "no simulated person answers this kind"
        return item

    def _new(self, agent, rec_id, action, role, now, ids, proposal, status="waiting", kind="single"):
        rung = proposal.get("safety_rung")
        item = Item(len(self.items) + 1, agent, rec_id, action, role, now, self._deadline(now, ids),
                    rung if isinstance(rung, int) else PRIORITY_NONE, self._pax(proposal), ids, self._facts(proposal),
                    self.incident_id, status, [(agent, rec_id, proposal, now)], kind)
        self.items.append(item)
        self.by_rec[(agent, rec_id)] = item
        return item

    def add_conflict(self, role, now, conflict, incident_id=None):
        """A cross-agent conflict for a person (logged with both views). No simulated person answers these."""
        item = Item(len(self.items) + 1, "orchestrator", -len(self.items) - 1, "resolve_agent_conflict", role, now,
                    self._deadline(now, []), 1 if conflict.get("safety") else 6, conflict.get("pax", 0), [],
                    {"conflict": conflict["kind"], "detail": conflict["detail"]}, incident_id, kind="conflict")
        self.items.append(item)
        return item

    def withdraw(self, agent, rec_id):
        item = self.by_rec.get((agent, rec_id))
        if item is None:
            return
        item.members = [m for m in item.members if not (m[0] == agent and m[1] == rec_id)]
        lead_gone = item.rec_id == rec_id and item.agent == agent
        if item.members and lead_gone:
            return                                           # the batch still waits for the rest (the person's question stays)
        if not item.members:
            if item.status == "waiting":
                item.status = "withdrawn"
            if self.humans:
                self.humans.withdraw(item.agent, item.rec_id)

    def due(self, now):
        """The answers people gave this minute, one question per proposal (a batch or duplicate answers all of its members)."""
        out = []
        for q in (self.humans.due(now) if self.humans else []):
            item = self.by_rec.get((q.agent, q.rec_id))
            if item is None or item.rec_id != q.rec_id or item.agent != q.agent:
                out.append(q)
                continue
            if item.status == "answered":                    # the other person already answered it (an escalation)
                continue
            item.status, item.answered_at, item.answered_by = "answered", now, q.role
            if q.role != item.role:                          # the duty manager answered an escalated item, for that role
                q = copy(q)
                q.answer = {**q.answer, "answered_by": item.answered_by, "on_behalf_of": item.role}
                q.role = item.role
            if self.humans and len(item.alerted):            # whoever answers first: the other person's question is withdrawn
                self.humans.withdraw(q.agent, q.rec_id)
            for agent, rec_id, proposal, asked in item.members:
                if agent == q.agent and rec_id == q.rec_id:
                    out.append(q)
                    continue
                m = copy(q)
                m.agent, m.rec_id, m.proposal, m.asked_at = agent, rec_id, proposal, asked
                m.answer = {**q.answer, "waited_min": round((now - asked).total_seconds() / 60, 1), "batch_or_merge": item.id}
                out.append(m)
        return out

    def escalate(self, now, chain):
        """S06: an item nobody answered by its deadline goes to the next role (each step once, with a new deadline)."""
        for item in self.items:
            if item.status != "waiting" or now < item.deadline:
                continue
            current = (item.alerted or [item.role])[-1]
            nxt = chain.get(current)
            if nxt is None or nxt in item.alerted:
                continue
            item.alerted.append(nxt)
            item.escalations.append({"at": now.isoformat(), "from": current, "to": nxt})
            item.deadline = now + timedelta(minutes=self.s["deadlines"]["escalated_answer_min"])
            if self.humans and item.kind != "conflict" and nxt == self.s["duty_manager"]:
                agent, rec_id, proposal, _ = item.members[0]          # the duty manager is asked too (same rules: passed only)
                self.humans.ask(agent, rec_id, proposal, nxt, now, True)
            self.events.append(("decision_escalated", {"queue_item": item.id, "agent": item.agent, "recommendation": item.rec_id,
                                                       "action": item.action, "from_role": current, "to_role": nxt,
                                                       "decision_ids": item.decision_ids, "decision": "S06"}))

    def workload(self, now, limit):
        """Queue items per role in the last hour (a batch or merged duplicate counts once); flag a role over the limit."""
        hour = now - timedelta(hours=1)
        counts = {}
        for i in self.items:
            if i.asked_at > hour and i.status != "refused_blocked":
                counts[i.role] = counts.get(i.role, 0) + 1
        for role, n in counts.items():
            key = (role, now.replace(minute=0, second=0, microsecond=0))
            if n > limit and key not in self.flagged:
                self.flagged.add(key)
                self.events.append(("workload_high", {"role": role, "items_last_hour": n, "limit": limit}))
        return counts

    def summary(self):
        by = {}
        for i in self.items:
            by.setdefault(i.status, 0)
            by[i.status] += 1
        return {"items": len(self.items), "by_status": by, "batches": sum(1 for i in self.items if i.kind == "batch" and len(i.members) > 1),
                "batched_proposals": sum(len(i.members) for i in self.items if i.kind == "batch"),
                "merged": sum(i.merged for i in self.items), "escalated": sum(1 for i in self.items if i.escalations),
                "conflict_items": sum(1 for i in self.items if i.kind == "conflict")}
