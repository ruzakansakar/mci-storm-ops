"""Reading the co-pilot's staff ledger: every assignment as it really stood (used by the orchestrator and the measurements)."""
from datetime import datetime, timedelta


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def effective(ledger):
    """Every staff assignment as it really stood: from its start to its end, or to when it was released or replaced; ramp work
    minus any pause while the ramp was closed. -> [{staff_id, role, task, flight_id, passenger_id, from, to, recommendation}]"""
    released = {}
    for r in ledger:
        if r["kind"] == "release":
            released[(r["staff_id"], r["recommendation"])] = _dt(r["at"])
    pauses = {}
    for r in ledger:
        if r["kind"] == "pause":
            pauses.setdefault(r["staff_id"], []).append((_dt(r["from_at"]), _dt(r["to_at"]) if r["to_at"] else _dt(r["from_at"]) + timedelta(hours=24)))
    out = []
    for r in ledger:
        if r["kind"] != "assign":
            continue
        a, b = _dt(r["from_at"]), _dt(r["to_at"])
        rel = released.get((r["staff_id"], r["recommendation"]))
        if rel is not None:
            b = min(b, max(a, rel))
        pieces = [(a, b)]
        if r["role"] == "ramp":
            for x, y in pauses.get(r["staff_id"], []):
                pieces = [p for (s, e) in pieces for p in ((s, min(e, x)), (max(s, y), e)) if p[0] < p[1]]
        for s, e in pieces:
            if s < e:
                out.append({**{k: r.get(k) for k in ("staff_id", "role", "task", "flight_id", "passenger_id", "recommendation",
                                                      "approved_by")}, "from": s, "to": e})
    return out
