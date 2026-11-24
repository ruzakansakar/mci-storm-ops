"""Phase 4b-3 part A: conflicts close on evidence or a human answer; holds are narrow, short and escalated; simulated people."""
import random
from datetime import timedelta

import pytest

from backend.agents.communication.detect import Memory, load_settings, scan
from backend.agents.communication.fix import FixMemory, propose_fixes
from backend.agents.communication.situation_view import SituationView, escalate_holds, held_fields, read_situation
from backend.agents.communication.state_at import clock
from backend.agents.situation import assess as A
from backend.execution.simulated_human import SimulatedHumans
from backend.rules.channel_fix import reasons
from tests.test_shared_state import two_flights, version
from tests.test_situation_fixes import FLIGHTS, T, ev, stop

S = load_settings()
SIT = A.load_settings()


def run_situation(events, times):
    """Situation at each time, with one memory (as a replay does); returns the conflicts of the last assessment."""
    from backend.contracts.feeds import Feeds
    from chaos.memory_replay import MemoryContext
    day = {"events": events, "schedule": FLIGHTS, "seed": 0}
    memory, sink = A.Memory(), {"versions": [], "events": [], "recs": []}
    out = None
    for t in times:
        out = A.assess(MemoryContext(day, T(t), Feeds(), sink), memory)["assessment"]
    return out, sink


def stop_and_fake_departure():
    return [stop(1, "11:30", "ground_stop_started", 13 * 60),
            ev(2, "11:40", "ramp_closed", {"reason": "lightning", "expected_end": 12 * 60 + 30}),
            ev(3, "11:45", "flight_departed", {"flight_id": "WN1005", "to": "DEN", "delay_min": 0})]


# ------------------------------------------------------------------ A1: Situation closes conflicts on evidence (a rule)

def test_a_later_delay_at_the_gate_settles_departed_while_ramp_closed_as_false():
    events = stop_and_fake_departure() + [ev(4, "12:00", "flight_delay_updated", {"flight_id": "WN1005", "delay_min": 90,
                                                                                  "new_departure": "14:30", "reason": "weather"})]
    a, _ = run_situation(events, ["11:46", "12:01"])
    c = next(c for c in a["conflicts"] if c["key"] == "departed-during-stop:WN1005")
    assert c["status"] == "settled" and c["side"] == c["sides"][1]          # "still at the gate"
    assert "false" in c["settled_by"] and c["settle_evidence"][0]["type"] == "flight_delay_updated"


def test_being_seen_airborne_settles_it_as_real():
    events = stop_and_fake_departure() + [ev(4, "12:30", "flight_diverted", {"flight_id": "WN1005", "diverted_to": "STL",
                                                                             "pax": 100, "reason": "weather"})]
    a, _ = run_situation(events, ["11:46", "12:31"])
    c = next(c for c in a["conflicts"] if c["key"] == "departed-during-stop:WN1005")
    assert c["status"] == "settled" and c["side"] == c["sides"][0] and "real" in c["settled_by"]


def test_nothing_settles_it_then_unresolved_needs_a_human_and_a_human_answer_closes_it():
    limit = SIT["conflicts"]["unresolved_after_min"]
    late = (T("11:45") + timedelta(minutes=limit + 1)).strftime("%H:%M")
    a, sink = run_situation(stop_and_fake_departure(), ["11:46", late])
    c = next(c for c in a["conflicts"] if c["key"].startswith("departed"))
    assert c["status"] == "unresolved" and "human" in c["needs"]
    assert any("A09" in r["decision_ids"] for r in sink["recs"])               # Situation asked (and never guessed)
    answer = ev(9, late, "human_decision_recorded", {"decision_id": "A09", "conflict_key": c["key"], "flight_id": "WN1005",
                                                     "answer_side": c["sides"][1], "decided_by_role": "network_operations_control",
                                                     "answered_at": late}, feed="decisions")
    a, _ = run_situation(stop_and_fake_departure() + [answer], ["11:46", late])
    c = next(c for c in a["conflicts"] if c["key"].startswith("departed"))
    assert c["status"] == "resolved_by_human" and f"network_operations_control) at {late}" in c["settled_by"]


# ------------------------------------------------------------------ A2: narrow holds, escalated at 15 and 45 minutes

def _conflict_version(n, at, status="open"):
    return version(n, at, conflicts=[{"key": "departed-during-stop:F1", "flight_ids": ["F1"], "fields": ["status"],
                                      "status": status, "opened_at": at.isoformat(),
                                      "sides": ["departed at 11:45 (flight ops)", "still at the gate (ground stop on, ramp closed)"]}])


