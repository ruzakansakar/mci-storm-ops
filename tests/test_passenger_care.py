"""Phase 4e: the Passenger Care agent. Seed 7 in memory, with co-pilot."""
import pytest

from backend.contracts import load_contracts
from backend.contracts.loader import ContractError, validate_contract
from simulator.message_metrics import forbidden_found


@pytest.fixture(scope="module")
def pair():
    from backend.seed.build import build
    from simulator.run import simulate
    d = build()
    return simulate(7, "a31_storm", data=d), simulate(7, "a31_storm", mode="copilot", data=d)


def test_contract_phase_4e_reads_the_published_data_and_shared_plans_only():
    c = load_contracts()["passenger_care"]
    assert c.phase == "4e" and set(c.shared_reads) == {"situation_assessments", "recovery_plans"}
    assert "bookings" not in c.reads and "flight_controllability" not in str(c.shared_reads)
    assert c.enabled("hold_hotel_rooms") and c.enabled("offer_refund") and not c.enabled("deliver_bags")
    data = dict(load_contracts()["network_recovery"].data)
    data["writes"] = {**data["writes"], "care_plans": {"mode": "insert"}}
    with pytest.raises(ContractError, match="care_plans"):
        validate_contract(data)                                   # only Passenger Care writes care_plans


def test_maya_gets_a_hotel_held_and_assigned_with_approval_and_transport(pair):
    care = pair[1].metrics["care"]
    m = care["maya"]
    assert m["hotel_name"] and m["hotel_status"] == "approved" and m["approved_by"] == "customer_service_supervisor"
    assert m["waited_min"] > 0 and "shuttle" in m["transport"]
    led = pair[1].copilot.care.ledger
    assert any(r["kind"] == "hold" for r in led) and any(r["kind"] == "assign" and r["passenger_id"] == "PAX0001" for r in led)
    assert m["plan_version"] and m["situation_version"] and m["recovery_plan_version"]


def test_maya_compensation_is_the_legal_floor_with_commitments_pending_a03(pair):
    comp = pair[1].metrics["care"]["maya"]["compensation"]
    assert comp == "legal floor offered; meal + $75 voucher + hotel cost pending the supervisor's controllability decision"
    asked = [e for e in pair[1].world.events if e["actor_name"] == "passenger_care" and e["type"] == "human_decision_needed"]
    assert any(e["payload"]["decision_id"] == "N01" for e in asked)
    approved_comp = [v for v in pair[1].copilot.care.verdicts if v["proposal"]["action"] == "issue_compensation"
                     and v["proposal"].get("kind") in ("commitment", "goodwill")]
    assert not approved_comp                                  # nothing owed is proposed as given while A03 is pending


def test_maya_overnight_message_names_the_hotel_and_shuttle_and_no_money(pair):
    text = pair[1].metrics["care"]["maya_overnight_message"]
    m = pair[1].metrics["care"]["maya"]
    assert text and m["hotel_name"] in text and "shuttle" in text
    assert not forbidden_found(text, backed=True)              # no blame or sensitive detail
    for w in ("$", "voucher", "meal", "refund", "compensat", "credit"):
        assert w not in text.lower()


def test_children_alone_and_wheelchair_users_on_affected_flights_have_rule_following_plans(pair):
    care = pair[1].metrics["care"]
    covered = {v["passenger_id"] for v in care["vulnerable"]}
    assert covered and set(care["vulnerable_on_affected_flights"]) <= covered
    assert all(v["follows_the_rules"] for v in care["vulnerable"])
    for v in care["vulnerable"]:
        if v["kind"] == "child_alone":
            assert "staff supervision" in v["plan"] and "guardian" in v["plan"]


def test_rooms_within_availability_costs_tracked_and_no_blocked_action_applied(pair):
    care = pair[1].metrics["care"]
    assert all(h["within_available"] for h in care["hotels_used"])
    assert care["costs"]["committed_usd"] + care["costs"]["pending_usd"] <= care["costs"]["cap_usd"]
    assert care["rule_check_blocks"] == 0
    led = pair[1].copilot.care.ledger
    saved = pair[1].copilot.care.context.saved
    assert all(saved[("recommendations", r["recommendation"])]["rule_check_ok"] for r in led)


def test_child_alone_never_gets_a_hotel_and_rebooking_advice_is_advisory(pair):
    co = pair[1]
    children = {v["passenger_id"] for v in co.metrics["care"]["vulnerable"] if v["kind"] == "child_alone"}
    assert not [r for r in co.copilot.care.ledger if r["passenger_id"] in children]
    advice = [v for v in co.copilot.care.verdicts if v["proposal"]["action"] == "rebook_passenger"]
    assert advice and not [e for e in co.world.events if e["type"] == "care_decision_recorded"
                           and e["payload"]["action"] == "rebook_passenger"]


def test_world_identical_and_earlier_agents_unchanged(pair, monkeypatch):
    from backend.seed.build import build
    from simulator.comparison import truth_fingerprint
    from simulator.run import simulate
    import backend.execution.copilot as cp
    today, co = pair
    assert truth_fingerprint(today) == truth_fingerprint(co)
    monkeypatch.setattr(cp.CareRunner, "tick", lambda self, now, humans, publish: None)
    without = simulate(7, "a31_storm", mode="copilot", data=build())
    assert without.metrics["passenger_minutes_of_disagreement"] == co.metrics["passenger_minutes_of_disagreement"]
    from tests.test_shared_state import _comparable      # event ids differ (care records take ids too); nothing else may
    strip = lambda r, t: [_comparable({k: v for k, v in x.items() if k != "agent_run_id"}) for x in r.copilot.store.records[t]]
    assert strip(without, "situation_assessments") == strip(co, "situation_assessments")
    # Since fix-seats Network Recovery subtracts the seats Care's rebooking advice promises (on purpose: the two must agree on
    # seats), so its seat-dependent parts (cancellations, seats, estimates) may differ without Care. Its delays, crew actions
    # and warnings must not.
    def nr(r):
        out = []
        for x in strip(r, "recovery_plans"):
            y = {k: x[k] for k in ("delays", "crew_actions", "warnings")}
            if not out or out[-1] != y:
                out.append(y)
        return out
    assert nr(without) == nr(co)


def test_hotel_holds_above_the_limit_need_approval():
    from backend.rules import load_rules
    from backend.rules.check import check
    from backend.agents.communication.state_at import clock
    rules = load_rules()
    limit = rules["autonomy"]["hold_hotel_rooms"]["max_rooms"]
    base = {"agent": "passenger_care", "action": "hold_hotel_rooms", "decision_ids": ["M01"], "data_as_of": clock("12:00"),
            "requested_level": "auto"}
    assert check({**base, "rooms": limit}, [], {"now": clock("12:00")}, rules)["result"] == "pass"
    assert check({**base, "rooms": limit + 1}, [], {"now": clock("12:00")}, rules)["result"] == "block"
