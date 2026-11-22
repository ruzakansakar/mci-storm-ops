"""One test (or more) per root cause fixed after the 60-day chaos run (chaos seeds 3791350-3791409).

Most are pure: they build the agent's picture from a handful of events (no database). A few replay a real chaos day in memory
(chaos/memory_replay.py: the same events, late deliveries and feed silences as in the database) and score it with the judge.
"""
import copy
from datetime import timedelta

import pytest

from backend.agents.situation import assess as A
from backend.agents.situation.run import wake_times
from backend.agents.situation.state_at import blocks, build_state, parse_clock
from backend.contracts import load_contracts
from backend.contracts.feeds import Feeds
from backend.rules import recovery as R
from chaos.memory_replay import MemoryContext, play_day, score

T = parse_clock
S = A.load_settings()
FLIGHTS = [{"id": f"WN10{i:02d}", "direction": "departure", "origin": "MCI", "destination": "DEN", "sched_dep": T(f"{8 + i}:00"),
            "sched_arr": T(f"{9 + i}:30"), "crew_id": f"C{i}"} for i in range(1, 12)] + \
          [{"id": "WN2001", "direction": "arrival", "origin": "DEN", "destination": "MCI", "sched_dep": T("09:00"),
            "sched_arr": T("10:30"), "crew_id": None}]


@pytest.fixture(scope="module")
def data():
    from backend.seed.build import build
    return build()


def ev(n, clock, typ, payload, feed=None, delivered=None):
    contract = load_contracts()["situation"]
    feed_, fields = contract.event_types[typ] if typ in contract.event_types else (feed or "faa_feed", list(payload))
    return {"id": n, "occurred_at": T(clock), "published_at": T(delivered) if delivered else None, "type": typ, "station": "MCI",
            "payload": payload, "feed": feed or feed_, "_fields": fields}


def stop(n, clock, typ, end_min):
    return ev(n, clock, typ, {"program": "ground_stop", "expected_end": end_min, "reason": "thunderstorms"})


def state(events, clock):
    """The agent's picture at `clock`, from these events as AgentContext would hand them over."""
    day = {"events": events, "schedule": FLIGHTS, "seed": 0}
    return build_state(FLIGHTS, MemoryContext(day, T(clock), Feeds(), None).read_events(), T(clock))


def assess_at(events, clock, feeds=None):
    day = {"events": events, "schedule": FLIGHTS, "seed": 0}
    return A.assess(MemoryContext(day, T(clock), feeds or Feeds(), {"versions": [], "events": [], "recs": []}), A.Memory(),
                    write=False)["assessment"]


# ------------------------------------------------------------------ 3791390 / 3791364 / 3791374: the announced end

def test_duplicate_late_and_out_of_order_faa_notices_do_not_move_the_announced_end():
    started = stop(1, "12:00", "ground_stop_started", 13 * 60)
    ext = stop(2, "12:50", "ground_stop_extended", 14 * 60)
    dup = {**stop(3, "12:50", "ground_stop_extended", 14 * 60)}                         # the same notice sent twice
    late_old = stop(4, "12:30", "ground_stop_extended", 13 * 60 + 30) | {"published_at": T("12:55")}   # issued earlier, arrives later
    bad = ev(5, "12:52", "ground_stop_extended", {"program": "ground_stop", "expected_end": "later"})  # malformed
    for order in ([started, ext, dup, late_old, bad], [bad, late_old, dup, ext, started]):
        st = state(order, "13:00")
        assert st.program.announced_end == T("14:00")                 # the latest issue time wins, whatever the arrival order
        assert st.program.extensions == 2 and st.duplicates == 1 and len(st.unreadable) == 1
        assert not st.past_programs


def test_a_reissued_start_notice_is_an_update_not_a_new_program():
    st = state([stop(1, "12:00", "ground_stop_started", 13 * 60), stop(2, "12:40", "ground_stop_started", 13 * 60 + 45)], "12:45")
    assert not st.past_programs and st.program.started == T("12:00")
    assert st.program.announced_end == T("13:45") and st.program.extensions == 1