def step(ctx, now, view, fixes, memory):
    ctx.now = now
    result = scan(ctx, memory, write=False)
    read_situation(ctx, view, S)
    return propose_fixes(ctx, result, fixes, view, S), escalate_holds(ctx, view, S)


def test_a_hold_covers_only_the_conflicted_detail_and_the_rest_is_fixed():
    ctx, view, fixes, memory = two_flights("11:51"), SituationView(), FixMemory(), Memory()
    # P1's trip also moved to another flight F2 (an itinerary change): that part must still be fixed
    ctx.events.append({"id": 99, "occurred_at": clock("11:50"), "type": "trip_record_updated",
                       "payload": {"passenger_id": "P1", "itinerary": ["F1", "F2"], "status": "cancelled", "destination": "BWI",
                                   "bag_routing": []}})
    ctx.store.records["situation_assessments"].append(_conflict_version(1, clock("11:50")))
    ctx.store.heartbeats["situation"] = [clock("11:50")]
    fixes_out, _ = step(ctx, clock("11:51"), view, fixes, memory)
    p1 = [p for p in fixes_out if p["passenger_id"] == "P1"]
    assert p1 and all(p["held_fields"] == ["status"] and "status" not in p["confirmed"] for p in p1)
    assert all(p["confirmed"]["itinerary"] == ["F1", "F2"] for p in p1)          # the itinerary change goes out now
    # the rule check: a held detail may be left out only while Situation's conflict is open about it
    world = {"confirmed_trips": {"P1": {"itinerary": ["F1", "F2"], "status": "cancelled", "destination": "BWI", "bag_routing": []}},
             "trip_versions": {"P1": p1[0]["truth_as_of"]}, "truth_feed_fresh": True, "flight_feed_fresh": True,
             "conflicts_known": True, "open_conflicts": {"F1"}, "trip_flights": {"P1": {"F1", "F2"}},
             "conflict_held_fields": {"P1": ["status"]}, "screens": {"P1": ("app", "sms", "gate_screen", "call_center")},
             "contact_types": {"P1": ["app", "sms"]}}
    assert not reasons(p1[0], world)
    assert reasons(p1[0], {**world, "conflict_held_fields": {}})                   # no open conflict: leaving it out is refused


def test_a_held_correction_leaves_the_conflicted_detail_on_the_screen_as_it_was():
    from copy import deepcopy
    from simulator.channels import Channels
    from simulator.config import load_config
    cfg = deepcopy(load_config())
    cfg["channels"]["app"].update(base_min=1, jitter_min=0, per_100_changes_min=0, fail_chance=0)
    truth = {"P1": {"itinerary": ("F1", "F2"), "status": "cancelled", "destination": "BWI", "bag_routing": (), "message": "x"}}
    ch = Channels(cfg, random.Random(1), {"P1": {**truth["P1"], "itinerary": ("F1",), "status": "confirmed"}}, 300)
    ch.truth = truth
    ch.fix_rng = random.Random(2)
    assert ch.deliver_fix(600, {"id": 1, "passenger_id": "P1", "channel": "app", "held_fields": ["status"],
                                "confirmed": {"itinerary": ["F1", "F2"], "destination": "BWI", "bag_routing": []}})["accepted"]
    ch.apply_due(601)
    assert ch.shown[("P1", "app")]["itinerary"] == ("F1", "F2") and ch.shown[("P1", "app")]["status"] == "confirmed"


def test_holding_asks_network_operations_at_15_and_urgently_at_45_minutes_counts_only():
    ctx, view, fixes, memory = two_flights("11:51"), SituationView(), FixMemory(), Memory()
    ctx.store.records["situation_assessments"].append(_conflict_version(1, clock("11:50")))
    hb = ctx.store.heartbeats.setdefault("situation", [])
    asked = []
    for minute in range(0, 50):
        now = clock("11:51") + timedelta(minutes=minute)
        hb.append(now)
        asked += [(minute, q) for q in step(ctx, now, view, fixes, memory)[1]]
    assert [m for m, _ in asked] == [S["conflicts"]["ask_after_min"], S["conflicts"]["urgent_after_min"]]
    normal, urgent = asked[0][1], asked[1][1]
    assert normal["action"] == "resolve_source_conflict" and normal["decision_ids"] == ["A09"] and not normal["urgent"]
    assert urgent["urgent"]
    titles = [r["title"] for (t, _), r in ctx.saved.items() if t == "recommendations" and "A09" in r["decision_ids"]]
    assert titles[0].startswith("Flight F1 has conflicting data (departed at 11:45 (flight ops) vs still at the gate")
    assert "1 passenger is affected; please confirm which is true." in titles[0]
    assert titles[1].startswith("URGENT: passengers are seeing information we can't confirm.")
    assert all(r["approver_role"] == "network_operations_control" for (t, _), r in ctx.saved.items()
               if t == "recommendations" and "A09" in r["decision_ids"])


