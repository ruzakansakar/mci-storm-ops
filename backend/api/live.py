"""Live human-in-the-loop API (mounted in backend/api/app.py). Field names are fixed: see docs/live_api.md.

The only writes are POST start, control and decide. Every other route only reads: a running session from memory (this process),
an older one (or one that ended with a server restart) from the database, read-only.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from backend.execution.live_human import OPEN, DecideError
from simulator import live as L
from simulator.clock import fmt

router = APIRouter(prefix="/api/live")


def _roles():
    import yaml
    from backend.settings import ROOT
    roles = set()
    for name in ("simulated_humans.yaml", "orchestrator.yaml"):
        doc = yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
        roles |= set((doc.get("handles") or {}).keys())
        chain = ((doc.get("queue") or {}).get("escalation_chain") or {})
        roles |= set(chain.keys()) | set(chain.values())
    from backend.contracts.decisions import load_decisions
    for d in load_decisions().values():
        r = (d.get("decided_by") or {}).get("role")
        if r:
            roles.add(r)
    return roles


ROLES = None


def known_roles():
    global ROLES
    if ROLES is None:
        ROLES = _roles()
    return ROLES


class StartBody(BaseModel):
    seed: int = Field(7, ge=0, le=2 ** 31 - 1)
    scenario: Literal["a31_storm", "random"] = "a31_storm"
    roles: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("roles")
    @classmethod
    def _known(cls, v):
        bad = [r for r in v if r not in known_roles()]
        if bad:
            raise ValueError(f"unknown role(s): {', '.join(bad)}")
        return list(dict.fromkeys(v))


class ControlBody(BaseModel):
    action: Literal["pause", "resume", "speed"]
    speed_idle: float | None = Field(None, gt=0)
    speed_pending: float | None = Field(None, gt=0)


class DecideBody(BaseModel):
    question_ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["approve", "reject", "wait", "hand_off"]
    reason: str = Field("", max_length=500)
    minutes: int | None = None


# ------------------------------------------------------------ helpers
def _db():
    from backend.database import connect
    return connect()


def _row(run_id):
    """The saved session (any session ever started), or None."""
    try:
        with _db() as conn, conn.cursor() as cur:
            L.ensure_tables(conn)
            cur.execute("SELECT run_id, seed, scenario, roles, status, sim_minute, speed_idle, speed_pending, started_at, results "
                        "FROM live_sessions WHERE run_id=%s", (run_id,))
            r = cur.fetchone()
    except Exception:                                    # noqa: BLE001 - no database: only this process's sessions exist
        return None
    if r is None:
        return None
    return dict(zip(("run_id", "seed", "scenario", "roles", "status", "minute", "speed_idle", "speed_pending", "started_at",
                     "results"), r))


def _session(run_id):
    s = L.get(run_id)
    if s is not None:
        return s, None
    row = _row(run_id)
    if row is None:
        raise HTTPException(404, f"No live session called {run_id}.")
    return None, row


def _fail(e: DecideError):
    raise HTTPException(e.code, {"error": e.message, "blocked_by": [{"id": b.id, "title": b.title} for b in e.blocked_by]})


def _saved_questions(run_id):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, group_key, agent, action, role, title, target, asked_minute, deadline_minute, urgent, priority_score, "
                    "status, snoozed_until, depends_on, last_option, consequence, why, answer, escalated, answered_minute "
                    "FROM live_questions WHERE run_id=%s ORDER BY id", (run_id,))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ------------------------------------------------------------ writes
@router.post("/start")
def start(body: StartBody):
    s = L.start(seed=body.seed, scenario=body.scenario, roles=body.roles)
    return {"run_id": s.run_id}


@router.post("/{run_id}/control")
def control(run_id: str, body: ControlBody):
    s, row = _session(run_id)
    if s is None:
        raise HTTPException(409, f"The session is {row['status']} (read-only after it ended or the server restarted).")
    if body.action == "speed" and body.speed_idle is None and body.speed_pending is None:
        raise HTTPException(422, "speed needs speed_idle and/or speed_pending")
    with s.lock:
        try:
            return s.control(body.action, body.speed_idle, body.speed_pending)
        except DecideError as e:
            _fail(e)


@router.post("/{run_id}/decide")
def decide(run_id: str, body: DecideBody):
    s, row = _session(run_id)
    if s is None:
        raise HTTPException(409, f"The session is {row['status']} (read-only after it ended or the server restarted).")
    if body.action == "wait" and body.minutes is None:
        raise HTTPException(422, "wait needs minutes (10 or 15)")
    if body.action == "reject" and not body.reason.strip():
        raise HTTPException(422, "a reason is required to reject")
    with s.lock:
        try:
            results = s.decide(list(dict.fromkeys(body.question_ids)), body.action, body.reason, body.minutes)
        except DecideError as e:
            _fail(e)
    failed = [r for r in results if r["error"]]
    if failed and len(failed) == len(results):            # nothing was decided: the error is the answer
        first = failed[0]
        raise HTTPException(first["code"], {"error": first["error"], "blocked_by": first.get("blocked_by") or [],
                                            "results": [{k: r[k] for k in ("id", "status", "error")} for r in results]})
    return {"ok": not failed, "results": [{"id": r["id"], "status": r["status"], "error": r["error"],
                                           **({"blocked_by": r["blocked_by"]} if r.get("blocked_by") else {})} for r in results]}


# ------------------------------------------------------------ reads
@router.get("/sessions")
def sessions():
    out = {}
    try:
        with _db() as conn, conn.cursor() as cur:
            L.ensure_tables(conn)
            cur.execute("SELECT run_id, started_at, status, roles, sim_minute FROM live_sessions ORDER BY started_at DESC")
            for rid, started, status, roles, minute in cur.fetchall():
                out[rid] = {"run_id": rid, "started_at": started.isoformat(), "status": status, "roles": roles, "minute": minute}
    except Exception:                                     # noqa: BLE001
        pass
    for rid, s in L.SESSIONS.items():
        out[rid] = {"run_id": rid, "started_at": s.started_at.isoformat(), "status": s.status, "roles": s.roles, "minute": s.minute}
    return sorted(out.values(), key=lambda r: r["started_at"], reverse=True)


@router.get("/{run_id}/state")
def state(run_id: str):
    s, row = _session(run_id)
    if s is not None:
        with s.lock:
            s.touch()                                     # the heartbeat: a client is watching
            return s.state()
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT wrong_now, wrong_minutes_total, today_wrong_now, today_wrong_minutes_total, pending, focus "
                    "FROM live_minutes WHERE run_id=%s ORDER BY minute DESC LIMIT 1", (run_id,))
        m = cur.fetchone()
    return {"run_id": run_id, "status": row["status"], "minute": row["minute"], "clock": fmt(row["minute"]),
            "speed_idle": row["speed_idle"], "speed_pending": row["speed_pending"], "roles": row["roles"],
            "pending": m[4] if m else 0, "urgent_pending": 0, "incidents": [],
            "world": {"ground_stop": None, "ramp_open": None, "stop_expected_end": None},
            "truth": {"wrong_now": m[0] if m else 0, "wrong_minutes_total": m[1] if m else 0,
                      "today_wrong_now": m[2] if m else None, "today_wrong_minutes_total": m[3] if m else None},
            "focus": (m[5] if m else None) or {"passenger_id": None, "name": None, "true_status": "", "screens": []}}


@router.get("/{run_id}/queue")
def queue(run_id: str, roles: str | None = Query(None, max_length=1000)):
    s, row = _session(run_id)
    wanted = None if roles in ("", "all") else [r for r in roles.split(",") if r] if roles else None
    if s is not None:
        with s.lock:
            if roles is None and s.roles:
                wanted = s.roles                          # the roles chosen at the start: the default view only
            return {"items": s.queue_items(wanted)}
    if roles is None and row["roles"]:
        wanted = row["roles"]
    items = []
    for q in _saved_questions(run_id):
        if q["status"] not in OPEN or (wanted and q["role"] not in wanted):
            continue
        items.append({"id": q["id"], "group_key": q["group_key"], "group_size": 1, "agent": q["agent"], "action": q["action"],
                      "role": q["role"], "role_title": (q["role"] or "").replace("_", " ").capitalize(), "title": q["title"],
                      "target": {"flight_id": q["target"].get("flight_id"), "crew_id": q["target"].get("crew_id"),
                                 "passenger_ids": q["target"].get("passenger_ids") or []},
                      "asked": fmt(q["asked_minute"]), "deadline": fmt(q["deadline_minute"]),
                      "minutes_left": max(0, q["deadline_minute"] - row["minute"]), "urgent": q["urgent"],
                      "priority_score": q["priority_score"], "safety_legal": bool((q["why"] or {}).get("safety_legal")),
                      "passengers_affected": 0, "passengers_seeing_wrong_info_now": 0,
                      "status": "escalated" if q["escalated"] else q["status"],
                      "snoozed_until": fmt(q["snoozed_until"]) if q["snoozed_until"] is not None else None,
                      "can_wait": False, "blocked_by": [], "last_option": q["last_option"],
                      "consequence_if_rejected": q["consequence"] or "", "why": q["why"]})
    items.sort(key=lambda i: (-(i["priority_score"] or 0), i["id"]))
    return {"items": items}


@router.get("/{run_id}/questions/{qid}/trace")
def trace(run_id: str, qid: int):
    s, row = _session(run_id)
    if s is not None:
        with s.lock:
            t = s.trace(qid)
        if t is None:
            raise HTTPException(404, f"No question {qid} in {run_id}.")
        return t
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT trace FROM live_decision_traces WHERE run_id=%s AND question_id=%s ORDER BY id DESC LIMIT 1", (run_id, qid))
        r = cur.fetchone()
    if r is None:
        raise HTTPException(404, f"No question {qid} in {run_id}.")
    return r[0]


@router.get("/{run_id}/flow")
def flow(run_id: str, since: int = Query(0, ge=0, le=1440)):
    s, row = _session(run_id)
    if s is not None:
        with s.lock:
            return s.flow(since)
    return {"agents": [], "orchestrator": {"open_incidents": 0, "queue": 0}, "edges": []}


@router.get("/{run_id}/history")
def history(run_id: str):
    s, row = _session(run_id)
    if s is not None:
        with s.lock:
            return {"decisions": s.history()}
    out = []
    for q in _saved_questions(run_id):
        a = q["answer"] or {}
        if q["status"] in OPEN or not a:
            continue
        out.append({"id": q["id"], "title": q["title"], "role": q["role"], "action": q["action"],
                    "answer": {"rejected_once": "rejected", "refused_rejected_before": "rejected",
                               "answered_by_duty_manager": "escalated"}.get(q["status"], q["status"]),
                    "reason": a.get("reason") or "", "asked": fmt(q["asked_minute"]),
                    "answered": fmt(q["answered_minute"]) if q["answered_minute"] is not None else "",
                    "waited_min": (q["answered_minute"] - q["asked_minute"]) if q["answered_minute"] is not None else 0})
    return {"decisions": out}


@router.get("/{run_id}/results")
def results(run_id: str):
    s, row = _session(run_id)
    if s is not None:
        with s.lock:
            if s.status != "finished":
                raise HTTPException(409, f"The session is {s.status}; results come at the end of the day.")
            return s.results_obj or s.results()
    if not row.get("results"):
        raise HTTPException(409, f"The session is {row['status']}; it has no end-of-day results.")
    return row["results"]