def test_two_faa_notices_with_the_same_issue_time_and_different_ends_are_a_conflict_a09():
    events = [stop(1, "12:00", "ground_stop_started", 13 * 60), stop(2, "12:30", "ground_stop_extended", 14 * 60),
              stop(3, "12:30", "ground_stop_extended", 13 * 60 + 30)]
    st = state(events, "12:35")
    assert st.program.announced_end == T("14:00")                     # the later one until a human says which is right
    a = assess_at(events, "12:35")
    assert "A09" in a["decision_ids"] and any(c["key"].startswith("faa-notices:") for c in a["conflicts"])


def test_a_storm_that_ends_early_is_inside_the_range():
    """3791390: one notice, the stop ended 30 min before its announced end. The range now has an early side for storms."""
    a = assess_at([stop(1, "19:53", "ground_stop_started", 22 * 60)], "20:00")
    assert a["end_earliest"] <= T("21:30") <= a["end_latest"]


def test_the_end_range_follows_the_programs_own_cause_not_the_days_main_cause():
    """A storm during an IT outage: the stop's end depends on the storm, even if the IT outage is the main suspected cause."""
    events = [stop(1, "08:37", "ground_stop_started", 9 * 60 + 13),
              ev(2, "08:37", "system_status_changed", {"system": "Amadeus Altea (simulated)", "status": "down"})]
    st = state(events, "08:38")
    end = A._expected_end(st, "it_outage", S)
    assert end["features"]["cause"] == "weather" and end["earliest"] < T("09:13")


def test_high_confidence_is_only_used_where_training_days_proved_it():
    x = S["expected_end"]
    table = x["confidence_by_group"]
    for key in [k for k in table if k.count("|") == 3]:
        cause, changes, time, messy = key.split("|")
        f = {"cause": cause, "changes": changes, "time": time, "messy": messy == "messy"}
        c = A.end_confidence(f, S)
        used = next(k for k in A.group_keys(f) if k in table and table[k][1] >= x["min_group_ticks"]) \
            if any(k in table and table[k][1] >= x["min_group_ticks"] for k in A.group_keys(f)) else None
        if c >= x["confidence_words"]["high"]:
            assert used and min(table[used][0], x["max_confidence"]) == c, key   # "high" only from a proven group (capped)
    f = {"cause": "crew", "changes": "0", "time": "day", "messy": False}         # a group never seen on training days
    assert A.end_confidence(f, S) < x["confidence_words"]["high"]


def test_confidence_never_rises_and_the_range_never_narrows_after_more_changes():
    x = S["expected_end"]
    for kind, late in x["late_margin_min"].items():
        assert late == sorted(late), kind
    table = x["confidence_by_group"]
    for key, (conf, n) in table.items():
        parts = key.split("|")
        if len(parts) > 1 and parts[1] == "2+":
            for fewer in ("0", "1"):
                k = "|".join([parts[0], fewer] + parts[2:])
                if k in table and table[k][1] >= x["min_group_ticks"]:
                    assert conf <= table[k][0], key


def test_a_premature_stop_ended_notice_keeps_the_range_open_while_planes_still_hold():
    events = [stop(1, "20:30", "ground_stop_started", 22 * 60), ev(2, "21:00", "flight_holding", {"flight_id": "WN2001"}),
              ev(3, "21:05", "flight_holding", {"flight_id": "WN1003"}),
              ev(4, "21:20", "ground_stop_ended", {"program": "ground_stop", "reason": "thunderstorms"})]
    a = assess_at(events, "21:25")
    assert a["status"] == "abnormal" and a["end_latest"] >= T("22:00") and a["end_confidence"] < 0.7


# ------------------------------------------------------------------ 3791409: a crew-cancellation wave

def test_a_late_delivered_cancellation_wakes_the_agent_when_it_arrives():
    times = wake_times([T("11:22")], Feeds(), S)
    assert T("11:22") in times
    assert {"flight_delayed", "flight_delay_updated", "flight_cancelled"} <= set(S["run"]["trigger_events"])


