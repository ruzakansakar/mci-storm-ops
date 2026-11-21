"""The rules catalog (config/rules/) and the checks for the rules the storm story touches."""
import copy

import pytest

from backend.rules import load_rules
from backend.rules.crew import fdp_limit_hours, staff_view
from backend.rules.loader import RulesError, check_entries, read_entries
from backend.rules.registry import CHECKERS
from backend.rules.safety import CHECKS, limits_for, screen
from backend.rules.tarmac import tarmac_timeline

R = load_rules()
L = limits_for(R)


# ------------------------------------------------------------------ the catalog itself

def test_every_rule_has_all_its_fields_and_every_enforced_rule_has_a_real_function():
    entries = read_entries()
    check_entries(entries)                                   # raises if anything is missing
    assert len({e["id"] for e in entries}) == len(entries)
    for e in entries:
        if e["enforced"]:
            assert callable(CHECKERS[e["checked_by"]]), e["id"]
        else:
            assert not e.get("checked_by"), f"{e['id']} names a function but says it is not enforced"


def _good():
    return {"id": "x", "path": "limits.x", "description": "d", "value": 1, "source": "law", "citation": "14 CFR 1", "enforced": True,
            "checked_by": "tarmac_limit"}


@pytest.mark.parametrize("change,message", [
    ({"source": None}, "source must be one of"),
    ({"source": "rumour"}, "source must be one of"),
    ({"citation": ""}, "missing 'citation'"),
    ({"checked_by": "no_such_function"}, "no function named"),
    ({"checked_by": None}, "no function named"),
    ({"description": " "}, "missing 'description'"),
])
def test_a_rule_missing_a_field_or_a_function_is_refused(change, message):
    bad = {**_good(), **change}
    with pytest.raises(RulesError, match=message):
        check_entries([bad])
    for field in ("id", "path", "value", "enforced"):
        broken = _good()
        del broken[field]
        with pytest.raises(RulesError, match=f"missing '{field}'"):
            check_entries([broken])
    with pytest.raises(RulesError, match="appears twice"):
        check_entries([_good(), _good()])


def test_the_old_single_rules_file_is_gone_and_the_five_new_files_exist():
    from backend.settings import ROOT
    assert not (ROOT / "config" / "rules.yaml").exists()
    for name in ("law", "southwest", "operations", "safety_ladder", "autonomy"):
        assert (ROOT / "config" / "rules" / f"{name}.yaml").exists()


def test_the_catalog_covers_everything_that_was_asked_for():
    ids = {e["id"] for e in R["entries"]}
    for must in ("pilot_fdp_table_b", "pilot_fdp_extension_before_takeoff_max_h", "pilot_min_rest_hours", "pilot_weekly_free_hours",
                 "pilot_fdp_168h_max", "pilot_fdp_672h_max", "pilot_flight_time_672h_max", "pilot_flight_time_365d_max",
                 "pilot_fatigue_report", "fa_duty_max_h", "fa_rest_after_14h_or_less", "fa_rest_after_over_14h", "fa_weekly_off_h",
                 "fa_beyond_control_exception", "dispatcher_max_consecutive_duty_h", "ground_staff_max_shift_min",
                 "ground_staff_break_interval_min", "ground_staff_break_length_min", "ground_staff_min_gap_min",
                 "ground_staff_max_consecutive_days", "tarmac_domestic_min", "tarmac_status_announcement_min",
                 "tarmac_food_water_min", "tarmac_exception_captain_safety", "tarmac_exception_atc", "tarmac_dot_report_days",
                 "tarmac_start_return_before_limit_min", "fuel_legal_reserve", "lightning_strike_inspection",
                 "lightning_ramp_radius_miles", "edct_compliance_window_min", "airport_curfews", "customs_international_arrivals",
                 "tsa_rescreen_outside_secure_area", "tsa_checkpoint_hours", "southwest_station_hours", "wheelchair_unattended_max_min",
                 "wheelchair_rule_2024_status", "child_alone_nonstop_or_same_plane", "child_alone_stranded_supervised",
                 "bag_fee_usd", "southwest_rebooks_on_other_airlines"):
        assert must in ids, must
    assert R["tarmac"]["start_return_before_limit_min"] == 30
    assert R["compensation"]["bag_fee_usd"] == 45               # $35 was the May 2025 fee; $45 since 9 Apr 2026


# ------------------------------------------------------------------ pilots

def crew(**kw):
    return {"crew": kw}


