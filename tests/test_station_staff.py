"""Phase 4f: the Station & Staff agent. Seed 7 in memory, today and with co-pilot."""
from datetime import datetime, timedelta

import pytest

from backend.contracts import load_contracts
from backend.contracts.loader import TRUTH_TABLES, ContractError, validate_contract
from backend.rules import load_rules
from backend.rules.safety import CHECKS, limits_for

LIM = limits_for(load_rules())
BUILT = {"assign_gates", "move_gate_agents", "call_in_ground_staff", "request_wheelchair_help", "supervise_child_alone",
         "arrange_food_water", "start_deplaning", "arrange_diversion_deplaning", "request_airport_resources", "flag_ramp_service_impact"}
HUMAN_ONLY = {"ramp_closure", "reopen_ramp", "stop_work_for_staff_safety", "return_to_gate", "keep_on_board_exception"}


@pytest.fixture(scope="module")
def pair():
    from backend.seed.build import build
    from simulator.run import simulate
    d = build()
    return simulate(7, "a31_storm", data=d), simulate(7, "a31_storm", mode="copilot", data=d), d


def test_contract_phase_4f_reads_published_data_and_help_requests_only():
    c = load_contracts()["station_staff"]
    assert c.phase == "4f" and {a for a in c.actions if c.enabled(a)} == BUILT
    assert "passengers" not in c.reads and not HUMAN_ONLY & set(c.actions)            # no names; human-only is never proposed
    assert set(c.static_reads) == {"flights", "staff", "stations", "passenger_groups"}
    care = c.shared_reads["care_plans"]
    assert set(care["fields"]) == {"version", "planned_at", "wheelchair_requests", "vulnerable"}   # no hotels, money or segments
    assert {"gate_assignments", "staff_assignments"} <= TRUTH_TABLES                   # co-pilot tables: no agent writes them
    data = dict(load_contracts()["network_recovery"].data)
    data["writes"] = {**data["writes"], "station_plans": {"mode": "insert"}}
    with pytest.raises(ContractError, match="station_plans"):
        validate_contract(data)                                                         # only Station & Staff writes station_plans
    nr = load_contracts()["network_recovery"].shared_reads["station_plans"]
    assert set(nr["fields"]) == {"version", "planned_at", "ramp_status", "gate_shortages", "staff_shortages"}


def test_every_tarmac_wait_has_its_deadlines_met_or_flagged_and_only_the_captain_is_asked(pair):
    co = pair[1]
    m = co.metrics["station"]
    waited = {e["payload"]["flight_id"] for e in co.world.events if e["type"] == "tarmac_started"}
    rows = {t["flight_id"]: t for t in m["tarmac"]}
    assert waited and waited == set(rows)
    assert all(t["ok"] for t in rows.values())
    lead = timedelta(minutes=co.copilot.station.s["tarmac"]["ask_captain_before_return_min"])
    asked = {(q["decision_id"], q["flight_id"]) for q in m["asked"]}
    for t in rows.values():
        start = next(e["t"] for e in co.world.events if e["type"] == "tarmac_started" and e["payload"]["flight_id"] == t["flight_id"])
        due = datetime.fromisoformat(t["start_return_by_due"])
        from simulator.clock import at
        if at(start) + timedelta(minutes=t["minutes"]) >= due - lead:                  # came near the time to start returning
            assert ("F06", t["flight_id"]) in asked and ("F07", t["flight_id"]) in asked
    assert {"WN1027", "WN1028"} <= {f for d, f in asked if d == "F07"}                  # the two planes that waited 150 min
    proposed = {v["proposal"]["action"] for v in co.copilot.station.verdicts}
    assert not proposed & HUMAN_ONLY and proposed <= BUILT


def test_a31_gets_a_gate_for_its_1410_arrival_and_every_arrival_has_one(pair):
    m = pair[1].metrics["station"]
    a = m["a31"]
    assert a["flight_id"] == "WN2031" and a["landed"].endswith("14:10:00-05:00")
    assert a["gate"] and a["approved_by"] == "station_manager" and a["from"] <= a["landed"] <= a["to"]
    assert m["arrivals_with_gate_at_landing"] == m["arrivals"]


