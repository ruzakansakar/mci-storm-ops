"""Phase 2 checks: do the rules in config/rules.yaml behave the way an ops person expects?

None of these touch the database; they test the rules on their own.
"""
import copy
from datetime import datetime, timedelta

import pytest

from backend.rules.autonomy import autonomy_for
from backend.rules.compensation import compensation_for
from backend.rules.loader import RulesError, load_rules, validate
from backend.rules.safety import CHECKS, decide, screen
from backend.settings import TZ

R = load_rules()
NOON = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)


def a31(**over):
    """A31 inbound from Denver, airborne, MCI under a ground stop (numbers from the class slides)."""
    o = {
        "id": "base", "international": False,
        "aircraft": {"maint_due_at": datetime(2026, 10, 9, 3, 45, tzinfo=TZ), "needs_inspection": False,
                     "fuel_minutes_extra": 55},
        "flight_end": NOON + timedelta(hours=2),
        "crew": {"duty_left_min": 260, "rest_hours": 10},
        "planned_duty_min": 90,
        "pax": {"wheelchair": 3, "child_alone": 0},
        "network_score": 5, "pax_disrupted": 158, "cost_usd": 0,
    }
    o.update(over)
    return o


# ------------------------------------------------------------------ the story

def test_a31_fuel_story_only_the_safe_options_survive():
    options = [
        a31(id="hold_45_min", hold_minutes=45, network_score=9, pax_disrupted=42),       # 55-45 = 10 left: kept
        a31(id="hold_50_min", hold_minutes=50, network_score=9, pax_disrupted=42),       # 55-50 = 5 left: removed
        a31(id="divert_STL", hold_minutes=30, minutes_to_alternate=15,                    # 55-30-15 = 10 left: kept
            station={"gates_free": 6, "has_customs": True, "wheelchair_staff": 6}),
        a31(id="divert_ICT", minutes_to_alternate=0, network_score=6,
            station={"gates_free": 2, "has_customs": False, "wheelchair_staff": 2}),
        a31(id="divert_TUL_no_gate", minutes_to_alternate=0,
            station={"gates_free": 0, "has_customs": False, "wheelchair_staff": 3}),
    ]
    result = decide(options, R)
    assert [o["id"] for o in result["ranked"]] == ["hold_45_min", "divert_ICT", "divert_STL"]
    why = {r["option"]: (r["rung"], r["reasons"][0]) for r in result["removed"]}
    assert set(why) == {"hold_50_min", "divert_TUL_no_gate"}
    assert why["hold_50_min"][0] == 2 and "fuel" in why["hold_50_min"][1] and "5 min" in why["hold_50_min"][1]
    assert why["divert_TUL_no_gate"][0] == 3


def test_a_better_network_option_never_beats_safety():
    unsafe = a31(id="fast_but_tired_crew", network_score=100, crew={"duty_left_min": 100, "rest_hours": 10})
    safe = a31(id="slower", network_score=1)
    kept, removed = screen([unsafe, safe], R)
    assert [o["id"] for o in kept] == ["slower"]
    assert removed[0]["rung_name"] == "legal"


def test_safe_options_ranked_network_then_people_then_cost():
    opts = [a31(id="cheap", network_score=5, pax_disrupted=100, cost_usd=10),
            a31(id="best_network", network_score=8, pax_disrupted=150, cost_usd=999),
            a31(id="fewer_people", network_score=5, pax_disrupted=50, cost_usd=500)]
    assert [o["id"] for o in decide(opts, R)["ranked"]] == ["best_network", "fewer_people", "cheap"]


# ------------------------------------------------------------------ individual checks

def test_unknown_counts_as_unsafe():
    assert CHECKS["crew_duty_left"](a31(crew={"duty_left_min": None}), R["limits"])
    assert CHECKS["needs_inspection"](a31(aircraft={"needs_inspection": None}), R["limits"])
    assert CHECKS["tarmac_limit"]({"tarmac_min": None}, R["limits"])


def test_tarmac_limit_is_3h_domestic_4h_international():
    lim = R["limits"]
    assert CHECKS["tarmac_limit"]({"tarmac_min": 180}, lim)
    assert not CHECKS["tarmac_limit"]({"tarmac_min": 200, "international": True}, lim)
    assert CHECKS["tarmac_limit"]({"tarmac_min": 240, "international": True}, lim)


def test_lightning_stops_ramp_work():
    assert CHECKS["ramp_closed_for_lightning"]({"needs_ramp": True, "ramp_open": False}, R["limits"])
    assert not CHECKS["ramp_closed_for_lightning"]({"needs_ramp": False, "ramp_open": False}, R["limits"])


