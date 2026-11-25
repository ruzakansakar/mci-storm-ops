"""Phase 5a: the orchestrator (coordination only). Unit tests for the queue, conflicts and the shared-state audit; seed 7 checks."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.orchestrator import audit as A
from backend.orchestrator import conflicts as C
from backend.orchestrator.queue import Queue

T = datetime.fromisoformat("2026-10-06T14:00:00-05:00")
SETTINGS = {"queue": {"batch": {"window_min": 15, "kinds": {"station_staff": ["call_in_ground_staff"]}},
                      "escalation_chain": {"station_manager": "director_of_operations"}, "workload_max_per_hour": 2,
                      "deadlines": {"immediate_min": 10, "default_deadline_min": 30, "escalated_answer_min": 20},
                      "duty_manager": "director_of_operations"}}
RULES = {"rule_check": {"human_answer_deadline_min": 10}}


class FakePeople:
    """Stand-in people: they answer what they handle after `after` minutes; they never see a blocked proposal."""
    def __init__(self, handles=(), after=5):
        self.handles, self.after, self.asked, self.refused, self.withdrawn = set(handles), after, [], 0, []

    def ask(self, agent, rec_id, proposal, role, now, ok):
        if not ok:
            self.refused += 1
            return None
        q = SimpleNamespace(agent=agent, rec_id=rec_id, action=proposal.get("action"), role=role, asked_at=now,
                            due_at=now + timedelta(minutes=self.after), proposal=proposal, answer={},
                            status="waiting" if proposal.get("action") in self.handles else "not_handled")
        self.asked.append(q)
        return q

    def withdraw(self, agent, rec_id):
        self.withdrawn.append((agent, rec_id))

    def due(self, now):
        out = []
        for q in self.asked:
            if q.status == "waiting" and q.due_at <= now:
                q.status = "answered"
                q.answer = {"decision": "approved", "waited_min": (now - q.asked_at).total_seconds() / 60}
                out.append(q)
        return out


def p(action, i, **kw):
    return {"id": i, "action": action, "decision_ids": ["E06"], "title": f"{action} {i}", **kw}


# ------------------------------------------------------------------ the queue
def test_the_queue_escalates_an_unanswered_item_and_a_late_answer_still_counts():
    people = FakePeople(handles={"call_in_ground_staff"}, after=25)
    q = Queue(people, SETTINGS, RULES, {"E06": {"deadline": "within 10 min"}})
    item = q.ask("station_staff", 1, p("call_in_ground_staff", 1), "station_manager", T, True)
    assert item.deadline == T + timedelta(minutes=10)                   # the decision's own deadline (5b)
    q.escalate(T + timedelta(minutes=9), SETTINGS["queue"]["escalation_chain"])
    assert not item.alerted
    q.escalate(T + timedelta(minutes=10), SETTINGS["queue"]["escalation_chain"])
    assert item.alerted == ["director_of_operations"] and any(k == "decision_escalated" for k, _ in q.events)
    esc = next(e for k, e in q.events if k == "decision_escalated")
    assert esc["from_role"] == "station_manager" and esc["to_role"] == "director_of_operations" and esc["decision"] == "S06"
    q.escalate(T + timedelta(minutes=30), SETTINGS["queue"]["escalation_chain"])
    assert item.alerted == ["director_of_operations"]                   # the end of the chain: once only
    answered = q.due(T + timedelta(minutes=25))
    assert [a.rec_id for a in answered] == [1] and item.status == "answered"


def test_routine_approvals_of_the_same_kind_are_one_queue_item_and_the_answer_goes_to_each():
    people = FakePeople(handles={"call_in_ground_staff"}, after=8)
    q = Queue(people, SETTINGS, RULES, {})
    a = q.ask("station_staff", 1, p("call_in_ground_staff", 1), "station_manager", T, True)
    b = q.ask("station_staff", 2, p("call_in_ground_staff", 2), "station_manager", T + timedelta(minutes=5), True)
    c = q.ask("station_staff", 3, p("call_in_ground_staff", 3), "station_manager", T + timedelta(minutes=20), True)
    assert a is b and c is not a and len(people.asked) == 2                # one person-question for the first two
    got = q.due(T + timedelta(minutes=8))
    assert sorted(x.rec_id for x in got) == [1, 2] and {x.answer["decision"] for x in got} == {"approved"}
    assert next(x for x in got if x.rec_id == 2).answer["waited_min"] == 3.0


def test_duplicates_are_merged_and_blocked_proposals_are_never_shown():
    people = FakePeople(handles={"move_gate_agents"}, after=5)
    q = Queue(people, SETTINGS, RULES, {})
    same = {"action": "move_gate_agents", "decision_ids": ["E04"], "flight_id": "WN1033", "tasks": ["X"]}
    one = q.ask("station_staff", 10, {**same, "id": 10}, "station_manager", T, True)
    two = q.ask("station_staff", 11, {**same, "id": 11}, "station_manager", T, True)
    assert one is two and one.merged == 1 and len(people.asked) == 1
    assert sorted(x.rec_id for x in q.due(T + timedelta(minutes=5))) == [10, 11]
    blocked = q.ask("station_staff", 12, {**same, "id": 12, "tasks": ["Y"]}, "station_manager", T, False)
    assert blocked.status == "refused_blocked" and people.refused == 1 and blocked not in q.ordered()


def test_the_queue_order_is_safety_rung_then_deadline_then_passengers():
    q = Queue(FakePeople(), SETTINGS, RULES, {"F02": {"deadline": "within 5 min"}})                 # the others: default 30
    late = q.ask("a", 1, {"action": "x", "decision_ids": [], "safety_rung": 5, "passenger_ids": ["P1"]}, "r", T, True)
    many = q.ask("a", 2, {"action": "y", "decision_ids": [], "safety_rung": 5, "passenger_ids": ["P1", "P2"]}, "r", T, True)
    soon = q.ask("a", 3, {"action": "z", "decision_ids": ["F02"], "safety_rung": 5}, "r", T, True)
    life = q.ask("a", 4, {"action": "w", "decision_ids": [], "safety_rung": 1}, "r", T + timedelta(minutes=9), True)
    assert [i.rec_id for i in q.ordered()] == [4, 3, 2, 1]


def test_workload_is_counted_per_role_per_hour_and_flagged_over_the_limit():
    q = Queue(FakePeople(), SETTINGS, RULES, {})
    for i in range(3):
        q.ask("a", i, {"action": f"k{i}", "decision_ids": []}, "station_manager", T + timedelta(minutes=i), True)
    counts = q.workload(T + timedelta(minutes=5), 2)
    assert counts["station_manager"] == 3 and any(k == "workload_high" for k, _ in q.events)


# ------------------------------------------------------------------ cross-agent conflicts (each kind)
def test_each_cross_agent_conflict_is_caught():
    reb = C.rebooking_on_cancelled([{"agent": "passenger_care", "rec_id": 5, "flight_id": "WN1042", "passenger_ids": ["P1"]}],
                                   {"WN1042": 12})
    assert reb and reb[0]["block"] == [("passenger_care", 5)] and set(reb[0]["views"]) == {"passenger_care", "network_recovery"}
    station = {"ramp_status": {"open": True}, "staff_shortages": [
        {"role": "ramp", "from": "2026-10-06T14:00:00-05:00", "to": "2026-10-06T14:15:00-05:00", "departures_workable": 0}]}
    dep = C.departure_without_capacity([{"flight_id": "WN1027", "new_departure": "2026-10-06T14:05:00-05:00"}], station, 40)
    assert dep and dep[0]["kind"] == "departure_without_capacity" and not dep[0]["block"]           # escalated, both views
    closed = {"ramp_status": {"open": False, "expected_reopen": "2026-10-06T12:45:00-05:00"}}
    assert C.departure_without_capacity([{"flight_id": "WN1027", "new_departure": "2026-10-06T12:30:00-05:00"}], closed)
    draft = {"id": "N1", "proposal_id": 7, "status": "held", "audience": "passenger", "text": "...", "change": {"flight_id": "WN1033"}}
    assert C.draft_contradicts_plan([draft], {}, 66)[0]["block"] == [("communication_draft", "N1")]          # proposal gone
    assert C.draft_contradicts_plan([draft], {7: {"flight_id": "WN1033", "new_departure": "x"}}, 66)        # proposal changed
    assert not C.draft_contradicts_plan([draft], {7: {"flight_id": "WN1033"}}, 66)                         # the same: fine
    crew = C.resource_used_twice("crew", [
        {"resource": "C35", "flight_id": "WN1033", "from": None, "to": None, "agent": "network_recovery", "rec_id": 1, "applied": False},
        {"resource": "C35", "flight_id": "WN1034", "from": None, "to": None, "agent": "network_recovery", "rec_id": 2, "applied": False}])
    assert crew and crew[0]["block"] == [("network_recovery", 2)]
    ac = C.resource_used_twice("aircraft", [
        {"resource": "A31", "flight_id": "WN2031", "from": None, "to": None, "agent": "network_recovery", "rec_id": None},
        {"resource": "A31", "flight_id": "WN1039", "from": None, "to": None, "agent": "network_recovery", "rec_id": None}])
    assert ac and ac[0]["resource_kind"] == "aircraft"
    g = lambda f, a, b, applied, rid: {"resource": "G05", "flight_id": f, "from": a, "to": b, "agent": "station_staff",
                                       "rec_id": rid, "applied": applied}
    gate = C.resource_used_twice("gate", [g("WN2030", "2026-10-06T14:00:00-05:00", "2026-10-06T14:50:00-05:00", True, 1),
                                          g("WN2031", "2026-10-06T14:05:00-05:00", "2026-10-06T14:55:00-05:00", False, 2)])
    assert gate and gate[0]["block"] == [("station_staff", 2)]                                              # the newer, waiting one
    assert not C.resource_used_twice("gate", [g("WN2030", "2026-10-06T14:00:00-05:00", "2026-10-06T14:50:00-05:00", True, 1),
                                              g("WN2031", "2026-10-06T14:50:00-05:00", "2026-10-06T15:30:00-05:00", False, 2)])
    s = lambda f, a, b, applied, rid: {"resource": "RAM008", "flight_id": f, "task": "departure", "from": a, "to": b,
                                       "agent": "station_staff", "rec_id": rid, "applied": applied}
    staff = C.resource_used_twice("staff", [s("WN1027", "2026-10-06T13:20:00-05:00", "2026-10-06T14:00:00-05:00", True, 3),
                                            s("WN1028", "2026-10-06T13:30:00-05:00", "2026-10-06T14:10:00-05:00", False, 4)])
    assert staff and staff[0]["block"] == [("station_staff", 4)]


# ------------------------------------------------------------------ the shared-state audit
def test_a_deliberately_stale_read_is_flagged_and_a_fresh_one_is_not():
    audits = [{"what": "read shared recovery_plans", "after": {"version": 60}},
              {"what": "read shared situation_assessments", "after": {"version": 18}}]
    read = A.reads_from(audits)
    stale = A.audit_agent("communication", read, {"recovery_plans": 66, "situation_assessments": 18}, [])
    assert [f["kind"] for f in stale] == ["stale_read"] and "recovery_plans v60 when v66 existed" in stale[0]["detail"]
    assert not A.audit_agent("communication", read, {"recovery_plans": 60, "situation_assessments": 18}, [])
    row = {"version": 9, "situation_version": 17}
    rec = A.audit_agent("passenger_care", {"situation_assessments": 18}, {"situation_assessments": 18}, [row])
    assert rec and rec[0]["kind"] == "stale_record"


# ------------------------------------------------------------------ seed 7
@pytest.fixture(scope="module")
def seed7():
    from backend.seed.build import build
    from simulator.run import simulate
    return simulate(7, "a31_storm", mode="copilot", data=build())


def test_seed7_no_stale_chain_one_incident_and_every_proposal_rule_checked_first(seed7):
    o = seed7.copilot
    assert o.findings == []
    assert len(o.incidents.all) == 1 and o.incidents.all[0].kind == "weather" and o.incidents.all[0].status == "resolved"
    shown = [i for i in o.queue.items if i.kind != "conflict" and i.status != "refused_blocked"]
    saved = {"communication": o.context.saved, "situation": o.situation.context.saved, "network_recovery": o.network.context.saved,
             "passenger_care": o.care.context.saved, "station_staff": o.station.context.saved, "orchestrator": o.octx.saved}
    for i in shown:
        for agent, rid, _, _ in i.members:
            assert saved[agent][("recommendations", rid)].get("rule_check_ok") is True, (agent, rid)
    assert len(o.log) > 1000 and all(r["at"] for r in o.log)                                    # one row a minute


def test_seed7_conflicts_are_caught_with_both_views_and_blocked_advice_never_reaches_a_person(seed7):
    o = seed7.copilot
    # 5b: Network Recovery no longer double-counts cancellations and the station's numbers were calibrated, so these two
    # kinds no longer occur on seed 7 (each kind is still caught in test_each_cross_agent_conflict_is_caught); no same-gate one
    assert not [c for c in o.conflicts if c.get("resource_kind") == "gate"]
    assert all(len(c["views"]) == 2 for c in o.conflicts)
    for b in o.blocked:
        if "recommendation" in b:
            ctx = {"passenger_care": o.care.context, "station_staff": o.station.context}[b["agent"]]
            assert ctx.saved[("recommendations", b["recommendation"])]["status"] == "blocked_by_orchestrator"
            assert not [q for q in o.people.questions if q.agent == b["agent"] and q.rec_id == b["recommendation"]
                        and q.status == "answered"]


def test_seed7_work_is_tagged_with_the_incident_and_the_page_section_renders(seed7):
    o = seed7.copilot
    inc = o.incidents.all[0]
    during = [t for t in o.tags if inc.opened_at.isoformat() <= t["at"] < inc.resolved_at.isoformat()]
    assert during and all(t["incident_id"] == inc.id for t in during)
    assert all(t["incident_id"] is None for t in o.tags if t["at"] < inc.opened_at.isoformat())
    from simulator.comparison_page import orchestrator_section
    html = orchestrator_section(seed7.metrics)
    assert "How the agents connected at 14:15" in html and "The human queue" in html and "Incidents" in html


def test_seed7_recovery_drafts_say_the_outcome_and_crew_drafts_the_report_time(seed7):
    h = seed7.copilot.recovery.history
    pax = [d for d in h if d["audience"] == "passenger" and d["change"].get("crew_id")]
    crew = [d for d in h if d["audience"] == "crew"]
    assert pax and all("crew" not in d["text"].lower() and "expected to leave at" in d["text"] for d in pax)
    assert crew and all("Please report by" in d["text"] for d in crew)
    c35 = next(d for d in crew if d["crew_id"] == "C35" and d["flight_id"] == "WN1033")
    assert c35["text"].startswith("Crew C35: you are reassigned to WN1033. Please report by ")


# ------------------------------------------------------------------ the orchestrator owns the loop: same results as before
def test_the_orchestrator_gives_the_old_loops_results_when_batching_is_off(seed7, monkeypatch):
    """With routine batching switched off, the orchestrator's minute loop gives exactly the old co-pilot loop's results: the same
    shared plans (content), messages, rooms, gates, staff and wrong-screen minutes (it adds only its own records)."""
    import backend.orchestrator.loop as L
    from backend.execution.copilot import Copilot
    from backend.seed.build import build
    from simulator.run import simulate
    orig = L.load_settings

    def no_batching():
        s = orig()
        s["queue"]["batch"]["kinds"] = {}
        s["queue"]["duty_manager"] = None             # 5b: nor a duty manager answering escalations (the old loop has neither)
        return s
    monkeypatch.setattr(L, "load_settings", no_batching)
    d = build()
    orch = simulate(7, "a31_storm", mode="copilot", data=d)
    monkeypatch.setattr(L, "Orchestrator", Copilot)               # the old loop, the same agents
    old = simulate(7, "a31_storm", mode="copilot", data=d)
    pointers = {"agent_run_id", "situation_version", "recovery_plan_version", "care_plan_version", "station_plan_version"}
    content = lambda r, t: [{k: v for k, v in x.items() if k not in pointers} for x in r.copilot.store.records[t]]
    for t in ("situation_assessments", "recovery_plans", "station_plans"):
        assert content(orch, t) == content(old, t), t
    child = lambda r: [{**x, "passengers": [{k: v for k, v in p.items() if k != "child_status"} for p in x["passengers"]],
                        "vulnerable": [{k: v for k, v in v_.items() if k != "staff_supervisor"} for v_ in x["vulnerable"]]}
                       for x in content(r, "care_plans")]
    assert child(orch) == child(old)                                # blocked advice on a cancelled flight is the one change
    msgs = lambda r: [{k: v for k, v in m.items() if k != "rec"} for m in r.copilot.messages.log]
    assert msgs(orch) == msgs(old)
    assert orch.copilot.care.ledger == old.copilot.care.ledger
    assert orch.copilot.station.gate_ledger == old.copilot.station.gate_ledger
    assert orch.copilot.station.staff_ledger == old.copilot.station.staff_ledger
    assert orch.metrics["passenger_minutes_of_disagreement"] == old.metrics["passenger_minutes_of_disagreement"] == 6180
    assert orch.metrics["truth_sha256"] == old.metrics["truth_sha256"]


# ------------------------------------------------------------------ two incidents at once (3 fresh chaos days, in memory)
@pytest.fixture(scope="module")
def two_incident_days():
    import chaos.compare as cc
    import simulator.run as sr
    from backend.seed.build import build
    keep, orig, out = {}, sr.simulate, {}

    def spy(*a, **k):
        r = orig(*a, **k)
        keep[k.get("mode", "today")] = r
        return r
    cc.simulate = spy
    try:
        d = build()
        for seed in (5034, 5031, 5040):          # fresh seeds (calibration uses 1-100); each has two overlapping causes
            cc.compare(seed, d, True)
            out[seed] = keep["copilot"]
    finally:
        cc.simulate = orig
    return out


def test_two_incidents_at_once_get_their_own_ids_are_ranked_tagged_and_both_close(two_incident_days):
    for seed, r in two_incident_days.items():
        o = r.copilot
        inc = o.incidents.all
        assert len(inc) == 2 and len({i.id for i in inc}) == 2, seed
        a, b = sorted(inc, key=lambda i: i.opened_at)
        assert b.opened_at < a.resolved_at, seed                       # they overlap
        assert all(i.status == "resolved" for i in inc), seed           # both close
        ranked = [e for e in o.octx.saved.values() if isinstance(e, dict) and e.get("type") == "incident_ranked"]
        assert ranked, seed
        order = ranked[0]["payload"]["order"]
        assert order == [i.id for i in sorted(inc, key=lambda i: (i.rank, i.opened_at))], seed   # T05: safety first
        asked = {i.decision_ids[0] for i in o.queue.items if i.agent == "orchestrator" and i.kind != "conflict"}
        assert {"T05", "A10", "Q07"} <= asked, seed
        spans = {i.id: (i.opened_at.isoformat(), i.resolved_at.isoformat()) for i in inc}
        for t in o.tags:                                                # every piece of work belongs to an incident open then
            if t["incident_id"] is not None:
                lo, hi = spans[t["incident_id"]]
                assert lo <= t["at"] <= hi, (seed, t)
            else:
                assert not any(lo <= t["at"] < hi for lo, hi in spans.values()), (seed, t)
        for i in o.queue.items:
            if i.incident_id is not None:
                lo, hi = spans[i.incident_id]
                assert lo <= i.asked_at.isoformat() <= hi, (seed, i.id)
        assert o.findings == [], seed                                   # no stale chain on these days either
        work = {t["incident_id"] for t in o.tags if t["kind"] in ("recommendation", "message", "recovery_plans")}
        assert {i.id for i in inc} <= work, seed                         # 5b: EACH incident gets its own work
