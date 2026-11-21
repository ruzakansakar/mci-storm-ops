"""The orchestrator's rule check (backend/rules/check.py) and human overrides (backend/rules/override.py)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.rules import load_rules
from backend.rules.check import check
from backend.rules.override import OverrideRefused, human_override
from tests.conftest import one

R = load_rules()
NOW = datetime(2026, 10, 6, 14, 55, tzinfo=ZoneInfo("America/Chicago"))
WORLD = {"now": NOW, "approved": {"REC-HOTEL-1"}, "approved_actions": [{"action": "cancel_flight", "flight_id": "WN1033"}],
         "flights": {"WN1033": {"status": "cancelled", "seats": 143, "pax_total": 0},
                     "WN1039": {"status": "delayed", "seats": 175, "pax_total": 150},
                     "WN1041": {"status": "scheduled", "seats": 143, "pax_total": 143},
                     "WN3005": {"status": "scheduled", "seats": 175, "pax_total": 20}}}


def proposal(**kw):
    base = {"id": "P1", "agent": "network_recovery", "action": "delay_flight", "decision_ids": ["B02"], "data_as_of": NOW,
            "flight_id": "WN1039", "safety_rung": 4,
            "option": {"crew": {"duty_left_min": 400, "rest_hours": 12, "availability": "on_duty"}, "planned_duty_min": 200}}
    base.update(kw)
    return base


def blocked(result, rule_id):
    assert result["result"] == "block", result
    assert rule_id in result["rule_ids"], (rule_id, result["rule_ids"])
    assert result["reasons"] and all(isinstance(r, str) and r for r in result["reasons"])
    assert result["needs_human"]
    return result


# ------------------------------------------------------------------ a clean proposal passes

def test_a_clean_proposal_passes_and_still_names_who_approves_it():
    r = check(proposal(), [], WORLD)
    assert r == {"result": "pass", "reasons": [], "rule_ids": [], "blocks": [], "needs_human": "network_operations_control"}
    auto = check(proposal(action="assess_situation", agent="situation", decision_ids=["A01"], safety_rung=None, option=None), [], WORLD)
    assert auto["result"] == "pass" and auto["needs_human"] is None


# ------------------------------------------------------------------ each kind of rule break

@pytest.mark.parametrize("option,rule_id", [
    ({"crew": {"fdp_start": "07:00", "segments": 1, "planned_fdp_h": 16.5}}, "pilot_fdp_table_b"),
    ({"crew": {"rest_hours": 9}}, "pilot_min_rest_hours"),
    ({"crew": {"availability": "sick"}}, "crew_availability"),
    ({"crew": {"fatigue_reported": True}}, "pilot_fatigue_report"),
    ({"crew": {"fdp_hours_168h": 55, "planned_fdp_h": 9}}, "pilot_fdp_168h_max"),
    ({"crew": {"duty_left_min": 298}, "planned_duty_min": 270}, "crew_duty_buffer_min"),
    ({"fa": {"duty_h": 15}}, "fa_duty_max_h"),
    ({"fa": {"previous_duty_h": 15, "rest_h": 10}}, "fa_rest_after_over_14h"),
    ({"staff": {"shift_min": 800}}, "ground_staff_max_shift_min"),
    ({"tarmac_min": 185}, "tarmac_domestic_min"),
    ({"tarmac_min": 130}, "tarmac_food_water_min"),
    ({"relies_on_tarmac_exception": True}, "tarmac_exception_captain_safety"),
    ({"airport": "SAN", "operation": "departure", "local_time": "23:45"}, "airport_curfews"),
    ({"international": True, "station": {"has_customs": False}}, "customs_international_arrivals"),
    ({"airport": "ICT", "deplaned_outside_secure": True, "local_time": "22:00"}, "tsa_checkpoint_hours"),
    ({"airport": "DSM", "needs_station_staff": True, "local_time": "23:00"}, "southwest_station_hours"),
    ({"lightning_distance_mi": 3, "ramp_activity": "fueling"}, "lightning_ramp_radius_miles"),
    ({"aircraft": {"struck_by_lightning": True}}, "lightning_strike_inspection"),
    ({"aircraft": {"needs_inspection": True}}, "rung_2"),
    ({"aircraft": {"fuel_minutes_extra": 20}, "hold_minutes": 15}, "fuel_buffer_min"),
    ({"pax": {"wheelchair": 1, "wheelchair_unattended_min": 45}}, "wheelchair_unattended_max_min"),
    ({"pax": {"child_alone": 1}, "plane_changes": 1, "child_alone_plan": True}, "child_alone_nonstop_or_same_plane"),
    ({"pax": {"child_alone": 1}, "international": True, "child_alone_plan": True}, "child_alone_international_allowed"),
    ({"pax": {"child_alone": 1}, "destination_kind": "hotel", "child_alone_plan": True}, "rung_5"),
])
def test_each_kind_of_rule_break_is_blocked_with_a_reason_and_rule_id(option, rule_id):
    r = check(proposal(option=option), [], WORLD)
    if rule_id.startswith("rung_"):
        assert r["result"] == "block" and r["reasons"][0].startswith(f"rung {rule_id[-1]}")
    else:
        blocked(r, rule_id)


def test_money_rules_offer_more_than_the_rules_give_is_blocked():
    weather = {"cause": "weather", "delay_min": 240, "rebooking_accepted": True}
    good = proposal(agent="passenger_care", action="issue_compensation", decision_ids=["N05"], safety_rung=8, option=None,
                    owed_inputs=weather, offer={"kind": "compensation", "items": [{"item": "optional travel credit", "usd": 50}]})
    assert check(good, [], WORLD)["result"] == "pass"
    too_much = dict(good, offer={"kind": "compensation", "items": [{"item": "optional travel credit", "usd": 200}]})
    blocked(check(too_much, [], WORLD), "goodwill_usd")
    not_owed = dict(good, offer={"kind": "compensation", "items": [{"item": "travel voucher", "usd": 75}]})
    blocked(check(not_owed, [], WORLD), "voucher_usd")


def test_actions_are_checked_against_the_autonomy_rules():
    blocked(check(proposal(action="divert_flight", decision_ids=["B08"]), [], WORLD), "action_divert_flight")
    blocked(check(proposal(action="keep_on_board_exception", decision_ids=["F07"]), [], WORLD), "action_keep_on_board_exception")
    r = blocked(check(proposal(requested_level="auto"), [], WORLD), "never_auto_rungs")          # needs approval, never auto
    assert "automatically" in r["reasons"][0]
    rung1 = proposal(action="send_status_update", agent="communication", decision_ids=["F01"], safety_rung=1, option=None,
                     requested_level="auto")
    blocked(check(rung1, [], WORLD), "never_auto_rungs")                                          # an auto action on rung 1
    blocked(check(proposal(action="do_something_new"), [], WORLD), "unlisted_action_is_human_only")


# ------------------------------------------------------------------ missing and stale data

def test_missing_or_stale_data_is_a_block_never_a_pass():
    for key in ("action", "decision_ids", "data_as_of", "agent"):
        p = proposal()
        del p[key]
        blocked(check(p, [], WORLD), "missing_or_stale_data_blocks" if key != "action" else "missing_or_stale_data_blocks")
    blocked(check(proposal(data_as_of=NOW - timedelta(minutes=10)), [], WORLD), "missing_or_stale_data_blocks")   # 600 s > 120 s
    assert check(proposal(data_as_of=NOW - timedelta(seconds=100)), [], WORLD)["result"] == "pass"
    blocked(check(proposal(data_as_of="this morning"), [], WORLD), "missing_or_stale_data_blocks")
    blocked(check(proposal(), [], {**WORLD, "now": None}), "missing_or_stale_data_blocks")
    unknown = proposal(option={"crew": {"duty_left_min": None}})
    assert check(unknown, [], WORLD)["result"] == "block"                                          # unknown counts as unsafe


# ------------------------------------------------------------------ conflicts with other pending proposals

def test_delay_and_cancel_on_the_same_flight_conflict():
    cancel = proposal(id="P2", action="cancel_flight", decision_ids=["B03"])
    r = blocked(check(proposal(), [cancel], WORLD), "proposal_conflicts_block")
    assert "can't both happen" in r["reasons"][0] and r["needs_human"] == "network_operations_control"
    assert check(proposal(), [dict(cancel, flight_id="WN1041")], WORLD)["result"] == "pass"


def test_rebooking_onto_a_cancelled_or_full_flight_or_one_another_proposal_cancels_is_blocked():
    rebook = dict(agent="passenger_care", action="rebook_passenger", decision_ids=["K01"], option=None, safety_rung=4, flight_id=None)
    blocked(check(proposal(**rebook, rebook_to="WN1033", passengers=1), [], WORLD), "proposal_conflicts_block")          # cancelled
    blocked(check(proposal(**rebook, rebook_to="WN1041", passengers=1), [], WORLD), "proposal_conflicts_block")          # full
    blocked(check(proposal(**rebook, rebook_to="WN1039", passengers=30), [], WORLD), "proposal_conflicts_block")         # not enough room
    other = proposal(id="P9", action="cancel_flight", flight_id="WN3005")
    blocked(check(proposal(**rebook, rebook_to="WN3005", passengers=1), [other], WORLD), "proposal_conflicts_block")
    assert check(proposal(**rebook, rebook_to="WN3005", passengers=1), [], WORLD)["result"] == "pass"
    blocked(check(proposal(**rebook, rebook_to="WN9999"), [], WORLD), "missing_or_stale_data_blocks")


def test_an_offer_or_promise_with_no_approved_decision_behind_it_is_blocked():
    msg = dict(agent="communication", action="send_status_update", decision_ids=["O14"], option=None, safety_rung=8)
    blocked(check(proposal(**msg, message={"promises": [{"what": "a hotel room", "backed_by": None}]}), [], WORLD),
            "offer_needs_approved_decision")
    assert check(proposal(**msg, message={"promises": [{"what": "a hotel room", "backed_by": "REC-HOTEL-1"}]}), [], WORLD)["result"] == "pass"
    blocked(check(proposal(**msg, offer={"kind": "hotel", "backed_by": "REC-UNKNOWN"}), [], WORLD), "offer_needs_approved_decision")


def test_a_message_that_contradicts_the_truth_or_an_approved_decision_is_blocked():
    msg = dict(agent="communication", action="send_status_update", decision_ids=["O14"], option=None, safety_rung=8)
    blocked(check(proposal(**msg, message={"claims": {"WN1039": "on_time"}}), [], WORLD), "message_matches_truth")
    r = blocked(check(proposal(**msg, message={"claims": {"WN1033": "delayed"}}), [], WORLD), "message_matches_truth")
    assert any("approved" in x for x in r["reasons"])
    assert check(proposal(**msg, message={"claims": {"WN1033": "cancelled"}}), [], WORLD)["result"] == "pass"


def test_the_same_crew_or_aircraft_assigned_twice_is_blocked():
    a = proposal(id="P1", action="call_in_reserve_crew", decision_ids=["D03"], flight_id="WN1039", assign={"crew_ids": ["R04"]})
    b = proposal(id="P2", action="call_in_reserve_crew", decision_ids=["D03"], flight_id="WN1041", assign={"crew_ids": ["R04"]})
    r = blocked(check(a, [b], WORLD), "no_double_assignment")
    assert r["needs_human"] == "crew_scheduling"
    c = proposal(id="P3", action="swap_aircraft", decision_ids=["C03"], flight_id="WN1039", assign={"aircraft_id": "A12"})
    d = proposal(id="P4", action="swap_aircraft", decision_ids=["C03"], flight_id="WN1041", assign={"aircraft_id": "A12"})
    blocked(check(c, [d], WORLD), "no_double_assignment")


# ------------------------------------------------------------------ examples from the seed 7 story

def test_story_examples_from_seed_7():
    # 14:55: WN1033's flight attendants have 298 min of duty left; a further delay needs 300 (flight + one leg + 30 min buffer)
    delay = proposal(flight_id="WN1033", option={"crew": {"duty_left_min": 298, "availability": "on_duty"}, "planned_duty_min": 270})
    r = blocked(check(delay, [], WORLD), "crew_duty_buffer_min")
    assert "28 min of duty left" in r["reasons"][0]
    # after WN1033 is cancelled, a proposal to rebook Maya (PAX0001) back onto it
    maya = proposal(agent="passenger_care", action="rebook_passenger", decision_ids=["K01"], option=None, flight_id=None,
                    rebook_to="WN1033", passengers=1)
    blocked(check(maya, [], WORLD), "proposal_conflicts_block")


# ------------------------------------------------------------------ overrides

def tarmac_block():
    return check(proposal(action="start_deplaning", agent="station_staff", decision_ids=["F05"], safety_rung=1,
                          option={"relies_on_tarmac_exception": True}), [], WORLD)


def test_the_orchestrator_and_the_agents_can_never_override(db):
    r = tarmac_block()
    for role in ("orchestrator", "network_recovery", "situation", "communication", "system"):
        with pytest.raises(OverrideRefused, match="named human"):
            human_override(r, "tarmac_exception_captain_safety", role, role, "it wants to", cur=db, now=NOW)
    from backend.contracts.context import AgentContext
    assert not hasattr(AgentContext, "override") and not hasattr(AgentContext, "human_override")


def test_a_human_override_is_accepted_only_for_an_exception_the_law_allows_and_it_is_logged(db):
    r = tarmac_block()
    with pytest.raises(OverrideRefused, match="no exception"):
        human_override(r, "tarmac_exception_captain_safety", "ramp_supervisor", "Pat Lee", "lightning", cur=db, now=NOW)
    with pytest.raises(OverrideRefused, match="reason"):
        human_override(r, "tarmac_exception_captain_safety", "captain", "Capt. Ana Ruiz", " ", cur=db, now=NOW)
    before = one(db, "SELECT count(*) FROM audit_log WHERE what='override of a rule-check block'")
    ok = human_override(r, "tarmac_exception_captain_safety", "captain", "Capt. Ana Ruiz",
                        "lightning on the ramp: deplaning by stairs is not safe", cur=db, now=NOW, proposal_id="P1")
    assert ok["result"] == "pass" and ok["overridden"]["role"] == "captain"
    assert one(db, "SELECT count(*) FROM audit_log WHERE what='override of a rule-check block'") == before + 1
    row = db.execute("SELECT actor, actor_name, entity_id, after FROM audit_log WHERE what='override of a rule-check block' "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "human" and row[1] == "Capt. Ana Ruiz" and row[2] == "tarmac_exception_captain_safety"
    assert row[3]["role"] == "captain" and "lightning" in row[3]["reason"]


def test_no_override_where_the_law_has_no_exception(db):
    rest = check(proposal(option={"crew": {"rest_hours": 9}}), [], WORLD)
    for role in ("captain", "crew_scheduling", "station_manager"):
        with pytest.raises(OverrideRefused):
            human_override(rest, "pilot_min_rest_hours", role, "A Person", "we need the flight", cur=db, now=NOW)
    conflict = check(proposal(), [proposal(id="P2", action="cancel_flight")], WORLD)
    with pytest.raises(OverrideRefused):
        human_override(conflict, "proposal_conflicts_block", "station_manager", "A Person", "pick one", cur=db, now=NOW)
    # an override only lifts the block it is about; other blocks stay
    both = check(proposal(action="start_deplaning", agent="station_staff", decision_ids=["F05"], safety_rung=1,
                          option={"relies_on_tarmac_exception": True, "pax": {"wheelchair": 1, "wheelchair_unattended_min": 45}}), [], WORLD)
    after = human_override(both, "tarmac_exception_captain_safety", "captain", "Capt. Ana Ruiz", "safety", cur=db, now=NOW)
    assert after["result"] == "block" and "wheelchair_unattended_max_min" in after["rule_ids"]


def test_only_law_rules_with_an_exception_can_be_overridden():
    overridable = {e["id"]: e["override_by"] for e in R["entries"] if e.get("override_by")}
    assert overridable == {"tarmac_food_water_min": ["captain"], "tarmac_exception_captain_safety": ["captain"],
                           "tarmac_exception_atc": ["captain"]}
    assert all(next(e for e in R["entries"] if e["id"] == i)["file"] == "law.yaml" for i in overridable)
