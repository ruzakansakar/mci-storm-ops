"""Incidents (Phase 5a). Only the orchestrator writes incidents.

From Situation's shared picture (read through the orchestrator's AgentContext, whitelisted fields):
  A04  open an incident when Situation reports something abnormal; a report of the SAME type is part of the open incident; a
       different type is a second incident (unless the open one is only minutes old and was 'unknown': then it is reclassified);
  T05  when two are open, rank them (safety first, config/orchestrator.yaml incidents.rank) and ask the station manager to
       confirm the order;
  A10  declare recovery when Situation says recovery/normal, or when an incident's type has not been reported for a while
       (network operations control approves);
  Q07  close it a while after recovery once nothing for it is still waiting in the queue (station manager approves); closing
       deletes the temporary working notes (AgentContext.close_incident) and keeps the audit log.
"""
from dataclasses import dataclass, field
from datetime import timedelta


@dataclass
class Incident:
    id: int
    kind: str
    opened_at: object
    status: str = "open"                 # open | monitoring (recovery declared) | resolved
    last_seen: object = None             # last time Situation reported this type as abnormal
    recovery_at: object = None
    resolved_at: object = None
    rank: int = 0
    row_id: int | None = None          # its row in the orchestrator's incidents writes
    asked: dict = field(default_factory=dict)   # decision -> recommendation id
    situation_versions: list = field(default_factory=list)


class Incidents:
    def __init__(self, ctx, settings):
        self.ctx, self.s = ctx, settings["incidents"]
        self.all: list[Incident] = []
        self.ranked_pairs = set()

    def open(self):
        return [i for i in self.all if i.status != "resolved"]

    def primary(self):
        """The incident new work belongs to: the highest ranked open one (T05: safety first)."""
        o = self.open()
        return min(o, key=lambda i: (i.rank, i.opened_at)).id if o else None

    def _rank(self, kind):
        order = self.s["rank"]
        return order.index(kind) if kind in order else len(order)

    def _row(self, i, **extra):
        return {"kind": i.kind, "station": self.s["station"], "title": f"{i.kind} at {self.s['station']}",
                "likely_cause": i.kind, "status": {"monitoring": "monitoring"}.get(i.status, i.status),
                "opened_at": i.opened_at, "resolved_at": i.resolved_at, "opened_by": "orchestrator", **extra}

    def update(self, now, pic, propose):
        """pic: Situation's projected picture (or None). propose(decision, action, title, why, incident, role_note) puts the
        orchestrator's own proposal through the rule check and into the queue. Returns the events to record."""
        out = []
        facts, sus = (pic or {}).get("facts") or {}, (pic or {}).get("suspected") or {}
        status = facts.get("status")
        kind = (sus.get("type") or {}).get("value") or "unknown"
        # Situation's main suspected cause, and any other cause it suspects on its own signals (a possible second incident)
        kinds = [kind] + [k for k in ((facts.get("impact") or {}).get("other_suspected_causes") or []) if k != kind]
        if pic is not None:
            for i in self.open():
                if pic["version"] not in i.situation_versions and (i.status == "open" or i.kind == kind):
                    i.situation_versions.append(pic["version"])
        for k in (kinds if status == "abnormal" else []):
            out += self._seen(now, pic, k, propose)
        for i in self.open():
            quiet = i.last_seen is not None and now - i.last_seen >= timedelta(minutes=self.s["quiet_before_recovery_min"])
            gone = status == "abnormal" and i.kind not in kinds and quiet
            if i.status == "open" and (status in ("recovery", "normal") or gone):
                if "A10" not in i.asked:
                    i.asked["A10"] = propose("A10", "declare_recovery", f"Recovery: {i.kind} incident #{i.id}",
                                             f"Situation now reports {status}" + (f" without {i.kind}" if gone else ""), i)
                    out.append(("incident_recovery_proposed", {"incident_id": i.id, "situation_status": status}))
        return out

    def _seen(self, now, pic, kind, propose):
        """A04 for one suspected cause: part of an open incident, a reclassification of a young unknown one, or a new one."""
        out = []
        if True:
            same = next((i for i in self.open() if i.kind == kind), None)
            young = next((i for i in self.open() if i.kind in ("unknown", "other") and
                          now - i.opened_at <= timedelta(minutes=self.s["reclassify_within_min"])), None)
            if same is not None:
                same.last_seen = now
                if same.status == "monitoring":                     # it flared up again
                    same.status, same.recovery_at = "open", None
                    out.append(("incident_updated", {"incident_id": same.id, "status": "open", "why": "abnormal again"}))
            elif young is not None:
                young.kind, young.last_seen, young.rank = kind, now, self._rank(kind)
                self.ctx.write("incidents", self._row(young), mode="update", row_id=young.row_id)
                out.append(("incident_updated", {"incident_id": young.id, "kind": kind, "why": "A04: reclassified"}))
            else:
                rid = self.ctx.write("incidents", self._row(Incident(0, kind, now)))
                inc = Incident(len(self.all) + 1, kind, now, last_seen=now, rank=self._rank(kind), row_id=rid,
                               situation_versions=[pic["version"]] if pic else [])
                self.all.append(inc)
                others = [i for i in self.open() if i.id != inc.id]
                out.append(("incident_opened", {"incident_id": inc.id, "kind": kind, "situation_version": (pic or {}).get("version"),
                                                "decision": "A04", "new": True, "also_open": [i.id for i in others]}))
                if others:                                           # T05: two at once: safety first
                    order = sorted(self.open(), key=lambda i: (i.rank, i.opened_at))
                    out.append(("incident_ranked", {"order": [i.id for i in order], "kinds": [i.kind for i in order],
                                                    "decision": "T05", "rule": "safety first"}))
                    propose("T05", "set_incident_priority", f"Two incidents: {' before '.join(i.kind for i in order)}",
                            "safety first (security, airport, weather, IT, FAA programs, crews)", inc)
        return out

    def approved(self, decision, incident_id, now, close_contexts):
        i = next((x for x in self.all if x.id == incident_id), None)
        if i is None or i.status == "resolved":
            return []
        if decision == "A10" and i.status == "open":
            i.status, i.recovery_at = "monitoring", now
            self.ctx.write("incidents", self._row(i), mode="update", row_id=i.row_id)
            return [("incident_updated", {"incident_id": i.id, "status": "monitoring", "decision": "A10"})]
        if decision == "Q07" and i.status == "monitoring":
            i.status, i.resolved_at = "resolved", now
            self.ctx.write("incidents", self._row(i), mode="update", row_id=i.row_id)
            deleted = close_contexts()
            return [("incident_closed", {"incident_id": i.id, "decision": "Q07", "temporary_notes_deleted": deleted,
                                         "audit_log": "kept"})]
        return []

    def ready_to_close(self, now, waiting_for):
        """Q07: a while after recovery, with nothing for this incident still waiting in the queue."""
        out = []
        for i in self.open():
            if i.status == "monitoring" and "Q07" not in i.asked and now - i.recovery_at >= timedelta(minutes=self.s["close_after_recovery_min"]) \
                    and not waiting_for(i.id):
                out.append(i)
        return out