def test_table_b_matches_the_regulation():
    t = R["pilot"]["fdp_table_b"]
    assert fdp_limit_hours(t, "07:00", 1) == 14 and fdp_limit_hours(t, "11:59", 2) == 14
    assert fdp_limit_hours(t, "05:30", 5) == 11.5 and fdp_limit_hours(t, "06:15", 3) == 12
    assert fdp_limit_hours(t, "23:30", 4) == 9 and fdp_limit_hours(t, "00:00", 1) == 9
    assert fdp_limit_hours(t, "04:30", 6) == 9 and fdp_limit_hours(t, "17:00", 7) == 9 and fdp_limit_hours(t, "17:00", 9) == 9
    assert fdp_limit_hours(t, "13:00", 7) == 10.5
    assert fdp_limit_hours(t, "07:00", 1, acclimated=False) == 13.5


def test_a_duty_period_inside_table_b_passes_and_over_it_fails():
    f = CHECKS["pilot_fdp_limit"]
    assert not f(crew(fdp_start="07:00", segments=3, planned_fdp_h=13), L)
    assert f(crew(fdp_start="07:00", segments=3, planned_fdp_h=13.5), L)
    assert f(crew(fdp_start="07:00", segments=3, planned_fdp_h=None), L)         # unknown is unsafe


def test_extension_before_takeoff_is_up_to_2_hours_and_only_the_captain_and_airline_can_make_it():
    f = CHECKS["pilot_fdp_limit"]
    base = dict(fdp_start="07:00", segments=1, planned_fdp_h=15.5)               # 1.5 h over the 14 h limit
    assert "human only" in f(crew(**base), L)                                    # not approved yet
    assert not f(crew(**base, extension_approved=True), L)
    assert f(crew(**{**base, "planned_fdp_h": 16.5}, extension_approved=True), L)            # 2.5 h is more than 2 h
    assert not f(crew(**{**base, "planned_fdp_h": 16}, extension_approved=True), L)         # exactly 2 h


def test_an_extension_of_more_than_30_minutes_can_only_be_used_once_before_rest():
    f = CHECKS["pilot_fdp_limit"]
    base = dict(fdp_start="07:00", segments=1, planned_fdp_h=14.75, extension_approved=True)   # 45 min over
    assert not f(crew(**base), L)
    assert "only once" in f(crew(**base, extension_used_since_rest=True), L)
    small = dict(base, planned_fdp_h=14.25)                                      # 15 min over: not limited to once
    assert not f(crew(**small, extension_used_since_rest=True), L)


def test_an_extension_may_not_break_the_cumulative_duty_limit():
    f = CHECKS["pilot_fdp_limit"]
    c = crew(fdp_start="07:00", segments=1, planned_fdp_h=15, extension_approved=True, fdp_hours_168h=46)
    assert "60 hours" in f(c, L)
    assert not f(crew(fdp_start="07:00", segments=1, planned_fdp_h=15, extension_approved=True, fdp_hours_168h=40), L)


def test_after_takeoff_the_duty_period_stretches_only_as_needed_to_land_safely():
    f = CHECKS["pilot_fdp_limit"]
    base = dict(fdp_start="07:00", segments=1, planned_fdp_h=15, airborne=True)
    assert "land safely" in f(crew(**base), L)
    assert not f(crew(**base, needed_to_land_safely=True), L)


def test_cumulative_limits_60_190_100_1000_and_30_hours_free():
    f = CHECKS["pilot_cumulative_limits"]
    assert not f(crew(fdp_hours_168h=50, fdp_hours_672h=150, flight_hours_672h=80, flight_hours_365d=800,
                      longest_free_stretch_168h=40, planned_fdp_h=9, planned_flight_h=6), L)
    assert "168 h" in f(crew(fdp_hours_168h=52, planned_fdp_h=9), L)
    assert "672 h" in f(crew(fdp_hours_672h=185, planned_fdp_h=9), L)
    assert "flight hours in 672" in f(crew(flight_hours_672h=96, planned_flight_h=5), L)
    assert "365" in f(crew(flight_hours_365d=998, planned_flight_h=3), L)
    assert "30 h" in f(crew(longest_free_stretch_168h=29), L)
    assert f(crew(fdp_hours_168h=None), L)                                       # unknown is unsafe


def test_a_pilot_who_reported_fatigue_cannot_be_scheduled():
    f = CHECKS["pilot_not_fatigued"]
    assert f(crew(fatigue_reported=True), L) and f(crew(fatigue_reported=None), L)
    assert not f(crew(fatigue_reported=False), L) and not f({}, L)


# ------------------------------------------------------------------ flight attendants

def fa(**kw):
    return {"fa": kw}


