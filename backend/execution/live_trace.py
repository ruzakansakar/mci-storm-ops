"""Why a question reached the person, and the path it took (live human-in-the-loop mode).

Everything here is read from what the co-pilot itself recorded: the events the agents were delivered (the shared store), the
shared versions they wrote (each records the upstream versions it used), their recommendations and rule-check results, the
orchestrator's minute log and the decision catalog. Nothing is invented: options are listed only if the agent produced them.
Never reads the simulator's truth (that is the answer key)."""
import re
from datetime import datetime

from backend.contracts.shared_state import SHARED
from backend.settings import TZ

AGENT_TABLE = {"situation": "situation_assessments", "network_recovery": "recovery_plans", "passenger_care": "care_plans",
               "station_staff": "station_plans"}
NAMES = {"situation": "Situation", "network_recovery": "Network Recovery", "passenger_care": "Passenger Care",
         "station_staff": "Station & Staff", "communication": "Communication", "orchestrator": "Orchestrator"}
# the upstream versions each shared version records (shared-state rules: every version carries what it used)
UPSTREAM = {"recovery_plans": {"situation_version": "situation", "station_plan_version": "station_staff"},
            "care_plans": {"situation_version": "situation", "recovery_plan_version": "network_recovery"},
            "station_plans": {"situation_version": "situation", "recovery_plan_version": "network_recovery",
                              "care_plan_version": "passenger_care"}}
KIND_OF_TABLE = {v: k for k, v in AGENT_TABLE.items()}
CREW_ACTS = ("call_in_reserve_crew", "swap_crews", "deadhead_crew")


def hhmm(t) -> str:
    if t is None:
        return ""
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t.astimezone(TZ).strftime("%H:%M")


def minute(t) -> int:
    t = t.astimezone(TZ)
    return t.hour * 60 + t.minute


def flights_in(*parts) -> list:
    return sorted(set(re.findall(r"\bWN\d{3,4}\b", " ".join(str(p) for p in parts))))


def make_tracer(copilot):
    """The tracer with the decision catalog, the rules and the contracts it explains from."""
    from backend.contracts import load_contracts
    from backend.contracts.decisions import load_decisions
    from backend.rules import load_rules
    return Tracer(copilot, load_decisions(), load_rules(), load_contracts())