def test_maintenance_due_before_the_plan_ends():
    o = a31(aircraft={"maint_due_at": NOON + timedelta(minutes=30)})
    assert CHECKS["maintenance_due"](o, R["limits"])


def test_child_alone_blocked_on_international_and_needs_a_plan():
    cun = {"international": True, "pax": {"child_alone": 2}}
    assert CHECKS["child_alone_international"](cun, R["limits"])
    assert CHECKS["child_alone_has_plan"]({"pax": {"child_alone": 1}}, R["limits"])
    assert not CHECKS["child_alone_has_plan"]({"pax": {"child_alone": 1}, "child_alone_plan": True}, R["limits"])


def test_class_dataset_crews_with_short_rest_are_caught():
    from backend.seed import class_files as cf
    short = [c for c in cf.read_dataset()["crew"]
             if CHECKS["crew_rest"]({"crew": {"rest_hours": c["Minimum Rest Hrs"]}}, R["limits"])]
    assert len(short) == 35  # same number the Phase 1 loader flagged in data_issues


# ------------------------------------------------------------------ who decides

def test_routine_messages_are_automatic():
    assert autonomy_for("send_status_update", R)["level"] == "auto"
    assert autonomy_for("fix_channel_mismatch", R)["level"] == "auto"


def test_diversion_is_human_only():
    a = autonomy_for("divert_flight", R)
    assert a["level"] == "human_only" and a["approver"] == "dispatcher_and_captain"


def test_unlisted_action_defaults_to_a_human():
    assert autonomy_for("something_new", R)["level"] == "human_only"


def test_auto_action_touching_safety_needs_approval():
    assert autonomy_for("send_status_update", R, safety_rung=1)["level"] == "needs_approval"


def test_large_hotel_block_needs_approval():
    assert autonomy_for("hold_hotel_rooms", R, rooms=20)["level"] == "auto"
    assert autonomy_for("hold_hotel_rooms", R, rooms=80)["level"] == "needs_approval"


# ------------------------------------------------------------------ compensation

def test_weather_gives_no_voucher_but_refund_floor_still_applies():
    c = compensation_for({"cause": "weather", "delay_min": 200, "rebooking_accepted": False}, R)
    kinds = {i["kind"] for i in c["items"]}
    assert c["controllable"] is False and "southwest" not in kinds and "legal" in kinds


def test_controllable_3h_delay_gives_meal_and_75_voucher():
    c = compensation_for({"cause": "crew", "delay_min": 190, "rebooking_accepted": True}, R)
    assert {i["item"] for i in c["items"]} == {"meal voucher", "travel voucher"}
    assert c["total_usd"] == R["compensation"]["meal_usd"] + 75


def test_controllable_overnight_hotel_unless_local():
    away = compensation_for({"cause": "maintenance", "delay_min": 600, "overnight": True, "rebooking_accepted": True}, R)
    local = compensation_for({"cause": "maintenance", "delay_min": 600, "overnight": True, "lives_locally": True,
                              "rebooking_accepted": True}, R)
    assert any("hotel" in i["item"] for i in away["items"])
    assert not any("hotel" in i["item"] for i in local["items"])


def test_international_significant_delay_is_6h():
    five_h = compensation_for({"cause": "weather", "delay_min": 300, "international": True}, R)
    seven_h = compensation_for({"cause": "weather", "delay_min": 420, "international": True}, R)
    assert not any(i["kind"] == "legal" for i in five_h["items"])
    assert any(i["kind"] == "legal" for i in seven_h["items"])


def test_unknown_cause_waits_for_a_human():
    assert compensation_for({"cause": "mystery", "delay_min": 300}, R)["needs_human"]


# ------------------------------------------------------------------ the config file itself

def test_changing_a_number_in_the_file_changes_the_decision():
    stricter = copy.deepcopy(R)
    stricter["limits"]["fuel_buffer_min"] = 60
    o = a31(id="divert_ICT", minutes_to_alternate=0)
    assert screen([o], R)[0] and not screen([o], stricter)[0]


@pytest.mark.parametrize("breakage", [
    lambda r: r.pop("limits"),
    lambda r: r["autonomy"]["cancel_flight"].update(level="maybe"),
    lambda r: r["autonomy"]["cancel_flight"].pop("approver"),
    lambda r: r["safety_ladder"][0]["checks"].append("made_up_check"),
    lambda r: r["safety_ladder"].reverse(),
    lambda r: r["limits"].update(tarmac_domestic_min=-5),
])
def test_broken_rules_file_is_refused(breakage):
    bad = copy.deepcopy(R)
    breakage(bad)
    with pytest.raises(RulesError):
        validate(bad)
