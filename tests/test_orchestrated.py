"""Phase 5b: the orchestrator, part 2: approved decisions change the world (orchestrated mode), physics shared by every mode."""
from datetime import datetime, timedelta

import pytest
import yaml

from backend.settings import ROOT


@pytest.fixture(scope="module")
def three():
    from backend.seed.build import build
    from simulator.run import simulate
    d = build()
    return {m: simulate(7, "a31_storm", mode=m, data=d) for m in ("today", "copilot", "orchestrated")}


def test_the_c35_swap_is_applied_wn1033_flies_and_maya_is_told_the_real_time(three):
    r = three["orchestrated"]
    w, o = r.world, r.copilot
    swap = [a for a in w.applied if a["action"] == "swap_crews" and a["facts"]["flight_id"] == "WN1033" and a["applied"]]
    assert swap and swap[0]["facts"]["crew_id"] == "C35"
    f = w.flights["WN1033"]
    assert f["status"] in ("departed", "arrived") and f["_role_crew"]["fa"] == "C35" and not f["cancel_reason"]
    assert w.truth["PAX0001"]["status"] == "confirmed" and list(w.truth["PAX0001"]["itinerary"]) == ["WN2031", "WN1033"]
    left = f"{f['actual_dep'] // 60:02d}:{f['actual_dep'] % 60:02d}"
    told = [e["payload"]["text"] for e in w.events if e["type"] == "message_delivered" and e["payload"].get("passenger_id") == "PAX0001"]
    assert any(f"leave at {left}" in t or f"around {left}" in t for t in told)                 # the time she really left
    assert not any("cancelled" in t for t in told)                                              # never told it was cancelled
    crew = [e["payload"]["text"] for e in w.events if e["type"] == "crew_message_delivered" and e["payload"].get("crew_id") == "C35"]
    assert crew and "Please report by" in crew[0]
    assert three["today"].world.flights["WN1033"]["status"] == "cancelled"                       # today's process cancels it


def test_no_crew_legality_violation_and_no_promise_in_any_mode(three):
    for mode, r in three.items():
        assert r.world.crew_violations == [], mode
        assert r.metrics["three_ways"]["promises_found"] == 0, mode


def test_every_applied_change_has_an_approval_and_an_audit_record(three):
    r = three["orchestrated"]
    o, w = r.copilot, r.world
    applied = [a for a in o.applied_log if a["applied"]]
    assert applied and len(applied) == sum(1 for a in w.applied if a["applied"])
    audit = {(a["what"], str(a["entity_id"])) for a in o.octx.audit} | {(a["what"], str(a["entity_id"]))
                                                                       for a in r.world.audit_rows if a.get("actor") == "agent"}
    for a in o.applied_log:
        assert a["approved_by"], a
        assert ("decision_applied" if a["applied"] else "decision_refused", str(a["recommendation"])) in audit, a
    answered = {(i.agent, m[1]) for i in o.queue.items if i.status == "answered" for m in i.members}
    for a in applied:
        assert (a["agent"], a["recommendation"]) in answered, a                                  # a person approved it first
    applied_events = [e for e in w.events if e["type"] == "recovery_action_applied"]
    assert len(applied_events) == sum(1 for a in applied if a["action"] != "rebook_passenger")


def test_every_fallback_to_todays_process_is_logged(three):
    r = three["orchestrated"]
    w = r.world
    events = [e for e in w.events if e["type"] == "process_fallback"]
    assert w.fallbacks and len(events) == len(w.fallbacks)
    assert {f["process"] for f in w.fallbacks} <= {"schedule thinning", "cancel cutoff", "reserve choice", "rebooking"}
    assert not three["today"].world.fallbacks and not three["copilot"].world.fallbacks