class Tracer:
    """Built once per session from the co-pilot (the orchestrator) after it is created."""

    def __init__(self, copilot, decisions: dict, rules: dict, contracts: dict):
        self.c, self.decisions, self.rules, self.contracts = copilot, decisions, rules, contracts

    # ------------------------------------------------------------ lookups
    def context(self, agent):
        c = self.c
        return {"situation": c.situation.context, "communication": c.context, "orchestrator": c.octx,
                "network_recovery": c.network and c.network.context, "passenger_care": c.care and c.care.context,
                "station_staff": c.station and c.station.context}.get(agent)

    def rec(self, agent, rec_id):
        ctx = self.context(agent)
        return (ctx.saved.get(("recommendations", rec_id)) if ctx is not None else None) or {}

    def latest(self, table, now, version=None):
        rows = [r for r in self.c.store.records[table] if r[SHARED[table]["time"]] <= now]
        if version is not None:
            rows = [r for r in rows if r["version"] == version]
        return max(rows, key=lambda r: r["version"]) if rows else None

    def feed_of(self, agent, type_):
        for name in (agent, "situation"):
            con = self.contracts.get(name)
            if con is not None and type_ in con.event_types:
                return con.event_types[type_][0]
        return "feed"

    # ------------------------------------------------------------ what a question is about
    def facts(self, agent, action, proposal, item_facts):
        """Target, flights and passengers of a question, from the proposal the agent made."""
        p = proposal or {}
        msgs = p.get("messages") or []
        pids = sorted({m.get("passenger_id") for m in msgs if m.get("passenger_id")} | set(p.get("passenger_ids") or []))
        target = {"flight_id": p.get("flight_id") or item_facts.get("flight_id"), "crew_id": p.get("crew_id"),
                  "passenger_ids": pids, "hotel_id": p.get("hotel_id") or item_facts.get("hotel_id"),
                  "gate": item_facts.get("gate"), "staff_id": p.get("staff_id") or item_facts.get("staff_id"),
                  "kind": (msgs[0].get("type") if msgs else None) or p.get("kind") or item_facts.get("kind")}
        rec = self.rec(agent, p.get("id"))
        flights = set(flights_in(rec.get("title"), rec.get("options"), p.get("title"), item_facts.get("title")))
        for m in msgs:
            flights |= set((m.get("trip_facts") or {}).get("itinerary") or [])
            if m.get("flight_id"):
                flights.add(m["flight_id"])
        if target["flight_id"]:
            flights.add(target["flight_id"])
        return target, sorted(flights), pids

    def title(self, agent, action, proposal, item_facts):
        rec = self.rec(agent, (proposal or {}).get("id"))
        t = rec.get("title") or (proposal or {}).get("title") or item_facts.get("title")
        if t:
            return t
        f = (proposal or {}).get("flight_id") or item_facts.get("flight_id")
        n = item_facts.get("messages")
        return " ".join(x for x in (action.replace("_", " ").capitalize(), f or "", f"({n} messages)" if n else "") if x)

    def safety_legal(self, proposal, settings):
        groups = settings["priority"]["safety_legal_decisions"]
        ids = set((proposal or {}).get("decision_ids") or [])
        kinds = [k for k, ds in groups.items() if ids & set(ds)]
        rung = (proposal or {}).get("safety_rung")
        if isinstance(rung, int) and rung <= settings["priority"]["safety_rung_max"]:
            kinds.append(f"safety rung {rung}")
        return kinds

    # ------------------------------------------------------------ why
    def inputs(self, agent, now, proposal):
        """The shared versions the proposing agent used: its own version at ask time and the upstream versions it records."""
        out = []
        table = AGENT_TABLE.get(agent)
        if table:
            row = self.latest(table, now)
            if row is not None:
                out.append({"kind": agent, "version": row["version"], "t": hhmm(row[SHARED[table]["time"]])})
                for col, kind in UPSTREAM.get(table, {}).items():
                    v = row.get(col)
                    up = self.latest(AGENT_TABLE[kind], now, v) if v is not None else None
                    if up is not None:
                        out.append({"kind": kind, "version": v, "t": hhmm(up[SHARED[AGENT_TABLE[kind]]["time"]])})
        elif agent in ("communication", "orchestrator"):
            sit = self.latest("situation_assessments", now)
            if agent == "communication" and getattr(self.c, "view", None) is not None and self.c.view.version:
                sit = self.latest("situation_assessments", now, self.c.view.version) or sit
            if sit is not None:
                out.append({"kind": "situation", "version": sit["version"], "t": hhmm(sit["assessed_at"])})
            pv = next((m.get("plan_version") for m in (proposal or {}).get("messages") or [] if m.get("plan_version")), None)
            if pv is not None:
                row = self.latest("recovery_plans", now, pv)
                if row is not None:
                    out.append({"kind": "network_recovery", "version": pv, "t": hhmm(row["planned_at"])})
        return out

    def triggers(self, agent, now, flights, limit=4):
        """The events that started it: the latest delivered events about its flights (else the latest the agent was given)."""
        evs = [e for e in self.c.store.events if e["id"] > 0 and (e.get("published_at") or e["occurred_at"]) <= now]
        mine = [e for e in evs if (e.get("payload") or {}).get("flight_id") in set(flights)] if flights else []
        con = self.contracts.get(agent)
        if not mine and con is not None:
            mine = [e for e in evs if e["type"] in con.event_types and e["type"] not in ("agent_heartbeat", "trip_record_updated")]
        out = []
        for e in mine[-limit:]:
            p = e.get("payload") or {}
            bits = [p.get(k) for k in ("flight_id", "reason", "new_departure", "expected_end", "diverted_to") if p.get(k)]
            bits = [hhmm(b) if isinstance(b, str) and re.match(r"\d{4}-\d\d-\d\dT", b) else str(b) for b in bits]
            out.append({"t": hhmm(e["occurred_at"]), "source": self.feed_of(agent, e["type"]),
                        "text": e["type"].replace("_", " ") + (": " + ", ".join(bits) if bits else "")})
        return out

    def options_considered(self, agent, action, proposal, now):
        """Only the options the agent itself produced (never invented)."""
        rec = self.rec(agent, (proposal or {}).get("id"))
        opts = list(rec.get("options") or [])
        chosen = opts[0].get("id") if opts else None        # the option it proposes is its first
        if agent == "network_recovery" and action in CREW_ACTS:     # the crew options are on the matching legality warning
            f = (proposal or {}).get("flight_id")
            ctx = self.context(agent)
            warn = [r for (t, _), r in ctx.saved.items() if t == "recommendations" and r.get("_action") == "flag_crew_legality"
                    and f and f in (r.get("title") or "") and (r.get("created_at") or now) <= now]
            if warn:
                opts = list(warn[-1].get("options") or [])
        out = []
        for o in opts:
            if o.get("id") == chosen:
                continue
            s = o.get("summary") or o.get("id") or ""
            if "not legal:" in s:
                out.append({"option": s.split(":")[0], "why_not": s.split("not legal:", 1)[1].strip()})
            elif s.endswith(": legal"):
                out.append({"option": s.split(":")[0], "why_not": "legal, but the agent ranked it below the option it proposes"})
            else:
                out.append({"option": s, "why_not": "the agent ranked it below the option it proposes"})
        return out

    def why_human(self, agent, action, proposal, role):
        from backend.rules.autonomy import autonomy_for
        ids = list((proposal or {}).get("decision_ids") or [])
        d = next((self.decisions[i] for i in ids if i in self.decisions), None)
        by = (d or {}).get("decided_by") or {}
        try:
            reason = autonomy_for(action, self.rules, safety_rung=(proposal or {}).get("safety_rung"), owner=by.get("role"))["reason"]
        except Exception:                                   # noqa: BLE001 - a decision with no owner: say so, never guess
            reason = "no automatic level is allowed"
        level = {"needs_approval": "needs approval", "human_only": "a person only"}.get(by.get("level"), by.get("level") or "needs approval")
        who = by.get("role") or role
        return f"{', '.join(ids) or action}: decided by {who.replace('_', ' ')} ({level}); {reason}"

    def why(self, q, now):
        rec = self.rec(q.agent, q.rec_id)
        plan = rec.get("title") or q.title
        if rec.get("why"):
            plan = f"{plan}. {rec['why']}"
        return {"plan": plan, "trigger": self.triggers(q.agent, now, q.flights),
                "inputs": self.inputs(q.agent, now, q.proposal),
                "options_considered": self.options_considered(q.agent, q.action, q.proposal, now),
                "rule_check": {"ok": rec.get("rule_check_ok", True), "rules": list(rec.get("rule_ids") or []),
                               "text": "; ".join(rec.get("rule_check_notes") or []) or "passed every rule it was checked against"},
                "why_human": self.why_human(q.agent, q.action, q.proposal, q.role)}

    # ------------------------------------------------------------ provenance: a plan built on a still-open proposal
    def built_on(self, q, now):
        """Network Recovery proposal ids (still in its published plan) that the plan behind this question was built on."""
        if q.agent == "network_recovery":
            return set()
        ver = next((i["version"] for i in q.why.get("inputs", []) if i["kind"] == "network_recovery"), None)
        row = self.latest("recovery_plans", now, ver) if ver is not None else None
        ids = set()
        for col in ("delays", "cancels", "crew_actions"):
            for it in (row or {}).get(col) or []:
                if it.get("proposal_id") is not None and (not q.flights or it.get("flight_id") in q.flights):
                    ids.add(it["proposal_id"])
        return ids

    # ------------------------------------------------------------ the trace (a small flowchart, saved at ask and at answer)
    def trace(self, q, now, snapshot="asked"):
        nodes, edges = [], []

        def node(kind, label, t, detail=None):
            nid = f"n{len(nodes) + 1}"
            nodes.append({"id": nid, "kind": kind, "label": label, "t": t, "detail": detail or {}})
            return nid

        def edge(a, b, label, t):
            edges.append({"from": a, "to": b, "label": label, "t": t})

        why = q.why or {}
        feeds = [node("feed", f"{e['source']}: {e['text']}", e["t"], e) for e in why.get("trigger", [])]
        versions = {}
        for i in why.get("inputs", []):
            versions[(i["kind"], i["version"])] = node(i["kind"], f"{NAMES.get(i['kind'], i['kind'])} v{i['version']}", i["t"], i)
        own = next((versions[k] for k in versions if k[0] == q.agent), None)
        if own is None:                                     # an agent without a shared table: its proposal is its output
            own = node(q.agent, f"{NAMES.get(q.agent, q.agent)} proposal #{q.rec_id}", q.asked, {"recommendation": q.rec_id})
        sit = next((v for k, v in versions.items() if k[0] == "situation"), None)
        for f in feeds:                                     # the world reaches Situation first (the agreed order), else the agent
            edge(f, sit or own, "used", nodes[int((sit or own)[1:]) - 1]["t"])
        for k, nid in versions.items():
            if nid == own:
                continue
            edge(nid, own, "used", nodes[int(own[1:]) - 1]["t"])
            if k[0] != "situation" and sit is not None and nid != sit:
                edge(sit, nid, "used", nodes[int(nid[1:]) - 1]["t"])
        rc = why.get("rule_check") or {}
        check = node("rule_check", "rule check: " + ("passed" if rc.get("ok", True) else "blocked"), q.asked,
                     {"rules": rc.get("rules", []), "text": rc.get("text", "")})
        edge(own, check, "sent to", q.asked)
        log = self.c.log[-1] if getattr(self.c, "log", None) else {}
        orch = node("orchestrator", f"orchestrator {q.asked}: queue {log.get('queue', 0)}, incidents "
                    + (", ".join(f"#{i['id']} {i['status']}" for i in log.get("incidents") or []) or "none"), q.asked,
                    {"incidents": log.get("incidents") or [], "queue": log.get("queue", 0)})
        edge(check, orch, "sent to", q.asked)
        qn = node("question", q.title, q.asked, {"question_id": q.id, "role": q.role, "deadline": q.deadline})
        edge(orch, qn, "sent to", q.asked)
        if snapshot == "answered" and q.answer:
            hn = node("human", f"{q.answer.get('by', 'human:' + q.role)}: {q.answer.get('decision')}", q.answer.get("t", ""), q.answer)
            edge(qn, hn, "answered", q.answer.get("t", ""))
        return {"question_id": q.id, "snapshot": snapshot, "nodes": nodes, "edges": edges}

    # ------------------------------------------------------------ the whole system (flow)
    def flow(self, now, since: int, waiting: dict):
        agents, edges = [], []
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for key in ("situation", "network_recovery", "passenger_care", "station_staff", "communication"):
            beats = self.c.store.heartbeats.get(key) or []
            last = beats[-1] if beats else (now if key == "communication" else None)
            versions = []
            table = AGENT_TABLE.get(key)
            if table:
                tcol = SHARED[table]["time"]
                rows = [r for r in self.c.store.records[table] if r[tcol] <= now and minute(r[tcol]) >= since and r[tcol] >= day0]
                prev = None
                for r in rows:
                    used = []
                    if key == "situation":
                        lo = prev[tcol] if prev else day0
                        feeds = {self.feed_of("situation", e["type"]) for e in self.c.store.events
                                 if e["id"] > 0 and lo < (e.get("published_at") or e["occurred_at"]) <= r[tcol]}
                        used = [{"kind": "feed", "ref": f} for f in sorted(feeds)]
                    for col, kind in UPSTREAM.get(table, {}).items():
                        if r.get(col) is not None:
                            used.append({"kind": kind, "ref": r[col]})
                            edges.append({"from": f"{kind}:{r[col]}", "to": f"{key}:{r['version']}", "t": hhmm(r[tcol])})
                    versions.append({"v": r["version"], "t": hhmm(r[tcol]), "used": used})
                    prev = r
            agents.append({"key": key, "name": NAMES[key], "last_run": hhmm(last) if last else None, "versions": versions,
                           "waiting_on_human": waiting.get(key, 0)})
        return agents, edges
