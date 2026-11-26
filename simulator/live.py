"""Live human-in-the-loop mode: the simulated world, the agents running on it minute by minute, and the user in the loop.

    start(seed=7, scenario="a31_storm", roles=[...]) -> a LiveSession running in a background thread of the API process.

  - The world is built exactly as orchestrated mode builds it (simulator.run.simulate, mode "orchestrated"); approved decisions
    change it as there. The run id is "<scenario>-s<seed>-live-<n>".
  - 05:00 to live_from (config/live.yaml) runs at once with the simulated people: the morning is already decided. From
    live_from on, every question goes to the user (backend/execution/live_human.py). The duty manager stays simulated.
  - The clock never stops unless paused: speed_idle while nothing waits for the user, speed_pending while something does. It
    pauses on its own when no client asked for the state for heartbeat_timeout_s. A restart of the server ends every session
    (it is then read-only in history).
  - Every minute: wrong screens now and passenger-minutes so far (the channels' own count, as every other mode), the same from
    today's run at the same minute, and the focus passenger's four screens. The matching today run is made first if missing.
  - Everything is saved: the run's events, recommendations and shared versions (as every mode), plus live_sessions,
    live_questions, live_decision_traces and live_minutes.
"""
import threading
import time
from datetime import datetime, timezone
from random import Random

from backend.execution.copilot import serial
from backend.execution.live_human import OPEN, DecideError, LiveHumans, load_settings
from backend.execution.live_after import After
from backend.execution.live_trace import hhmm, make_tracer
from simulator.clock import at, fmt, hhmm as clock_min
from simulator.comparison import FIELDS
from simulator.run import run_id_for

LIVE_DDL = """
CREATE TABLE IF NOT EXISTS live_sessions (
    run_id         text PRIMARY KEY,
    seed           bigint NOT NULL,
    scenario       text NOT NULL,
    roles          jsonb NOT NULL DEFAULT '[]'::jsonb,
    status         text NOT NULL CHECK (status IN ('running','paused','finished','stopped')),
    sim_minute     int NOT NULL DEFAULT 0,
    speed_idle     real NOT NULL,
    speed_pending  real NOT NULL,
    live_from      int NOT NULL,
    focus_passenger text,
    results        jsonb,
    error          text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    ended_at       timestamptz
);
CREATE TABLE IF NOT EXISTS live_questions (
    run_id          text NOT NULL,
    id              int NOT NULL,
    agent           text NOT NULL,
    action          text NOT NULL,
    role            text,
    target          jsonb NOT NULL DEFAULT '{}'::jsonb,
    asked_minute    int NOT NULL,
    deadline_minute int NOT NULL,
    urgent          boolean NOT NULL DEFAULT false,
    status          text NOT NULL,
    group_key       text,
    depends_on      int[] NOT NULL DEFAULT '{}',
    priority_score  real,
    why             jsonb NOT NULL DEFAULT '{}'::jsonb,
    answer          jsonb,
    title           text,
    recommendation  bigint,
    last_option     boolean NOT NULL DEFAULT false,
    consequence     text,
    previous        int,
    snoozed_until   int,
    escalated       boolean NOT NULL DEFAULT false,
    answered_minute int,
    PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS live_decision_traces (
    id          bigserial PRIMARY KEY,
    run_id      text NOT NULL,
    question_id int NOT NULL,
    snapshot    text NOT NULL CHECK (snapshot IN ('asked','answered')),
    at_minute   int NOT NULL,
    trace       jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS live_minutes (
    run_id                    text NOT NULL,
    minute                    int NOT NULL,
    wrong_now                 int NOT NULL,
    wrong_minutes_total       int NOT NULL,
    today_wrong_now           int,
    today_wrong_minutes_total int,
    pending                   int NOT NULL DEFAULT 0,
    focus                     jsonb,
    PRIMARY KEY (run_id, minute)
);
ALTER TABLE metric_runs DROP CONSTRAINT IF EXISTS metric_runs_mode_check;
ALTER TABLE metric_runs ADD CONSTRAINT metric_runs_mode_check CHECK (mode IN ('today','copilot','orchestrated','live'));
"""

SESSIONS: dict = {}                  # run_id -> LiveSession (this process only; nothing survives a restart)
REGISTRY_LOCK = threading.Lock()


class Stop(Exception):
    """Ends a session before the end of the day (tests, shutdown)."""


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(LIVE_DDL)
    conn.commit()


