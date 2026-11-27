"""Live human-in-the-loop API (mounted in backend/api/app.py). Field names are fixed: see docs/live_api.md.

The only writes are POST start, control and decide. Every other route only reads: a running session from memory (this process),
an older one (or one that ended with a server restart) from the database, read-only.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.execution.live_human import OPEN, DecideError
from simulator import live as L
from simulator.clock import fmt

router = APIRouter(prefix="/api/live")


class LiveError(HTTPException):
    """An error whose detail is always one plain sentence; extra facts (who blocks a wait, per-question results) beside it."""

    def __init__(self, code, message, **extra):
        super().__init__(code, str(message))
        self.extra = extra


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore")         # unknown fields (e.g. "to_role") are accepted and ignored


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
        ROLES = _roles() | set(_live()["views"]["frontline_roles"])
    return ROLES


def _live():
    from backend.execution.live_human import load_settings
    return load_settings()


def frontline():
    return set(_live()["views"]["frontline_roles"])


class StartBody(Body):
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


class ControlBody(Body):
    action: Literal["pause", "resume", "speed"]
    speed_idle: float | None = Field(None, gt=0)
    speed_pending: float | None = Field(None, gt=0)


class DecideBody(Body):
    question_ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["approve", "approve_option", "approve_with_change", "reject", "wait", "hand_off"]
    option_id: str | None = Field(None, max_length=20)
    change: dict | None = None
    reason: str = Field("", max_length=500)
    minutes: int | None = None

    @field_validator("change")
    @classmethod
    def _small(cls, v):
        if v is not None and (len(v) > 3 or any(not isinstance(k, str) or len(str(x)) > 1000 for k, x in v.items())):
            raise ValueError("change: at most 3 short fields")
        return v


class ReportBody(Body):
    kind: Literal["gate_unavailable", "jet_bridge_slow", "long_queue", "special_assistance_waiting", "equipment_issue", "other"]
    flight_id: str | None = Field(None, pattern=r"^WN\d{3,4}$")
    gate: str | None = Field(None, pattern=r"^[A-Z]{1,2}\d{1,3}$")
    text: str = Field("", max_length=500)
    role: str = Field("", max_length=60)

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v and v not in known_roles():
            raise ValueError(f"unknown role: {v}")
        return v


class NoteBody(Body):
    text: str = Field(min_length=1, max_length=500)
    role: str = Field("", max_length=60)

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v and v not in known_roles():
            raise ValueError(f"unknown role: {v}")
        return v


class RebookBody(Body):
    option_id: str = Field(min_length=1, max_length=20)


class Assignment(Body):
    customer_id: str = Field(min_length=1, max_length=20)
    option_id: str = Field(min_length=1, max_length=20)


class FlightRebookBody(Body):
    assignments: list[Assignment] = Field(min_length=1, max_length=300)


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
    raise LiveError(e.code, e.message, blocked_by=[{"id": b.id, "title": b.title} for b in e.blocked_by])


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
    if body.action == "approve_option" and not body.option_id:
        raise HTTPException(422, "approve_option needs option_id")
    if body.action == "approve_with_change" and not body.change:
        raise HTTPException(422, "approve_with_change needs change")
    with s.lock:
        try:
            results = s.decide(list(dict.fromkeys(body.question_ids)), body.action, body.reason, body.minutes,
                               body.option_id, body.change)
        except DecideError as e:
            _fail(e)
    failed = [r for r in results if r["error"]]
    if failed and len(failed) == len(results):            # nothing was decided: the error is the answer
        first = failed[0]
        raise LiveError(first["code"], first["error"], blocked_by=first.get("blocked_by") or [],
                        results=[{k: r[k] for k in ("id", "status", "error")} for r in results])
    return {"ok": not failed, "results": [{"id": r["id"], "status": r["status"], "error": r["error"], "done": r.get("done"),
                                           **({"blocked_by": r["blocked_by"]} if r.get("blocked_by") else {})} for r in results]}


def _running(run_id):
    s, row = _session(run_id)
    if s is None:
        raise HTTPException(409, f"The session is {row['status']} (read-only after it ended or the server restarted).")
    return s


def _live_session(run_id):
    """A session in this process (the flight, Customer, station and clock views read the live world)."""
    s, row = _session(run_id)
    if s is None or s.world is None:
        raise HTTPException(409, "This view needs a session running in this server (it reads the live world).")
    return s


def _frontline(role):
    if role not in frontline():
        raise HTTPException(403, "Customer lookups are for frontline staff (role counter_gate_agent).")


@router.post("/{run_id}/reports")
def report(run_id: str, body: ReportBody):
    s = _running(run_id)
    with s.lock:
        try:
            return s.report(body.kind, body.flight_id, body.gate, body.text, body.role)
        except DecideError as e:
            _fail(e)


@router.post("/{run_id}/flights/{flight_id}/notes")
def add_note(run_id: str, flight_id: str, body: NoteBody):
    s = _running(run_id)
    with s.lock:
        try:
            return s.add_note(flight_id, body.text, body.role)
        except DecideError as e:
            _fail(e)


@router.post("/{run_id}/customers/{pid}/rebook")
def rebook(run_id: str, pid: str, body: RebookBody, role: str = Query("counter_gate_agent")):
    _frontline(role)
    s = _running(run_id)
    with s.lock:
        if s.world is None or pid not in s.world.truth:
            raise HTTPException(404, f"No Customer {pid}.")
        try:
            return s.rebook(pid, body.option_id)
        except DecideError as e:
            _fail(e)


@router.post("/{run_id}/customers/{pid}/refund")
def refund(run_id: str, pid: str, role: str = Query("counter_gate_agent")):
    _frontline(role)
    s = _running(run_id)
    with s.lock:
        if s.world is None or pid not in s.world.truth:
            raise HTTPException(404, f"No Customer {pid}.")
        try:
            return s.refund(pid)
        except DecideError as e:
            _fail(e)


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
    if wanted and set(wanted) <= frontline():
        return {"items": []}                               # frontline staff have no approval queue
    if s is not None:
        with s.lock:
            if roles is None and s.roles:
                wanted = [r for r in s.roles if r not in frontline()] or None   # the start roles: the default view only
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


@router.get("/{run_id}/flights/{flight_id}/notes")
def notes(run_id: str, flight_id: str):
    s = _live_session(run_id)
    with s.lock:
        if flight_id not in s.world.flights:
            raise HTTPException(404, f"No flight {flight_id}.")
        return list(s.notes.get(flight_id, []))


@router.get("/{run_id}/flights/{flight_id}")
def flight(run_id: str, flight_id: str):
    s = _live_session(run_id)
    with s.lock:
        f = s.views.flight(flight_id)
    if f is None:
        raise HTTPException(404, f"No flight {flight_id}.")
    return f


@router.get("/{run_id}/customers")
def customers(run_id: str, q: str = Query("", max_length=60), role: str = Query("counter_gate_agent")):
    _frontline(role)
    s = _live_session(run_id)
    with s.lock:
        return s.views.search(q)


@router.get("/{run_id}/customers/{pid}")
def customer(run_id: str, pid: str, role: str = Query("counter_gate_agent")):
    _frontline(role)
    s = _live_session(run_id)
    with s.lock:
        c = s.views.customer(pid)
    if c is None:
        raise HTTPException(404, f"No Customer {pid}.")
    return c


@router.get("/{run_id}/station")
def station(run_id: str):
    s = _live_session(run_id)
    with s.lock:
        return s.views.station()


@router.get("/{run_id}/clocks")
def clocks(run_id: str):
    s = _live_session(run_id)
    with s.lock:
        return s.views.clocks()


@router.get("/{run_id}/handover")
def handover(run_id: str, since: str | None = Query(None, pattern=r"^\d{1,2}:\d{2}$")):
    s = _live_session(run_id)
    m = None
    if since:
        h, mm = since.split(":")
        m = int(h) * 60 + int(mm)
    with s.lock:
        return s.views.handover(m)


@router.get("/{run_id}/sent")
def sent(run_id: str, since: int = Query(0, ge=0, le=1440)):
    s = _live_session(run_id)
    with s.lock:
        return s.views.sent(since)


@router.get("/{run_id}/out_of_date")
def out_of_date(run_id: str):
    s = _live_session(run_id)
    with s.lock:
        return s.views.out_of_date()


@router.get("/{run_id}/customers/{pid}/rebooking")
def customer_rebooking(run_id: str, pid: str, reason: Literal["customer_request"] = Query("customer_request"),
                       role: str = Query("counter_gate_agent")):
    _frontline(role)
    s = _live_session(run_id)
    with s.lock:
        if pid not in s.world.truth:
            raise HTTPException(404, f"No Customer {pid}.")
        return {"customer_id": pid, "reason": reason, "options": s.views.request_options(pid)}


@router.get("/{run_id}/flights/{flight_id}/customers")
def flight_customers(run_id: str, flight_id: str, role: str = Query("counter_gate_agent")):
    _frontline(role)
    s = _live_session(run_id)
    with s.lock:
        out = s.views.flight_customers(flight_id)
    if out is None:
        raise HTTPException(404, f"No flight {flight_id}.")
    return out


@router.post("/{run_id}/flights/{flight_id}/rebook")
def flight_rebook(run_id: str, flight_id: str, body: FlightRebookBody, role: str = Query("counter_gate_agent")):
    _frontline(role)
    s = _running(run_id)
    with s.lock:
        if s.world is None or flight_id not in s.world.flights:
            raise HTTPException(404, f"No flight {flight_id}.")
        return s.rebook_many(flight_id, [a.model_dump() for a in body.assignments])


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