def test_flight_attendant_duty_limits_14_16_18_20():
    f = CHECKS["fa_duty_limit"]
    assert not f(fa(duty_h=14), L) and f(fa(duty_h=14.5), L)
    assert not f(fa(duty_h=16, extra_fas=1), L) and f(fa(duty_h=16.5, extra_fas=1), L)
    assert not f(fa(duty_h=18, extra_fas=2), L) and f(fa(duty_h=18.5, extra_fas=2), L)
    assert f(fa(duty_h=19, extra_fas=3), L)                                      # 18-20 h only with a flight outside the 48 states
    assert not f(fa(duty_h=20, extra_fas=3, outside_48=True), L)
    assert f(fa(duty_h=None), L)


def test_flight_attendants_beyond_airline_control_exception():
    f = CHECKS["fa_duty_limit"]
    assert not f(fa(duty_h=15, scheduled_duty_h=13, beyond_carrier_control=True), L)       # weather pushed a legal schedule over
    assert f(fa(duty_h=15, scheduled_duty_h=15, beyond_carrier_control=True), L)            # it was scheduled over the limit
    assert f(fa(duty_h=15, scheduled_duty_h=13, beyond_carrier_control=False), L)


def test_flight_attendant_rest_10_or_12_hours_and_24_off_in_7_days():
    f = CHECKS["fa_rest"]
    assert not f(fa(previous_duty_h=14, rest_h=10), L) and f(fa(previous_duty_h=14, rest_h=9.5), L)
    assert not f(fa(previous_duty_h=15, rest_h=12), L) and f(fa(previous_duty_h=15, rest_h=11), L)
    assert not f(fa(longest_off_h_7d=24), L) and f(fa(longest_off_h_7d=20), L)
    assert f(fa(previous_duty_h=None, rest_h=10), L)


# ------------------------------------------------------------------ ground staff

def test_ground_staff_shift_break_gap_and_days_limits():
    f = CHECKS["ground_staff_limits"]
    ok = {"staff": {"shift_min": 600, "min_since_break": 250, "planned_break_min": 30, "gap_min": 700, "consecutive_days": 5}}
    assert not f(ok, L)
    for key, bad in (("shift_min", 721), ("min_since_break", 301), ("planned_break_min", 20), ("gap_min", 500), ("consecutive_days", 7)):
        assert f({"staff": {**ok["staff"], key: bad}}, L), key
    assert f({"staff": {"shift_min": None}}, L)


def test_ground_staff_check_reads_the_staff_table_fields():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    start = datetime(2026, 10, 6, 4, 0, tzinfo=ZoneInfo("America/Chicago"))
    row = {"shift_start": start, "shift_end": start + timedelta(minutes=510), "last_break": start + timedelta(hours=1)}
    assert staff_view(row, start + timedelta(hours=7)) == {"shift_min": 510, "min_since_break": 360}
    assert CHECKS["ground_staff_limits"]({"staff": staff_view(row, start + timedelta(hours=7))}, L)      # 6 h with no break


# ------------------------------------------------------------------ the tarmac helper

def plane(**kw):
    base = dict(kind="departure", door_close=600, now=600, international=False, gate_available=True, stairs_available=False,
                staff_available=True, ramp_open=True, station_has_customs=True, tsa_checkpoint_open=True,
                wheelchair=0, wheelchair_staff=2, child_alone=0, child_alone_staff=1)
    base.update(kw)
    return base


def test_tarmac_deadlines_for_a_departure_start_at_door_close():
    t = tarmac_timeline(plane(), R)
    assert t["clock_starts_at"] == "door_close" and t["limit_min"] == 180
    assert t["deadlines"] == {"status_announcement": 630, "food_and_water": 720, "start_return_by": 750, "limit": 780}
    assert t["always"] == ["working toilets", "medical attention if needed"]
    assert t["notify_each_time_deplaning_is_possible"] is True


def test_tarmac_clock_for_an_arrival_or_a_diversion_starts_at_touchdown_and_international_gets_4_hours():
    t = tarmac_timeline(plane(kind="arrival", touchdown=500, now=520, international=True), R)
    assert t["clock_start"] == 500 and t["limit_min"] == 240 and t["deadlines"]["limit"] == 740
    d = tarmac_timeline(plane(kind="diversion", touchdown=480, now=500), R)
    assert d["clock_starts_at"] == "touchdown" and d["notify_each_time_deplaning_is_possible"] is True
    assert tarmac_timeline(plane(kind="arrival", touchdown=500), R)["notify_each_time_deplaning_is_possible"] is False


def test_tarmac_report_to_dot_is_due_after_the_limit():
    assert tarmac_timeline(plane(now=779), R)["dot_report_due_within_days"] is None
    assert tarmac_timeline(plane(now=781), R)["dot_report_due_within_days"] == 30