def test_physics_is_the_same_in_every_mode_and_matches_station_and_staff():
    sim = yaml.safe_load((ROOT / "config" / "simulator.yaml").read_text(encoding="utf-8"))["physics"]["ground"]
    st = yaml.safe_load((ROOT / "config" / "agent_settings" / "station_staff.yaml").read_text(encoding="utf-8"))
    assert sim["southwest_gates"] == st["gates"]["southwest_gates"] and sim["remote_stands"] == st["gates"]["remote_stands"]
    for kind in ("departure", "arrival"):
        for role in ("ramp", "gate_agent"):
            w = st["staff"][kind][role]
            assert sim[kind][role] == [w["count"], w["before_min"] + w["after_min"]], (kind, role)


def test_copilot_is_still_advice_only_and_three_ways_are_measured(three):
    from simulator.comparison import truth_fingerprint
    assert truth_fingerprint(three["today"]) == truth_fingerprint(three["copilot"])
    assert three["copilot"].metrics["passenger_minutes_of_disagreement"] == 6180
    t, c, o = (three[m].metrics["three_ways"] for m in ("today", "copilot", "orchestrated"))
    assert t["cancellations"] == c["cancellations"] == 5 and o["cancellations"] < t["cancellations"]
    assert o["stranded_overnight"] < t["stranded_overnight"]
    assert t["ground_waits"] == c["ground_waits"] == 0
    from simulator.comparison_page import three_ways_section
    html = three_ways_section({m: three[m].metrics["three_ways"] for m in three})
    assert "Three ways" in html and "Maya's day: Orchestrated" in html


def test_5a_fixes_no_same_gate_conflict_and_no_role_over_the_limit(three):
    o = three["copilot"].copilot
    assert not [c for c in o.conflicts if c["kind"] == "resource_used_twice" and c.get("resource_kind") == "gate"]
    m = three["copilot"].metrics["orchestrator"]
    assert all(n <= m["workload_limit"] for hs in m["by_role_hour"].values() for n in hs.values())


def test_deadlines_come_from_each_decisions_own_deadline():
    from backend.orchestrator.queue import deadline_for
    s = {"immediate_min": 10, "default_deadline_min": 30}
    now = datetime.fromisoformat("2026-10-06T14:00:00-05:00")
    assert deadline_for({"deadline": "within 15 min of the stop being announced"}, now, s) == now + timedelta(minutes=15)
    assert deadline_for({"deadline": "immediately"}, now, s) == now + timedelta(minutes=10)
    assert deadline_for({"deadline": "before 22:00"}, now, s) == now.replace(hour=22)
    assert deadline_for({"deadline": "before the flight departs"}, now, s) == now + timedelta(minutes=30)


def test_the_duty_manager_answers_an_escalated_item_for_the_role_it_was_for():
    from random import Random
    from backend.execution.simulated_human import SimulatedHumans
    from backend.orchestrator.queue import Queue
    s = yaml.safe_load((ROOT / "config" / "orchestrator.yaml").read_text(encoding="utf-8"))
    people = SimulatedHumans(Random(1), None, {"response_min": {"station_manager": {"normal": [60, 60], "urgent": [60, 60]},
                                                                "director_of_operations": {"normal": [3, 3], "urgent": [3, 3]}},
                                               "handles": {"station_manager": ["move_gate_agents"]},
                                               "duty_manager": "director_of_operations"})
    q = Queue(people, {"queue": {**s["queue"], "batch": {"window_min": 15, "kinds": {}}}}, {}, {"E04": {"deadline": "within 15 min"}})
    now = datetime.fromisoformat("2026-10-06T14:00:00-05:00")
    item = q.ask("station_staff", 7, {"action": "move_gate_agents", "decision_ids": ["E04"]}, "station_manager", now, True)
    q.escalate(now + timedelta(minutes=15), s["queue"]["escalation_chain"])
    assert item.alerted == ["director_of_operations"]
    got = q.due(now + timedelta(minutes=18))
    assert len(got) == 1 and got[0].role == "station_manager" and got[0].answer["answered_by"] == "director_of_operations"
    assert item.answered_by == "director_of_operations" and q.due(now + timedelta(minutes=60)) == []   # answered once only