def test_wheelchair_passengers_covered_and_the_child_supervised_with_care_closed(pair):
    co = pair[1]
    m = co.metrics["station"]
    reqs = {r["passenger_id"] for p in co.copilot.care.memory.plans for r in p["wheelchair_requests"]}
    covered = {w["passenger_id"] for w in m["wheelchair"]}
    assert reqs and reqs == covered and len(reqs) == 2
    assert all(w["staff"] and w["longest_unattended_min"] <= LIM["wheelchair"]["unattended_max_min"] for w in m["wheelchair"])
    assert all(w["longest_unattended_min"] == 0 for w in m["wheelchair"])
    kids = m["children"]
    assert len(kids) == 1 and kids[0]["supervisor"] and kids[0]["approved_by"] == "station_manager"
    assert kids[0]["care_request_status"] == "closed_by_station_staff"
    assert not [k for k in co.metrics["care"]["pending_at_day_end"] if "child" in k]   # Passenger Care's pending item is closed
    names = co.copilot.station.context.name_reads
    assert names == [] and ("passengers", "full_name") not in co.copilot.station.context.read_columns


def test_no_staff_member_breaks_a_shift_or_break_limit(pair):
    co, d = pair[1], pair[2]
    m = co.metrics["station"]
    g = LIM["ground_staff"]
    assert not m["overlaps"] and not m["outside_shift"] and not m["over_planned_break"]
    assert m["longest_without_break_min"] <= g["break_interval_min"] and m["longest_shift_min"] <= g["max_shift_min"]
    for v in co.copilot.station.verdicts:
        staff = (v["option"] or {}).get("staff")
        if staff:
            assert CHECKS["ground_staff_limits"]({"staff": staff}, LIM) is None, v["proposal"]["title"]
            assert set(staff) == {"shift_min", "min_since_break", "planned_break_min", "gap_min", "consecutive_days"}
    assert all(r["approved_by"] == "station_manager" for r in co.copilot.station.staff_ledger if r["kind"] == "call_in")


def test_no_ramp_work_while_the_ramp_is_closed_for_lightning(pair):
    from simulator.station_metrics import effective
    co = pair[1]
    m = co.metrics["station"]
    assert m["ramp_closures"] == [["2026-10-06T12:10:00-05:00", "2026-10-06T12:45:00-05:00"]]
    assert m["ramp_work_in_closure"] == []
    a, b = (datetime.fromisoformat(x) for x in m["ramp_closures"][0])
    ramp = [w for w in effective(co.copilot.station.staff_ledger) if w["role"] == "ramp"]
    assert ramp and not [w for w in ramp if w["from"] < b and a < w["to"]]
    paused = [r for r in co.copilot.station.staff_ledger if r["kind"] == "pause"]
    assert paused and all(r["approved_by"] == "automatic" for r in paused)


def test_network_recovery_takes_the_station_into_account_and_earlier_agents_are_unchanged(pair, monkeypatch):
    from backend.seed.build import build
    from simulator.comparison import truth_fingerprint
    from simulator.run import simulate
    import backend.execution.copilot as cp
    today, co, _ = pair
    assert truth_fingerprint(today) == truth_fingerprint(co)
    monkeypatch.setattr(cp.StationRunner, "tick", lambda self, now, humans: None)
    without = simulate(7, "a31_storm", mode="copilot", data=build())
    nr_with, nr_without = co.copilot.network.memory.plans, without.copilot.network.memory.plans
    # Network Recovery reads the station's plan and records the version it used (moving a departure out of a slot the station
    # can't work is tested in test_station_fit_...; since 5b's calibrated staff numbers, no slot is that short on seed 7)
    assert any(p.get("station_plan_version") for p in nr_with) and not any(p.get("station_plan_version") for p in nr_without)
    assert not [d for p in nr_without for d in p["delays"] if d.get("station_check")]
    warn = lambda plans: {(w["kind"], w["flight_id"], w.get("role")) for p in plans for w in p["warnings"]}
    assert warn(nr_with) == warn(nr_without)                                           # its crew warnings still appear
    swap = lambda plans: {(c["flight_id"], c["role"], (c.get("option") or {}).get("crew_id"))
                          for p in plans for c in p["crew_actions"] if c["flight_id"] == "WN1033"}
    assert ("WN1033", "fa", "C35") in swap(nr_with) and swap(nr_with) == swap(nr_without)
    from tests.test_shared_state import _comparable
    strip = lambda r: [_comparable({k: v for k, v in x.items() if k != "agent_run_id"}) for x in r.copilot.store.records["situation_assessments"]]
    assert strip(without) == strip(co)                                                  # Situation unchanged
    msgs = lambda r: [{k: v for k, v in m.items() if k not in ("rec", "backed_by")} for m in r.copilot.messages.log]
    assert msgs(without) == msgs(co)                                                    # Communication: same messages, Maya's too
    room = lambda r: [{k: v for k, v in x.items() if k != "recommendation"} for x in r.copilot.care.ledger]
    assert room(without) == room(co)                                                    # Passenger Care: same rooms
    assert without.metrics["care"]["maya"]["hotel_name"] == co.metrics["care"]["maya"]["hotel_name"]
    assert without.metrics["passenger_minutes_of_disagreement"] == co.metrics["passenger_minutes_of_disagreement"] == 6180