def test_tarmac_blockers_are_listed():
    t = tarmac_timeline(plane(gate_available=False, stairs_available=False, staff_available=False, wheelchair=3, wheelchair_staff=0), R)
    assert any("no ground staff" in b for b in t["blockers"]) and any("no gate and no stairs" in b for b in t["blockers"])
    assert any("wheelchair" in b for b in t["partial_blockers"])
    lightning = tarmac_timeline(plane(gate_available=False, stairs_available=True, ramp_open=False), R)
    assert any("lightning" in b for b in lightning["blockers"])
    no_customs = tarmac_timeline(plane(international=True, station_has_customs=False), R)
    assert any("customs" in b for b in no_customs["blockers"])
    no_tsa = tarmac_timeline(plane(gate_available=False, stairs_available=True, tsa_checkpoint_open=False), R)
    assert any("TSA" in b for b in no_tsa["blockers"])
    kids = tarmac_timeline(plane(child_alone=2, child_alone_staff=0), R)
    assert any("children" in b for b in kids["partial_blockers"])


def test_the_tarmac_helper_never_decides_to_keep_people_on_board():
    seen = set()
    for now in range(600, 900, 5):
        for blocked in (True, False):
            t = tarmac_timeline(plane(now=now, gate_available=not blocked, stairs_available=False, staff_available=not blocked), R)
            seen.add(t["decision"])
            assert t["decision"] in ("deplane_when_possible", "captain_decides", "not_yet")
    assert seen == {"deplane_when_possible", "captain_decides", "not_yet"}
    early = tarmac_timeline(plane(now=610, gate_available=False, staff_available=False), R)
    late = tarmac_timeline(plane(now=760, gate_available=False, staff_available=False), R)
    assert early["decision"] == "not_yet" and late["decision"] == "captain_decides"
    assert "captain" in late["message"] and len(late["human_only_exceptions"]) == 2


def test_keeping_people_on_board_past_the_limit_is_human_only_and_must_be_logged():
    f = CHECKS["tarmac_exception_needs_human"]
    assert f({"relies_on_tarmac_exception": True}, L)
    assert f({"relies_on_tarmac_exception": True, "tarmac_exception": {"decided_by": "co-pilot", "logged": True}}, L)
    assert f({"relies_on_tarmac_exception": True, "tarmac_exception": {"decided_by": "captain", "logged": False}}, L)
    assert not f({"relies_on_tarmac_exception": True, "tarmac_exception": {"decided_by": "captain", "logged": True}}, L)
    assert not f({}, L)
    assert R["autonomy"]["keep_on_board_exception"]["level"] == "human_only"
    kept, removed = screen([{"id": "stay on board", "relies_on_tarmac_exception": True}], R)
    assert not kept and removed[0]["rung"] == 1


def test_the_start_return_before_limit_assumption_is_30_minutes():
    t = tarmac_timeline(plane(), R)
    assert t["deadlines"]["limit"] - t["deadlines"]["start_return_by"] == 30


# ------------------------------------------------------------------ lightning, curfews, diversion airports

def test_lightning_close_to_the_airport_stops_ramp_work_even_if_the_ramp_is_still_open():
    f = CHECKS["ramp_closed_for_lightning"]
    assert f({"lightning_distance_mi": 4, "ramp_activity": "fueling"}, L)
    for job in ("fueling", "loading", "pushback", "towing", "marshalling"):
        assert f({"lightning_distance_mi": 5, "ramp_activity": job}, L), job
    assert not f({"lightning_distance_mi": 9, "ramp_activity": "fueling"}, L)
    assert not f({"lightning_distance_mi": 2, "ramp_activity": "boarding"}, L)


def test_a_plane_struck_by_lightning_needs_an_inspection_before_its_next_flight():
    f = CHECKS["lightning_strike_inspection"]
    assert f({"aircraft": {"struck_by_lightning": True}}, L)
    assert f({"aircraft": {"struck_by_lightning": True, "inspection_done": False}}, L)
    assert not f({"aircraft": {"struck_by_lightning": True, "inspection_done": True}}, L)
    assert not f({"aircraft": {"struck_by_lightning": False}}, L)
    assert f({"aircraft": {"struck_by_lightning": None}}, L)
    kept, removed = screen([{"id": "fly A31 now", "aircraft": {"struck_by_lightning": True}}], R)
    assert removed[0]["rung"] == 2