def stop_orphans(conn):
    """After a server restart no session is running: anything left running or paused is now stopped (read-only)."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE live_sessions SET status='stopped', ended_at=coalesce(ended_at, now()) "
                    "WHERE status IN ('running','paused') AND NOT (run_id = ANY(%s))", (list(SESSIONS),))
    conn.commit()


def screen_text(row) -> str:
    """What a screen (or the truth) shows, in plain words."""
    trip = " → ".join(row.get("itinerary") or []) or "no flight"
    return f"{row.get('status', '')}: {trip}" + (f" (to {row['destination']})" if row.get("destination") else "")


class Baseline:
    """Today's run for the same seed and scenario: its wrong-screen series minute by minute, and its results."""

    def __init__(self, metrics: dict):
        self.metrics = metrics or {}
        series = self.metrics.get("comparison_series") or {}
        self.by_t, total = {}, 0
        for t, n in zip(series.get("t") or [], series.get("pax_disagreeing") or []):
            total += n
            self.by_t[t] = (n, total)

    def at(self, t):
        return self.by_t.get(t, (None, None))


def load_run_metrics(conn, run_id):
    with conn.cursor() as cur:
        cur.execute("SELECT metrics FROM metric_runs WHERE run_id=%s ORDER BY id DESC LIMIT 1", (run_id,))
        row = cur.fetchone()
    return row[0] if row else None


def ensure_today(conn, seed, scenario, data=None):
    """The matching today run (made first, without wiping anything, if it is missing)."""
    rid = run_id_for(seed, scenario, "today")
    m = load_run_metrics(conn, rid)
    if m is None:
        from simulator.run import simulate
        from simulator.store import Writer
        simulate(seed, scenario, "today", data=data, writer=Writer(conn, rid, seed, persist_truth=False))
        m = load_run_metrics(conn, rid)
    return Baseline(m)


def next_run_id(conn, seed, scenario):
    with conn.cursor() as cur:
        cur.execute("SELECT run_id FROM live_sessions WHERE seed=%s AND scenario=%s", (seed, scenario))
        taken = {r[0] for r in cur.fetchall()} | set(SESSIONS)
    n = 1
    while f"{scenario}-s{seed}-live-{n}" in taken:
        n += 1
    return f"{scenario}-s{seed}-live-{n}"


class LiveWriter:
    """The run's events and records are saved as every mode saves them; the metrics row says mode 'live'."""

    def __new__(cls, conn, run_id, seed):
        from simulator.store import Writer

        class _W(Writer):
            def finish(self, scenario, metrics, mode, until):
                return super().finish(scenario, {**metrics, "mode": "live"}, "live", until)
        return _W(conn, run_id, seed, persist_truth=False)


