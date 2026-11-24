"""Simulated people (Phase 4b-3): stand-ins for the named humans who answer the co-pilot's questions and approvals, so the
co-pilot can be tested end to end. Used ONLY in co-pilot mode (the simulator's minute loop); today mode never creates one.

  - Each role answers what config/simulated_humans.yaml says it handles, after a response time drawn by role and urgency.
  - It never sees, let alone approves, a proposal the rule check blocked (G09); a proposal withdrawn meanwhile is not answered.
  - For a conflict (A09) it answers with the side that matches the real truth: it plays a person who can look out of the window,
    so it is given a `window` function onto the simulator's truth. No agent ever gets that.
  - Every answer is an event with actor "simulated_human:<role>", clearly apart from the agents' events.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from random import Random

import yaml

from backend.settings import ROOT


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "simulated_humans.yaml").read_text(encoding="utf-8"))


@dataclass
class Question:
    rec_id: int
    agent: str
    action: str
    role: str
    asked_at: object
    due_at: object
    urgent: bool
    proposal: dict
    status: str = "waiting"             # waiting | answered | withdrawn | not_handled
    answer: dict = field(default_factory=dict)


class SimulatedHumans:
    def __init__(self, rng, window, settings: dict | None = None):
        self.s = settings or load_settings()
        self.rng, self.window = rng, window
        self.network_rng = Random()
        self.network_rng.setstate(rng.getstate())  # new questions must not change existing supervisor response times
        self.questions: list[Question] = []
        self.answered_conflicts: dict = {}      # conflict key -> the answer already given (one answer per conflict)
        self.refused_blocked = 0                # proposals it never saw because the rule check blocked them

    def ask(self, agent, rec_id, proposal, role, now, rule_check_ok: bool):
        """A proposal waiting for a person. Only a proposal the rule check passed ever reaches one."""
        if not rule_check_ok:
            self.refused_blocked += 1
            return None
        action = proposal.get("action")
        if action not in self.s["handles"].get(role, []):
            q = Question(rec_id, agent, action, role, now, None, False, proposal, status="not_handled")
            self.questions.append(q)
            return q
        urgent = bool(proposal.get("urgent"))
        lo, hi = self.s["response_min"][role]["urgent" if urgent else "normal"]
        rng = self.network_rng if agent == "network_recovery" or proposal.get("recovery_message") else self.rng
        q = Question(rec_id, agent, action, role, now, now + timedelta(minutes=rng.randint(lo, hi)), urgent, proposal)
        self.questions.append(q)
        return q

    def withdraw(self, agent, rec_id):
        for q in self.questions:
            if q.agent == agent and q.rec_id == rec_id and q.status == "waiting":
                q.status = "withdrawn"

    def due(self, now) -> list[Question]:
        """The questions a person answers now (their response time has passed)."""
        out = []
        for q in self.questions:
            if q.status == "waiting" and q.due_at <= now:
                q.status = "answered"
                waited = (now - q.asked_at).total_seconds() / 60
                q.answer = {"role": q.role, "answered_at": now, "waited_min": round(waited, 1)}
                if q.action == "resolve_source_conflict":
                    q.answer["conflicts"] = self._conflicts(q, now)
                else:
                    q.answer["decision"] = "acknowledged" if q.action == "escalate_channel_problem" else "approved"
                out.append(q)
        return out

    def _conflicts(self, q, now):
        out = []
        for opt in q.proposal.get("options") or []:
            key = opt.get("conflict_key")
            if not key or key in [c["conflict_key"] for c in out]:
                continue
            if key in self.answered_conflicts:
                out.append({**self.answered_conflicts[key], "repeat": True})
                continue
            side = self.window(key, opt.get("sides") or [], opt.get("flight_ids") or [], now)
            a = {"conflict_key": key, "flight_id": (opt.get("flight_ids") or [None])[0], "answer_side": side}
            self.answered_conflicts[key] = a
            out.append(a)
        return out

    def summary(self) -> dict:
        ans = [q for q in self.questions if q.status == "answered"]
        return {"questions": len(self.questions), "answered": len(ans),
                "withdrawn_before_answer": sum(q.status == "withdrawn" for q in self.questions),
                "not_handled": sum(q.status == "not_handled" for q in self.questions),
                "blocked_never_seen": self.refused_blocked,
                "waits_min": [q.answer["waited_min"] for q in ans],
                "by_role": {r: sum(q.role == r for q in ans) for r in self.s["handles"]}}