def test_two_crew_cancellations_are_noticed_at_once_as_crew_suspected_with_low_confidence():
    cx = lambda n, clock, fid: ev(n, clock, "flight_cancelled", {"flight_id": fid, "reason": "crew out of legal duty", "pax": 100})
    one = assess_at([cx(1, "10:55", "WN1003")], "10:56")
    assert one["status"] == "normal"                                   # a single crew cancellation happens on ordinary days
    two = assess_at([cx(1, "10:55", "WN1003"), cx(2, "11:40", "WN1004")], "11:40")
    assert two["status"] == "abnormal" and two["type"] == "crew" and two["confidence"] < 0.5
    four = assess_at([cx(1, "10:55", "WN1003"), cx(2, "11:40", "WN1004"), cx(3, "11:45", "WN1005"), cx(4, "11:50", "WN1006")], "11:50")
    assert four["confidence"] > two["confidence"]                      # more evidence, more confidence


def test_the_crew_wave_day_3791409_is_noticed_within_5_minutes(data):
    sc = score(play_day(3791409, data))
    det = next(c for c in sc["checks"] if c["name"] == "detection")
    assert det["result"] == "PASS" and max(sc["detect_minutes"]) <= 5


def test_the_judge_starts_the_detection_clock_when_the_signal_could_be_seen(data):
    truth = play_day(3791409, data)["truth"]
    crew = next(i for i in truth["incidents"] if i["kind"] == "crew_wave")
    assert crew["first_signal"][11:16] == "11:05" and crew["first_visible"][11:16] == "11:22"     # delivered 17 min late


# ------------------------------------------------------------------ 3791386: stale feeds and the conflict

def test_a_silence_inside_a_longer_silence_does_not_make_the_heartbeat_look_fresh():
    f = Feeds(outages={"flight_ops": [(T("06:45"), T("07:15")), (T("06:09"), T("08:26"))]})
    assert f.window("flight_ops", T("06:45")) == (T("06:09"), T("08:26"))
    assert f.last_heartbeat("flight_ops", T("07:00")) == T("06:09") and f.is_up("flight_ops", T("08:26"))


def test_health_up_feed_down_is_not_a_conflict_while_system_health_says_down(data):
    truth = play_day(3791386, data)["truth"]
    cf = next(c for c in truth["conflicts"] if c["kind"] == "health_up_feed_down")
    assert cf["detectable"] is False


def test_the_day_3791386_now_refuses_to_guess_all_through_the_silence(data):
    sc = score(play_day(3791386, data))
    for name in ("stale feeds", "conflicts"):
        assert next(c for c in sc["checks"] if c["name"] == name)["result"] in ("PASS", "N/A"), name


def test_the_judge_prints_times_in_mci_local_time():
    from datetime import timezone
    from chaos.judge import _local
    assert _local(T("06:45").astimezone(timezone.utc)).strftime("%H:%M") == "06:45"


def test_one_s01_for_a_feed_and_the_system_behind_it():
    events = [stop(1, "06:00", "ground_stop_started", 8 * 60),
              ev(2, "06:09", "system_status_changed", {"system": "Amadeus Altea (simulated)", "status": "down"})]
    day = {"events": events, "schedule": FLIGHTS, "seed": 0}
    feeds = Feeds(outages={"flight_ops": [(T("06:09"), T("08:26"))]})
    sink, memory = {"versions": [], "events": [], "recs": []}, A.Memory()
    for clock in ("06:00", "06:09", "06:15", "06:20", "06:25", "06:30"):
        A.assess(MemoryContext(day, T(clock), feeds, sink), memory)
    s01 = [r for r in sink["recs"] if "S01" in r["decision_ids"]]
    assert len(s01) == 1 and "flight_ops" in s01[0]["title"] and "Amadeus" in s01[0]["title"]


# ------------------------------------------------------------------ recovery: one definition for the agent and the judge