def test_a_human_answer_closes_the_conflict_and_fixing_resumes_fully():
    ctx, view, fixes, memory = two_flights("11:51"), SituationView(), FixMemory(), Memory()
    recs = ctx.store.records["situation_assessments"]
    recs.append(_conflict_version(1, clock("11:50")))
    ctx.store.heartbeats["situation"] = [clock("11:50"), clock("12:05")]
    out, _ = step(ctx, clock("11:51"), view, fixes, memory)
    assert not [p for p in out if p["passenger_id"] == "P1"]                 # only the status was wrong: held
    closed = _conflict_version(2, clock("12:05"), status="resolved_by_human")
    closed["conflicts"][0]["settled_by"] = "resolved by a human (network_operations_control) at 12:05"
    recs.append(closed)
    out, _ = step(ctx, clock("12:05"), view, fixes, memory)
    p1 = [p for p in out if p["passenger_id"] == "P1"]
    assert p1 and all(not p["held_fields"] and p["confirmed"]["status"] == "cancelled" for p in p1)
    assert view.cleared[-1]["how"].startswith("resolved by a human (network_operations_control)")


# ------------------------------------------------------------------ A3: the simulated people

def test_a_simulated_person_never_sees_a_blocked_proposal_and_answers_by_role_and_urgency():
    h = SimulatedHumans(random.Random(1), lambda key, sides, f, now: sides[1])
    now = clock("12:00")
    assert h.ask("communication", 1, {"action": "draft_message"}, "customer_service_supervisor", now, False) is None
    assert h.refused_blocked == 1
    q = h.ask("communication", 2, {"action": "resolve_source_conflict", "urgent": True,
                                   "options": [{"conflict_key": "k", "flight_ids": ["F1"], "sides": ["a", "b"]}]},
              "network_operations_control", now, True)
    lo, hi = h.s["response_min"]["network_operations_control"]["urgent"]
    assert timedelta(minutes=lo) <= q.due_at - now <= timedelta(minutes=hi)
    assert h.ask("situation", 3, {"action": "classify_cause"}, "customer_service_supervisor", now, True).status == "not_handled"
    answered = h.due(now + timedelta(minutes=hi))
    assert [x.rec_id for x in answered] == [2] and answered[0].answer["conflicts"][0]["answer_side"] == "b"


@pytest.fixture(scope="module")
def conflict_day():
    """A fresh chaos day with a 'departed while the ramp was closed' conflict (one of last phase's 10 conflict days)."""
    import chaos.compare as cc
    import simulator.run as sr
    from backend.seed.build import build
    keep, orig = {}, sr.simulate

    def spy(*a, **k):
        r = orig(*a, **k)
        keep[k.get("mode", "today")] = r
        return r
    cc.simulate = spy
    try:
        row = cc.compare(88854510, build(), True)
    finally:
        cc.simulate = orig
    return row, keep["today"], keep["copilot"]


def test_on_a_real_conflict_day_a_person_answers_situation_closes_it_and_the_hold_is_short(conflict_day):
    row, today, co = conflict_day
    answers = [e for e in co.world.events if e["type"] == "human_decision_recorded"]
    assert answers and all(e["actor"] == "simulated_human" and e["actor_name"] == "simulated_human:network_operations_control"
                           for e in answers)
    closed = [c for c in co.metrics["conflict_detail"] if c["flight_id"] == answers[0]["payload"]["flight_id"]]
    assert closed and closed[0]["closed_by"] in ("a person", "evidence")
    assert all(h["minutes"] <= 60 and not h["worse_than_today"] for h in row["conflict_holds"])
    assert not [e for e in today.world.events if str(e.get("actor", "")).startswith("simulated_human")]   # copilot only


def test_simulated_people_only_ever_approved_what_the_rule_check_passed(conflict_day):
    _, _, co = conflict_day
    saved = {"communication": co.copilot.context.saved, "passenger_care": co.copilot.care.context.saved}
    approved = [(e["payload"]["agent"], e["payload"]["recommendation"]) for e in co.world.events if e["type"] == "human_approved"
                and not e["payload"].get("void")]
    assert approved and all(saved[a][("recommendations", r)]["rule_check_ok"] for a, r in approved)