def test_san_has_a_departure_curfew_from_2330_to_0630():
    f = CHECKS["airport_curfew"]
    dep = lambda t, a="SAN": {"airport": a, "operation": "departure", "local_time": t}
    assert f(dep("23:30"), L) and f(dep("23:59"), L) and f(dep("02:00"), L) and f(dep("06:30"), L)
    assert not f(dep("23:29"), L) and not f(dep("06:31"), L) and not f(dep("12:00"), L)
    assert not f({"airport": "SAN", "operation": "arrival", "local_time": "01:00"}, L)       # only departures are banned
    assert not f(dep("01:00", "MCI"), L) and not f(dep("01:00", "LAS"), L)                    # no curfew there
    assert f({"airport": "SAN", "operation": "departure", "local_time": None}, L)             # unknown time is unsafe


def test_dca_night_flying_needs_the_noise_rule():
    f = CHECKS["airport_curfew"]
    o = {"airport": "DCA", "operation": "arrival", "local_time": "23:10"}
    assert f(o, L) and f({**o, "meets_noise_rule": False}, L)
    assert not f({**o, "meets_noise_rule": True}, L) and not f({**o, "local_time": "15:00"}, L)


def test_international_diversions_need_customs_at_the_diversion_airport():
    stl = {"id": "STL", "international": True, "station": {"has_customs": True}}
    ict = {"id": "ICT", "international": True, "station": {"has_customs": False}}
    kept, removed = screen([stl, ict], R)
    assert [o["id"] for o in kept] == ["STL"] and removed[0]["option"] == "ICT" and removed[0]["rung"] == 3


def test_tsa_checkpoint_and_southwest_staff_hours_at_diversion_stations():
    tsa, staff = CHECKS["tsa_rescreening"], CHECKS["station_staffed"]
    out = {"airport": "ICT", "deplaned_outside_secure": True}
    assert tsa({**out, "local_time": "21:00"}, L) and not tsa({**out, "local_time": "12:00"}, L)
    assert not tsa({"airport": "ICT", "local_time": "23:00"}, L)                               # nobody deplaned outside
    assert tsa({**out, "airport": "XXX", "local_time": "12:00"}, L)                            # unknown station is unsafe
    assert not tsa({**out, "airport": "DEN", "local_time": "03:00"}, L)                        # DEN is open all day
    need = {"airport": "DSM", "needs_station_staff": True}
    assert staff({**need, "local_time": "22:30"}, L) and not staff({**need, "local_time": "20:00"}, L)
    assert staff({**need, "local_time": "04:00"}, L) and not staff({"airport": "DSM", "local_time": "04:00"}, L)


# ------------------------------------------------------------------ wheelchairs and children

def test_nobody_is_left_alone_in_a_wheelchair_for_more_than_30_minutes():
    f = CHECKS["wheelchair_unattended"]
    assert not f({"pax": {"wheelchair": 1, "wheelchair_unattended_min": 30}}, L)
    assert f({"pax": {"wheelchair": 1, "wheelchair_unattended_min": 31}}, L)
    assert not f({"pax": {"wheelchair": 1, "wheelchair_unattended_min": 90, "wheelchair_waived": True}}, L)   # the passenger waived it
    assert f({"pax": {"wheelchair": 1, "wheelchair_unattended_min": None}}, L)
    kept, removed = screen([{"id": "leave in chair", "pax": {"wheelchair": 1, "wheelchair_unattended_min": 45}}], R)
    assert removed[0]["rung"] == 5


def test_a_stranded_child_is_never_sent_to_a_hotel_alone():
    f = CHECKS["child_alone_not_to_hotel_alone"]
    kid = {"pax": {"child_alone": 1}, "destination_kind": "hotel"}
    assert f(kid, L) and not f({**kid, "staff_supervised": True}, L)
    assert not f({"pax": {"child_alone": 1}, "destination_kind": "gate"}, L)
    assert not f({"pax": {"child_alone": 0}, "destination_kind": "hotel"}, L)


def test_children_alone_nonstop_only_and_never_international_still_hold():
    kept, removed = screen([{"id": "connection", "pax": {"child_alone": 1}, "plane_changes": 1, "child_alone_plan": True},
                            {"id": "international", "pax": {"child_alone": 1}, "international": True, "child_alone_plan": True},
                            {"id": "nonstop", "pax": {"child_alone": 1}, "plane_changes": 0, "child_alone_plan": True}], R)
    assert [o["id"] for o in kept] == ["nonstop"] and len(removed) == 2


def test_every_check_named_in_the_ladder_exists_and_rules_are_not_changed_by_loading():
    before = copy.deepcopy(R["limits"])
    load_rules.cache_clear()
    assert load_rules()["limits"] == before
    for rung in R["safety_ladder"]:
        for name in rung.get("checks", []):
            assert name in CHECKS