def test_operations_back_needs_every_condition():
    blocks_ = [(T("10:00"), T("11:00"), "FAA ground stop active"), (T("10:40"), T("11:05"), "WN2001 holding")]
    activity = [(T("11:10"), "WN1003 delayed")]
    assert not R.operations_back(T("11:02"), blocks_, activity, 30)       # still holding
    assert not R.operations_back(T("11:20"), blocks_, activity, 30)       # a new delay 10 min ago
    assert R.operations_back(T("11:40"), blocks_, activity, 30)
    assert R.busy_spells(T("11:40"), blocks_, activity, 30)[-1]["back_at"] == T("11:40")
    assert R.aftermath(0, [], 3) and R.aftermath(0, [], 3, incident_closed=True) == [] and R.aftermath(None, [], 0)


def test_the_agent_and_the_judge_use_the_same_definition():
    from backend.settings import ROOT
    for path in ("backend/agents/situation/assess.py", "chaos/judge.py"):
        assert "from backend.rules import recovery" in (ROOT / path).read_text(encoding="utf-8"), path


def test_recovery_switches_when_operations_are_back_and_normal_when_the_aftermath_is_cleared():
    events = [stop(1, "09:00", "ground_stop_started", 10 * 60), ev(2, "09:10", "flight_diverted", {"flight_id": "WN2001"}),
              ev(3, "09:50", "flight_delayed", {"flight_id": "WN1003", "delay_min": 40}),
              ev(4, "10:00", "ground_stop_ended", {"program": "ground_stop", "reason": "thunderstorms"}),
              ev(5, "11:30", "flight_arrived", {"flight_id": "WN2001", "after_diversion": True})]
    assert assess_at(events, "10:10")["status"] == "abnormal"            # a flight newly delayed 20 min ago: not back yet
    a = assess_at(events, "10:25")
    assert a["status"] == "recovery" and any("out of position" in x for x in a["aftermath"])
    assert assess_at(events, "11:35")["status"] == "normal"               # the diverted aircraft is back: aftermath cleared


def test_passengers_stranded_overnight_keep_it_in_recovery():
    events = [stop(1, "09:00", "ground_stop_started", 10 * 60),
              ev(2, "09:20", "flight_cancelled", {"flight_id": "WN1003", "reason": "weather", "pax": 120}),
              ev(3, "09:20", "passenger_stranded_overnight", {"lives_locally": False, "from_flight": "WN1003"}),
              ev(4, "10:00", "ground_stop_ended", {"program": "ground_stop", "reason": "thunderstorms"})]
    for clock in ("10:05", "14:00", "23:00"):
        assert assess_at(events, clock)["status"] == "recovery", clock


def test_crews_timing_out_after_a_storm_are_its_aftermath_not_a_new_incident():
    cx = lambda n, clock, fid: ev(n, clock, "flight_cancelled", {"flight_id": fid, "reason": "crew out of legal duty", "pax": 100})
    storm = [stop(1, "09:00", "ground_stop_started", 10 * 60),
             ev(2, "10:00", "ground_stop_ended", {"program": "ground_stop", "reason": "thunderstorms"})]
    after = storm + [cx(3, "11:00", "WN1004"), cx(4, "11:20", "WN1005")]
    assert assess_at(after, "11:25")["status"] == "recovery"
    later = storm + [cx(3, "15:00", "WN1008"), cx(4, "15:20", "WN1009")]               # hours later: a new crew wave
    assert assess_at(later, "15:20")["status"] == "abnormal"


@pytest.mark.parametrize("seed", [3791370, 3791358, 3791408])
def test_recovery_warn_days_now_pass(data, seed):
    sc = score(play_day(seed, data))
    assert next(c for c in sc["checks"] if c["name"] == "recovery")["result"] == "PASS"


def test_quiet_and_normal_variation_days_still_say_normal(data):
    for seed in (s for s in range(1, 101) if __import__("chaos.generator", fromlist=["plan_day"]).plan_day(s).ingredients
                 in (["quiet"], ["normal_variation"])):
        sc = score(play_day(seed, data))
        assert not [v for v in sc["outputs"]["versions"] if v["status"] in ("abnormal", "recovery")], seed