class LiveSession:
    def __init__(self, run_id, seed=7, scenario="a31_storm", roles=(), settings=None, connect=None, data=None,
                 sleep=time.sleep, clock=time.monotonic, pace=True, driver=None, until=None, live_from=None, baseline=None):
        self.s = settings or load_settings()
        c = self.s["clock"]
        self.run_id, self.seed, self.scenario, self.roles = run_id, seed, scenario, list(roles)
        self.connect, self.data = connect, data
        self.sleep, self.clock, self.pace, self.driver, self.until = sleep, clock, pace, driver, until
        self.live_from = clock_min(live_from or c["live_from"])
        self.speed_idle, self.speed_pending = float(c["speed_idle"]), float(c["speed_pending"])
        self.status, self.minute, self.error = "running", 0, None
        self.started_at, self.ended_at = datetime.now(timezone.utc), None
        self.lock = threading.RLock()
        self.heartbeat = clock()
        self.auto_paused = False
        self.baseline = baseline
        self.orchestrated = None
        self.humans = self.copilot = self.world = self.channels = self.comparison = self.tracer = self.after = self.plain = None
        self.focus = self.focus_name = None
        self.focus_since = {}            # channel -> (wrong?, since minute)
        self.trip_changes = {}
        self.ev_cursor = 0
        self.total = 0
        self.minutes = []                # one row a minute (from live_from) for live_minutes
        self.saved_minutes = self.saved_traces = 0
        self.save_now = False
        self.result = None
        self.results_obj = None
        self.notes = {}                  # flight -> [{t, role, text}]: notes staff wrote on a flight (notes only)
        self.reports = []                # reports from the floor, as sent
        self.first_trip, self.disrupted_since, self.stranded_since = {}, {}, {}
        self.views = None
        self.thread = None
        self.conn = None

    # ------------------------------------------------------------ start / run
    def start(self, background=True):
        if background:
            self.thread = threading.Thread(target=self.run, name=f"live:{self.run_id}", daemon=True)
            self.thread.start()
        else:
            self.run()
        return self

    def run(self):
        from simulator.run import simulate
        self.lock.acquire()
        try:
            if self.connect is not None:
                self.conn = self.connect()
                ensure_tables(self.conn)
                if self.baseline is None:
                    self.baseline = ensure_today(self.conn, self.seed, self.scenario, self.data)
                self.orchestrated = load_run_metrics(self.conn, run_id_for(self.seed, self.scenario, "orchestrated"))
                self._save_session()
            self.baseline = self.baseline or Baseline({})
            self.writer = LiveWriter(self.conn, self.run_id, self.seed) if self.conn is not None else None
            kw = {"writer": self.writer} if self.writer is not None else {}
            until = self.until
            self.result = simulate(self.seed, self.scenario, mode="orchestrated", data=self.data, run_id=self.run_id,
                                   humans=self._humans, on_ready=self._ready, on_tick=self._tick, until=until, **kw)
            self.status = "finished"
            self.results_obj = self.results()
            self._final_traces()
        except Stop:
            if self.status not in ("finished",):
                self.status = "stopped"
        except Exception as err:                          # noqa: BLE001 - the session ends; the reason is kept and shown
            import traceback
            self.error = f"{type(err).__name__}: {err}"
            self.trace_text = traceback.format_exc()
            self.status = "stopped"
        finally:
            self.ended_at = datetime.now(timezone.utc)
            try:
                if self.conn is not None:
                    self._save(final=True)
                    self.conn.close()
            except Exception as err:                      # noqa: BLE001
                self.error = self.error or f"saving: {type(err).__name__}: {err}"
            self.lock.release()

    def _humans(self, sim):
        self.humans = LiveHumans(sim, at(self.live_from), self.s, Random(f"live-duty:{self.run_id}"))
        return self.humans

    def _ready(self, world, channels, copilot, comparison, recorder):
        from simulator.comparison import focus_passenger
        self.world, self.channels, self.copilot, self.comparison = world, channels, copilot, comparison
        self.tracer = make_tracer(copilot)
        self.after = After(copilot, channels, self.s)
        self.humans.tracer, self.humans.queue, self.humans.after = self.tracer, copilot.queue, self.after
        from backend.execution.live_autonomy import load as load_autonomy
        from backend.execution.live_plain import Plain
        self.plain = Plain(copilot, self.s)
        self.humans.plain, self.humans.copilot, self.humans.autonomy = self.plain, copilot, load_autonomy()
        self.humans.closed_gates = {}                            # gate -> note: reported unavailable from the floor
        from backend.execution.live_views import Views
        self.views = Views(self)
        self.humans.views = self.views
        self.first_trip = {pid: list(t["itinerary"]) for pid, t in world.truth.items()}
        copilot.human_only_port = self.humans.human_only        # human-only decisions come to you (live mode only)
        self.humans.wrong_pax = channels.dis_pax
        if self.scenario != "random":
            self.focus = focus_passenger({"passengers": list(world.passengers.values())})
            self.focus_name = (world.passengers.get(self.focus) or {}).get("full_name")

    # ------------------------------------------------------------ the minute
    def _tick(self, t, world, channels):
        self.minute = t
        now = at(t)
        self._record_events(t)
        self.humans.refresh(now)
        self._metrics(t)
        if self.until is not None and t > self.until:
            raise Stop()
        if t < self.live_from:
            self.lock.release()                          # the morning runs at once; readers still get in between minutes
            self.lock.acquire()
            return
        if self.driver is not None:
            self.driver(self, t)
            self._record_events(t)
            self.humans.refresh(now)
        if self.conn is not None and ((t - self.live_from) % self.s["clock"]["write_every_min"] == 0 or self.save_now):
            self._save()
        if self.status == "stopped":
            raise Stop()
        self._wait()

    def _wait(self):
        """Hold the clock: a minute takes 1/speed real seconds (from the end of the last wait); paused holds it."""
        if not self.pace:
            return
        self.lock.release()
        try:
            started = getattr(self, "tick_started", self.clock())
            while True:
                if self.status == "stopped":
                    break
                if self.status == "running" and self.clock() - self.heartbeat > self.s["clock"]["heartbeat_timeout_s"]:
                    self.status, self.auto_paused, self.save_now = "paused", True, True
                if self.save_now and self.conn is not None:
                    with self.lock:
                        self._save()
                if self.status == "paused":
                    self.sleep(self.s["clock"]["poll_s"])
                    started = self.clock()
                    continue
                speed = self.speed_pending if self.pending_count() else self.speed_idle
                left = started + 1.0 / max(speed, 1e-6) - self.clock()
                if left <= 0:
                    break
                self.sleep(min(left, self.s["clock"]["poll_s"]))
        finally:
            self.lock.acquire()
            self.tick_started = self.clock()
        if self.status == "stopped":
            raise Stop()

    def _record_events(self, t):
        """The user's answers as events (actor human:<role>); a final "no" also reaches the agents that read it (contract)."""
        h = self.humans
        if not h or not h.events:
            return
        for actor, kind, payload, when in h.events:
            p = serial(payload)
            self.world.events.append({"t": t, "type": kind, "actor": actor, "actor_name": actor, "station": None,
                                      "payload": p, "incident": False})
            if kind in ("human_rejected", "human_decision_recorded"):     # what agents may read (their contracts decide)
                self.copilot.side_event_id -= 1      # its own id namespace (fix-seats side feeds), so nothing is renumbered
                self.copilot.store.events.append({"id": self.copilot.side_event_id, "occurred_at": at(t), "published_at": None,
                                                  "type": kind, "station": None, "payload": p})
        h.events = []
        self.save_now = True

    def _track(self, t):
        """When each tracked Customer was first disrupted and first stranded (for the clocks)."""
        for pid, tr in self.world.truth.items():
            if pid not in self.disrupted_since and (tr["status"] not in ("confirmed", "scheduled") or
                                                    list(tr["itinerary"]) != self.first_trip.get(pid)):
                self.disrupted_since[pid] = t
            if pid not in self.stranded_since and tr["status"] in ("cancelled", "overnight"):
                self.stranded_since[pid] = t

    def _metrics(self, t):
        self._track(t)
        cmp = self.comparison
        n = cmp.counts[-1] if cmp.counts else 0
        self.total += n
        if self.scenario == "random" and self.focus is None:
            self._pick_focus(t)
        if t < self.live_from:
            return
        today_now, today_total = self.baseline.at(t)
        self.minutes.append({"minute": t, "wrong_now": n, "wrong_minutes_total": self.total, "today_wrong_now": today_now,
                             "today_wrong_minutes_total": today_total, "pending": self.pending_count(), "focus": self.focus_view(t)})

    def _pick_focus(self, t):
        for e in self.world.events[self.ev_cursor:]:
            if e["type"] == "trip_record_updated" and e["actor"] == "simulator":
                pid = e["payload"]["passenger_id"]
                self.trip_changes[pid] = self.trip_changes.get(pid, -1) + 1      # the first record is the start of the day
        self.ev_cursor = len(self.world.events)
        if t >= self.live_from and self.trip_changes and max(self.trip_changes.values()) > 0:
            best = max(self.trip_changes.values())
            self.focus = min(p for p, n in self.trip_changes.items() if n == best)
            self.focus_name = (self.world.passengers.get(self.focus) or {}).get("full_name")

    def focus_view(self, t):
        pid = self.focus
        if pid is None or pid not in self.world.truth:
            return {"passenger_id": None, "name": None, "true_status": "", "screens": []}
        screens = []
        for ch in self.channels.screens[pid]:                       # backend/screens.py: only the screens they have
            shown = self.channels.shown[(pid, ch)]
            wrong = any((pid, ch, f) in self.channels.dis for f in FIELDS if f != "bag_routing")
            was = self.focus_since.get(ch)
            if was is None or was[0] != wrong:
                self.focus_since[ch] = was = (wrong, t)
            screens.append({"channel": ch, "shows": screen_text(shown), "wrong": wrong, "since": fmt(was[1])})
        return {"passenger_id": pid, "name": self.focus_name, "true_status": screen_text(self.world.truth[pid]), "screens": screens}

    # ------------------------------------------------------------ what the API reads (under the lock)
    def pending_count(self):
        return sum(1 for q in self.humans.questions if q.status in OPEN) if self.humans else 0

    def touch(self):
        self.heartbeat = self.clock()

    def now(self):
        return at(self.minute)

    def urgent(self, q):
        left = (q.deadline_at - self.now()).total_seconds() / 60
        return bool(q.urgent or q.safety_legal or left <= self.s["decisions"]["urgent_within_min"])

    def state(self):
        w, last = self.world, (self.minutes[-1] if self.minutes else None)
        open_ = [q for q in self.humans.questions if q.status in OPEN] if self.humans else []
        incidents = [{"id": str(i.id), "status": i.status, "title": f"{i.kind.replace('_', ' ')} at MCI (incident #{i.id})"}
                     for i in (self.copilot.incidents.all if self.copilot else [])]
        stop = (w.restr or {}) if w is not None else {}
        return {"run_id": self.run_id, "status": self.status, "minute": self.minute, "clock": fmt(self.minute),
                "speed_idle": self.speed_idle, "speed_pending": self.speed_pending, "roles": self.roles,
                "pending": len(open_), "urgent_pending": sum(self.urgent(q) for q in open_), "incidents": incidents,
                "world": {"ground_stop": bool(stop), "ramp_open": w is None or w.ramp_until is None,
                          "stop_expected_end": fmt(stop["end"]) if stop.get("end") is not None else None},
                "truth": {"wrong_now": last["wrong_now"] if last else 0,
                          "wrong_minutes_total": last["wrong_minutes_total"] if last else self.total,
                          "today_wrong_now": last["today_wrong_now"] if last else None,
                          "today_wrong_minutes_total": last["today_wrong_minutes_total"] if last else None},
                "focus": (last or {}).get("focus") or (self.focus_view(self.minute) if self.world else
                                                       {"passenger_id": None, "name": None, "true_status": "", "screens": []}),
                **({"error": self.error} if self.error else {})}

    def queue_items(self, roles=None):
        now, out = self.now(), []
        qs = [q for q in self.humans.questions if q.status in OPEN] if self.humans else []
        for q in qs:
            if roles and q.role not in roles:
                continue
            blocked = self.humans.blockers(q)
            out.append({"id": q.id, "group_key": q.group_key, "group_size": sum(1 for p in qs if p.group_key == q.group_key),
                        "agent": q.agent, "action": q.action, "role": q.role, "role_title": (q.role or "").replace("_", " ").capitalize(),
                        "title": q.title, "target": {"flight_id": q.target.get("flight_id"), "crew_id": q.target.get("crew_id"),
                                                     "passenger_ids": list(q.passenger_ids)},
                        "asked": q.asked, "deadline": q.deadline,
                        "minutes_left": max(0, int((q.deadline_at - now).total_seconds() // 60)), "urgent": self.urgent(q),
                        "priority_score": q.priority_score, "safety_legal": bool(q.safety_legal), "passengers_affected": q.pax,
                        "passengers_seeing_wrong_info_now": q.wrong_now_pax,
                        "status": "escalated" if q.escalated else q.status,
                        "snoozed_until": hhmm(q.snoozed_until) if q.status == "snoozed" else None,
                        "can_wait": not blocked, "blocked_by": [{"id": b.id, "title": b.title} for b in blocked],
                        "last_option": q.last_option, "consequence_if_rejected": q.consequence or "", "why": q.why,
                        **self.plain_of(q), "bundle": q.bundle, "section": q.section,
                        "allowed_changes": self.humans.allowed_changes(q),
                        **({"care": self.views.care_block(q)} if q.agent == "passenger_care" and self.views else {})})
        out.sort(key=lambda i: (-i["priority_score"], i["id"]))
        return out

    def plain_of(self, q):
        p = q.plain or {}
        return {k: p.get(k) for k in ("headline", "story", "recommendation", "options", "effect", "if_declined")}

    def question(self, qid):
        return self.humans.by_id.get(qid) if self.humans else None

    def trace(self, qid):
        """The latest saved snapshot; once answered, continued with what the decision did (changes, messages, destinations)."""
        from copy import deepcopy
        snaps = [t for i, _, t in self.humans.traces if i == qid]
        q = self.question(qid)
        if not snaps or q is None:
            return None
        t = deepcopy(snaps[-1])
        if t["snapshot"] == "answered" and self.world is not None and not t.get("continued"):
            t.pop("preview", None)
            self.after.extend(t, q, self.world.events)
            if self.plain is not None:
                self.plain.relabel(t, q)
            t["continued"] = True                                  # the after-the-answer part is in (never added twice)
        return t

    def _final_traces(self):
        """At the end: every answered question's flowchart, continued to its destinations, is saved once more."""
        for q in self.humans.questions if self.humans else []:
            if q.answered_at is not None:
                t = self.trace(q.id)
                if t is not None:
                    self.humans.traces.append((q.id, "answered", t))

    def history(self):
        names = {"rejected_once": "rejected", "refused_rejected_before": "rejected", "handed_off": "handed_off"}
        out = []
        for q in self.humans.questions if self.humans else []:
            if q.status in OPEN or q.status == "withdrawn":
                if not q.waited:
                    continue
                ans = "waited"
            elif q.status == "answered_by_duty_manager":
                ans = "handed_off" if q.handed_off else "escalated"
            elif q.status == "auto":
                ans = "auto"
            else:
                ans = names.get(q.status, q.status)
            out.append({"id": q.id, "title": q.title, "headline": (q.plain or {}).get("headline"), "role": q.role,
                        "action": q.action, "answer": ans, "done": q.done, "bundle": q.bundle,
                        "reason": (q.answer or {}).get("reason") or (q.auto_rule and f"Done automatically: {q.auto_rule}") or "",
                        "asked": q.asked,
                        "answered": hhmm(q.answered_at) if q.answered_at else "",
                        "waited_min": int((q.answered_at - q.asked_at).total_seconds() // 60) if q.answered_at
                        else sum(w["minutes"] for w in q.waited)})
        return out

    def flow(self, since):
        waiting = {}
        for q in self.humans.questions if self.humans else []:
            if q.status in OPEN:
                waiting[q.agent] = waiting.get(q.agent, 0) + 1
        agents, edges = self.tracer.flow(self.now(), since, waiting) if self.tracer else ([], [])
        if self.after is not None:                               # Communication: each batch of messages it drafts or sends
            comm = next(a for a in agents if a["key"] == "communication")
            comm["versions"] = self.after.comm_versions(self.now(), since)
            for v in comm["versions"]:
                for u in v["used"]:
                    edges.append({"from": f"{u['kind']}:{u['ref']}", "to": f"communication:{v['v']}", "t": hhmm(v["t"])})
                v["t"] = hhmm(v["t"])
        incidents = [i for i in (self.copilot.incidents.all if self.copilot else []) if i.status != "resolved"]
        return {"agents": agents, "orchestrator": {"open_incidents": len(incidents), "queue": self.pending_count()}, "edges": edges}

    def decide(self, ids, action, reason=None, minutes=None, option_id=None, change=None):
        if self.status in ("finished", "stopped"):
            raise DecideError(409, f"the session is {self.status}")
        res = self.humans.decide(ids, action, self.now(), reason, minutes, option_id, change)
        self._record_events(self.minute)
        self.humans.refresh(self.now())
        self.save_now = True
        return res

    # ------------------------------------------------------------ the floor: reports and flight notes (writes)
    def report(self, kind, flight_id=None, gate=None, text="", role=""):
        """A report from the floor goes into shared state as a floor_report event (agents read it through their contracts)."""
        if self.status in ("finished", "stopped"):
            raise DecideError(409, f"the session is {self.status}")
        if flight_id and flight_id not in self.world.flights:
            raise DecideError(404, f"no flight {flight_id}")
        payload = {"kind": kind, "flight_id": flight_id, "gate": gate, "text": text, "role": role,
                   "reported_at": self.now().isoformat()}
        self.copilot.side_event_id -= 1
        self.copilot.store.events.append({"id": self.copilot.side_event_id, "occurred_at": self.now(), "published_at": None,
                                          "type": "floor_report", "station": "MCI", "payload": dict(payload)})
        self.world.events.append({"t": self.minute, "type": "floor_report", "actor": f"human:{role or 'floor'}",
                                  "actor_name": f"human:{role or 'floor'}", "station": "MCI", "payload": serial(payload),
                                  "incident": False})
        if kind == "gate_unavailable" and gate:
            self.humans.closed_gates[gate] = text or "reported unavailable from the floor"
        self.reports.append({"t": fmt(self.minute), **payload})
        self.save_now = True
        return {"ok": True, "report": self.reports[-1]}

    def add_note(self, flight_id, text, role):
        if flight_id not in self.world.flights:
            raise DecideError(404, f"no flight {flight_id}")
        if self.status in ("finished", "stopped"):
            raise DecideError(409, f"the session is {self.status}")
        note = {"t": fmt(self.minute), "role": role, "text": text}
        self.notes.setdefault(flight_id, []).append(note)
        self.world.events.append({"t": self.minute, "type": "flight_note_added", "actor": f"human:{role or 'floor'}",
                                  "actor_name": f"human:{role or 'floor'}", "station": None,
                                  "payload": {"flight_id": flight_id, "text": text, "role": role}, "incident": False})
        self.save_now = True
        return list(self.notes[flight_id])

    def rebook(self, pid, option_id):
        """Rebook a Customer through Passenger Care's path: a rebook_passenger decision (K01), rule-checked and carried out by
        the reservations system exactly as an approved Passenger Care rebooking is in orchestrated mode."""
        from backend.execution.executor import apply_approved
        if self.status in ("finished", "stopped"):
            raise DecideError(409, f"the session is {self.status}")
        opts = {o["option_id"]: o for o in self.views.rebooking(pid)}
        if option_id not in opts:
            raise DecideError(409 if opts else 409, "that option is not open for this Customer" if opts else
                              "this Customer has no rebooking options (their trip was not cancelled or moved to tomorrow)")
        to = opts[option_id]["flights"][0]["flight_id"]
        facts = {"passenger_ids": [pid], "flight_id": to}
        if not self.views.stranded(pid):                       # leaving a delayed flight: name it
            facts["from_flight"] = self.views.leaving_on(pid)
        proposal = {"id": f"counter-{pid}-{self.minute}", "agent": "passenger_care", "action": "rebook_passenger",
                    "decision_ids": ["K01"], "facts": facts}
        out = apply_approved(proposal, self.now(), self.copilot.apply_port)
        self._event_frontline("customer_rebooked" if out["applied"] else "customer_rebook_refused",
                              {"passenger_id": pid, "flight_id": to, "reason": out.get("reason")})
        if not out["applied"]:
            raise DecideError(409, "Could not rebook: " + str(out.get("reason") or "the rule check said no"))
        return {"ok": True, "trip": self.views.trip(pid)}

    def refund(self, pid):
        """A refund through Passenger Care's path: its own refund offer for this Customer, approved and rule-checked again."""
        from types import SimpleNamespace
        if self.status in ("finished", "stopped"):
            raise DecideError(409, f"the session is {self.status}")
        care = self.copilot.care
        rid = care.memory.proposed.get(("refund", pid)) if care else None
        if rid is None:
            raise DecideError(409, "Passenger Care has no refund for this Customer (no cancellation or significant change on record)")
        rec = care.context.saved.get(("recommendations", rid)) or {}
        if rec.get("status") in ("approved", "done_automatically") or any(
                q.status == "auto" and q.rec_id == rid and q.agent == "passenger_care" for q in self.humans.questions):
            return {"ok": True, "trip": self.views.trip(pid), "refund": "already issued"}
        q = SimpleNamespace(rec_id=rid, role="counter_gate_agent", answer={"waited_min": 0}, agent="passenger_care")
        a = care.approved(q, self.now(), self.copilot.care_publish(self.now()))
        if a is None or a.get("void"):
            raise DecideError(409, "The refund could not be issued: " + "; ".join((a or {}).get("void") or ["it is no longer pending"]))
        for actor, kind, payload in self.copilot.vendor_out:
            self.world.events.append({"t": self.minute, "type": kind, "actor": "system", "actor_name": actor, "station": None,
                                      "payload": serial(payload), "incident": False})
        self.copilot.vendor_out = []
        self._event_frontline("customer_refunded", {"passenger_id": pid, "recommendation": rid})
        for lq in self.humans.questions:
            if lq.agent == "passenger_care" and lq.rec_id == rid and lq.status in OPEN:
                lq.status, lq.answered_at, lq.done = "approved", self.now(), "Refund issued at the counter"
                lq.answer = {"decision": "approved", "by": "human:counter_gate_agent", "t": fmt(self.minute)}
        return {"ok": True, "trip": self.views.trip(pid), "refund": "issued"}

    def _event_frontline(self, kind, payload):
        self.world.events.append({"t": self.minute, "type": kind, "actor": "human:counter_gate_agent",
                                  "actor_name": "human:counter_gate_agent", "station": None, "payload": serial(payload),
                                  "incident": False})
        self.save_now = True

    def control(self, action, speed_idle=None, speed_pending=None):
        c = self.s["clock"]
        if self.status in ("finished", "stopped"):
            raise DecideError(409, f"the session is {self.status}")
        if action == "pause":
            self.status, self.auto_paused = "paused", False
        elif action == "resume":
            self.status, self.auto_paused = "running", False
            self.touch()
        elif action == "speed":
            for name, v in (("speed_idle", speed_idle), ("speed_pending", speed_pending)):
                if v is None:
                    continue
                if not c["speed_min"] <= float(v) <= c["speed_max"]:
                    raise DecideError(422, f"{name} must be between {c['speed_min']} and {c['speed_max']}")
                setattr(self, name, float(v))
        self.save_now = True
        return self.state()

    # ------------------------------------------------------------ end of the day
    def results(self):
        def pick(m, focus):
            if not m:
                return None
            trip = None
            if focus and m is self.result.metrics and focus in self.world.truth:
                trip = screen_text(self.world.truth[focus])
            elif focus and self.scenario != "random":       # other runs follow the configured focus passenger only
                fin = (m.get("maya_minutes") or [])[-1:] if m.get("maya_minutes") else []
                trip = screen_text(fin[0]["truth"]) if fin else None
            return {"wrong_screen_passenger_minutes": m.get("passenger_minutes_of_disagreement"),
                    "critical_passenger_minutes": m.get("critical_passenger_minutes_wrong"),
                    "lost_updates_still_wrong": m.get("lost_updates_wrong_end"),
                    "cancelled": m.get("flights_cancelled"), "stranded_overnight": m.get("passengers_stranded_overnight"),
                    "focus_final_trip": trip}
        mine = self.result.metrics if self.result else None
        return {"you": pick(mine, self.focus), "today": pick(self.baseline.metrics if self.baseline else None, self.focus),
                "orchestrated": pick(self.orchestrated, self.focus), "decisions": self.humans.decisions() if self.humans else {}}

    # ------------------------------------------------------------ saving (only this session's thread uses its connection)
    def _save_session(self):
        from psycopg.types.json import Jsonb
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO live_sessions (run_id, seed, scenario, roles, status, sim_minute, speed_idle, speed_pending, live_from, "
                "focus_passenger, results, error, started_at, ended_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (run_id) DO UPDATE SET status=EXCLUDED.status, sim_minute=EXCLUDED.sim_minute, "
                "speed_idle=EXCLUDED.speed_idle, speed_pending=EXCLUDED.speed_pending, focus_passenger=EXCLUDED.focus_passenger, "
                "results=EXCLUDED.results, error=EXCLUDED.error, ended_at=EXCLUDED.ended_at",
                (self.run_id, self.seed, self.scenario, Jsonb(self.roles), self.status, self.minute, self.speed_idle,
                 self.speed_pending, self.live_from, self.focus, Jsonb(serial(self.results_obj)) if self.results_obj else None,
                 self.error, self.started_at, self.ended_at))
        self.conn.commit()

    def _save(self, final=False):
        from psycopg.types.json import Jsonb
        self.save_now = False
        if getattr(self, "writer", None) is not None and self.world is not None and not final:
            self.writer.flush(self.world, self.channels)       # the run's events so far (the day page reads them while it runs)
        h = self.humans
        with self.conn.cursor() as cur:
            if h is not None:
                rows = []
                for qid in sorted(h.dirty):
                    q = h.by_id[qid]
                    rows.append((self.run_id, q.id, q.agent, q.action, q.role, Jsonb(serial(q.target)), _m(q.asked_at),
                                 _m(q.deadline_at), bool(q.urgent), q.status, q.group_key, list(q.depends_on), q.priority_score,
                                 Jsonb(serial(q.why)), Jsonb(serial(q.answer)) if q.answer else None, q.title, q.rec_id,
                                 q.last_option, q.consequence, q.previous, _m(q.snoozed_until) if q.snoozed_until else None,
                                 q.escalated, _m(q.answered_at) if q.answered_at else None))
                if rows:
                    cur.executemany(
                        "INSERT INTO live_questions (run_id, id, agent, action, role, target, asked_minute, deadline_minute, urgent, status, "
                        "group_key, depends_on, priority_score, why, answer, title, recommendation, last_option, consequence, previous, "
                        "snoozed_until, escalated, answered_minute) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (run_id, id) DO UPDATE SET status=EXCLUDED.status, deadline_minute=EXCLUDED.deadline_minute, "
                        "group_key=EXCLUDED.group_key, depends_on=EXCLUDED.depends_on, priority_score=EXCLUDED.priority_score, "
                        "answer=EXCLUDED.answer, snoozed_until=EXCLUDED.snoozed_until, escalated=EXCLUDED.escalated, "
                        "answered_minute=EXCLUDED.answered_minute", rows)
                h.dirty = set()
                new = h.traces[self.saved_traces:]
                if new:
                    cur.executemany("INSERT INTO live_decision_traces (run_id, question_id, snapshot, at_minute, trace) "
                                    "VALUES (%s,%s,%s,%s,%s)",
                                    [(self.run_id, qid, snap, self.minute, Jsonb(serial(tr))) for qid, snap, tr in new])
                    self.saved_traces = len(h.traces)
            new = self.minutes[self.saved_minutes:]
            if new:
                cur.executemany("INSERT INTO live_minutes (run_id, minute, wrong_now, wrong_minutes_total, today_wrong_now, "
                                "today_wrong_minutes_total, pending, focus) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                                "ON CONFLICT (run_id, minute) DO NOTHING",
                                [(self.run_id, r["minute"], r["wrong_now"], r["wrong_minutes_total"], r["today_wrong_now"],
                                  r["today_wrong_minutes_total"], r["pending"], Jsonb(serial(r["focus"]))) for r in new])
                self.saved_minutes = len(self.minutes)
        self.conn.commit()
        self._save_session()


def _m(t):
    """Minutes after midnight (local) of a moment."""
    from simulator.clock import minute_of
    return minute_of(t)


# ------------------------------------------------------------ the sessions of this process
def start(seed=7, scenario="a31_storm", roles=(), connect=None, **kw):
    from backend.database import connect as db_connect
    from simulator.scenario import SCENARIOS
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")
    connect = connect or db_connect
    conn = connect()
    try:
        ensure_tables(conn)
        with REGISTRY_LOCK:
            rid = next_run_id(conn, seed, scenario)
            s = LiveSession(rid, seed, scenario, roles, connect=connect, **kw)
            SESSIONS[rid] = s
    finally:
        conn.close()
    return s.start(background=True)


def get(run_id):
    return SESSIONS.get(run_id)