def test_station_fit_moves_a_departure_out_of_a_slot_with_no_crew_or_a_closed_ramp():
    from backend.agents.network_recovery.plan import Planner
    p = Planner.__new__(Planner)
    t = datetime.fromisoformat("2026-10-06T14:05:00-05:00")
    p.station = {"version": 9, "facts": {"ramp_status": {"open": True}, "staff_shortages": [
        {"role": "ramp", "from": "2026-10-06T14:00:00-05:00", "to": "2026-10-06T14:15:00-05:00", "short_by": 9, "departures_workable": 0},
        {"role": "ramp", "from": "2026-10-06T14:15:00-05:00", "to": "2026-10-06T14:30:00-05:00", "short_by": 2, "departures_workable": 1}]}}
    used = {}
    new, why = p.station_fit(t, used)
    assert new == datetime.fromisoformat("2026-10-06T14:15:00-05:00") and "no ramp crew" in why
    second, _ = p.station_fit(t, used)                                                  # the 14:15 slot can only work one
    assert second == datetime.fromisoformat("2026-10-06T14:30:00-05:00")
    p.station = {"version": 3, "facts": {"ramp_status": {"open": False, "expected_reopen": "2026-10-06T12:45:00-05:00"}}}
    new, why = p.station_fit(datetime.fromisoformat("2026-10-06T12:20:00-05:00"), {})
    assert new == datetime.fromisoformat("2026-10-06T12:45:00-05:00") and "ramp closed" in why
    p.station = None
    assert p.station_fit(t, {}) == (t, None)                                            # no station plan: unchanged


def test_the_rule_check_blocks_staff_over_the_limits_and_ramp_work_while_closed():
    from backend.rules.check import check
    now = datetime.fromisoformat("2026-10-06T12:00:00-05:00")
    g = LIM["ground_staff"]
    ok = {"shift_min": g["max_shift_min"], "min_since_break": g["break_interval_min"], "planned_break_min": g["break_length_min"],
          "gap_min": g["min_gap_between_shifts_min"], "consecutive_days": g["max_consecutive_days"]}
    base = {"agent": "station_staff", "action": "move_gate_agents", "decision_ids": ["E04"], "data_as_of": now}
    assert check({**base, "option": {"staff": ok}}, [], {"now": now})["result"] == "pass"
    for k, bad in (("shift_min", 1), ("min_since_break", 1), ("gap_min", -1), ("consecutive_days", 1), ("planned_break_min", -1)):
        assert check({**base, "option": {"staff": {**ok, k: ok[k] + bad}}}, [], {"now": now})["result"] == "block", k
    assert check({**base, "option": {"needs_ramp": True, "ramp_open": False}}, [], {"now": now})["result"] == "block"


def test_simulated_station_people_answer_only_passed_proposals_and_never_human_only(pair):
    co = pair[1].copilot
    h = co.people                     # the simulated people behind the orchestrator's queue (5a)
    station_q = [q for q in h.questions if q.agent == "station_staff"]
    assert station_q and all(q.action in BUILT for q in station_q)
    assert not [q for q in h.questions if q.action in HUMAN_ONLY]
    answered = [q for q in station_q if q.status == "answered"]
    assert answered and all(q.role in ("station_manager", "director_of_operations") for q in answered)   # 5b: escalated items
    assert h.refused_blocked == 0 and pair[1].metrics["station"]["rule_check_blocks"] == 0
    human_only_asks = {q["decision_id"] for q in pair[1].metrics["station"]["asked"]}
    assert {"E01", "E02", "F06", "F07"} <= human_only_asks                              # asked, with the facts; nobody answers
