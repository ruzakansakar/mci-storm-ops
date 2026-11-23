"""Phase 4d: the Network Recovery agent (advisory). Seed 7 in memory, with co-pilot."""
from datetime import datetime

import pytest

from backend.contracts import load_contracts
from backend.contracts.buffered import BufferedContext
from backend.contracts.context import ContractViolation
from backend.contracts.loader import ContractError, validate_contract


@pytest.fixture(scope="module")
def pair():
    from backend.seed.build import build
    from simulator.run import simulate
    d = build()
    return simulate(7, "a31_storm", data=d), simulate(7, "a31_storm", mode="copilot", data=d)


# ------------------------------------------------------------------ the contract

def test_contract_phase_4d_reads_the_published_plan_only_and_no_names():
    c = load_contracts()["network_recovery"]
    assert c.phase == "4d" and "passengers" not in c.reads and "bookings" not in c.reads
    assert set(c.static_reads) >= {"flights", "aircraft", "crews"}
    for col in ("est_dep", "status", "diverted_to"):          # end-of-day values: rebuilt from events instead
        assert col not in c.reads["flights"]
    assert set(c.reads["passenger_groups"]) == {"flight_id", "segment", "count"}
    assert c.enabled("call_in_reserve_crew") and c.enabled("swap_crews") and not c.enabled("swap_aircraft")
    ctx = BufferedContext(None, feeds=None, agent="network_recovery", rows={})
    from backend.agents.communication.state_at import clock
    ctx.now = clock("12:00")
    out = {"title": "x", "why": "x", "data_as_of": ctx.now.isoformat(), "decision_ids": ["C01"], "flights_affected": [],
           "passengers_affected": 0, "reserve_crews_needed": [], "sources": [{"source": "x", "as_of": ctx.now.isoformat()}],
           "options": [{"id": "a", "summary": "x", "flights": [], "passengers_disrupted": 0, "cost_usd": 0, "network_score": 0}]}
    with pytest.raises(ContractViolation, match="phase|later"):
        ctx.propose("swap_aircraft", out)                      # defined, not built


def test_only_network_recovery_writes_recovery_plans():
    data = dict(load_contracts()["communication"].data)
    data["writes"] = {**data["writes"], "recovery_plans": {"mode": "insert"}}
    with pytest.raises(ContractError, match="recovery_plans"):
        validate_contract(data)


# ------------------------------------------------------------------ seed 7: crews, fuel, plan

def test_wn1033_flight_attendants_warned_well_ahead_with_a_legal_swap(pair):
    n = pair[1].metrics["network"]
    fa = next(c for c in n["crew_timeouts"] if c["flight_id"] == "WN1033" and c["role"] == "fa")
    assert fa["what_happened"].startswith("cancelled") and fa["at"] == "14:55"
    assert fa["lead_min"] >= 60 and fa["best_option"] == "swap"
    assert "WN1033" in n["plan"]["crew_saves"]
    act = [p for p in n["plans"] for a in p["crew_actions"] if a["flight_id"] == "WN1033" and a["role"] == "fa" and a["option"]]
    opt = next(a["option"] for a in act[-1]["crew_actions"] if a["flight_id"] == "WN1033" and a["role"] == "fa")
    assert opt["kind"] == "swap" and opt["legal"] and opt["from_flight"] == "WN1035"


def test_a31_warned_about_fuel_before_12_15_with_safe_alternates(pair):
    a31 = next(f for f in pair[1].metrics["network"]["fuel_diversions"] if f["flight_id"] == "WN2031")
    assert a31["diverted_at"] == "12:15" and a31["first_warning"] < "12:15" and a31["lead_min"] > 0
    assert "STL" in a31["safe_alternates"]


def test_no_illegal_proposals_and_every_crew_option_proposed_is_legal(pair):
    nr = pair[1].copilot.network
    assert pair[1].metrics["network"]["illegal_proposals"] == 0 and nr.verdicts
    from backend.agents.network_recovery.plan import CREW_CHECKS
    from backend.rules import load_rules
    from backend.rules.safety import CHECKS, limits_for
    lim = limits_for(load_rules())
    crew_actions = [v for v in nr.verdicts if v["proposal"]["action"] in ("call_in_reserve_crew", "swap_crews", "deadhead_crew")]
    assert crew_actions
    for v in crew_actions:
        assert v["verdict"]["result"] == "pass" and v["applied"] is False and v["would_need_approval_by"] == "crew_scheduling"


def test_diverting_and_extending_duty_are_only_asked_never_proposed(pair):
    co = pair[1]
    actions = {v["proposal"]["action"] for v in co.copilot.network.verdicts}
    assert not actions & {"divert_flight", "extend_crew_duty"}
    asks = [e for e in co.world.events if e["actor_name"] == "network_recovery" and e["type"] == "human_decision_needed"]
    assert any(e["payload"]["decision_id"] == "B09" for e in asks)


def test_advisory_nothing_applied_world_identical_and_other_agents_unchanged(pair, monkeypatch):
    from backend.seed.build import build
    from simulator.comparison import truth_fingerprint
    from simulator.run import simulate
    import backend.execution.copilot as cp
    today, co = pair
    assert truth_fingerprint(today) == truth_fingerprint(co)
    monkeypatch.setattr(cp.NetworkRunner, "tick", lambda self, now: None)
    without = simulate(7, "a31_storm", mode="copilot", data=build())
    assert without.metrics["passenger_minutes_of_disagreement"] == co.metrics["passenger_minutes_of_disagreement"]
    strip = lambda r: [{k: v for k, v in x.items() if k not in ("agent_run_id",)} for x in r.copilot.store.records["situation_assessments"]]
    assert strip(without) == strip(co)                       # Situation's versions are the same with or without it


def test_recovery_plans_are_versioned_shared_and_estimates_stay_estimates(pair):
    co = pair[1]
    rows = co.copilot.store.records["recovery_plans"]
    assert rows and [r["version"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r["planned_at"] <= co.copilot.network.context.now for r in rows)
    from backend.contracts.shared_state import project
    with_est = [r for r in rows if r["estimates"]]                   # versions written while the stop was on
    view = project(with_est[-1], "recovery_plans", {"fields": ["version", "planned_at", "cancels", "estimates"]})
    assert "estimates" in view["suspected"] and view["suspected"]["estimates"]["suspected"] is True
    assert view["suspected"]["estimates"]["value"]["label"].startswith("estimate")
