"""Phase 3 checks: does the simulator create a believable day and a believable gap between truth and screens?"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

from backend.database import connect
from backend.seed.build import build
from simulator.channels import count_disagreements
from simulator.clock import hhmm
from simulator.config import load_config
from simulator.run import NotBuiltYet, run, simulate
from tests.conftest import one

ROOT = Path(__file__).resolve().parents[1]
CAUSES = {"stale_copy", "sync_delay", "seat_hold", "message_failed", "bag_system"}


@pytest.fixture(scope="session")
def data():
    return build()


_cache = {}


def sim(data, seed, scenario):
    if (seed, scenario) not in _cache:
        _cache[(seed, scenario)] = simulate(seed, scenario, data=data)
    return _cache[(seed, scenario)]


def status_at(world, fid, minute):
    status = "scheduled"
    for a in world.audit_rows:
        if a["entity_type"] == "flight" and a["entity_id"] == fid and "status" in a["after"] and a["t"] <= minute:
            status = a["after"]["status"]
    return status


def ramp_open_at(world, minute):
    ramp = True
    for a in world.audit_rows:
        if a["entity_type"] == "station" and "ramp_open" in a["after"] and a["t"] <= minute:
            ramp = a["after"]["ramp_open"]
    return ramp


# ------------------------------------------------------------------ determinism

def test_same_seed_same_world(data):
    a, b = simulate(3, "random", data=data), simulate(3, "random", data=data)
    assert a.world.events == b.world.events
    assert a.world.audit_rows == b.world.audit_rows
    assert a.metrics == b.metrics


def test_a31_same_seed_same_world(data):
    a, b = sim(data, 7, "a31_storm"), simulate(7, "a31_storm", data=data)
    assert a.world.events == b.world.events and a.metrics == b.metrics


def test_different_seeds_differ(data):
    a, b = simulate(1, "random", data=data), simulate(2, "random", data=data)
    assert a.world.events != b.world.events


# ------------------------------------------------------------------ the class story

def test_a31_ground_stop_and_airborne_a31(data):
    r = sim(data, 7, "a31_storm")
    stops = [e for e in r.world.events if e["type"] == "ground_stop_started"]
    assert len(stops) == 1 and stops[0]["station"] == "MCI" and stops[0]["t"] == hhmm("11:30")
    assert [e["t"] for e in r.world.events if e["type"] == "ground_stop_ended"] == [hhmm("14:00")]
    for t in ("11:30", "12:00", "12:14"):
        assert status_at(r.world, "WN2031", hhmm(t)) == "airborne"
    takeoff = next(e for e in r.world.events if e["type"] == "flight_airborne" and e["payload"]["flight_id"] == "WN2031")
    assert takeoff["payload"]["fuel_minutes_extra"] == 55


def test_a31_diverts_after_about_30_minutes_of_holding(data):
    r = sim(data, 7, "a31_storm")
    div = [e for e in r.world.events if e["type"] == "flight_diverted" and e["payload"]["flight_id"] == "WN2031"]
    assert len(div) == 1 and div[0]["station"] == "STL"
    assert div[0]["t"] == hhmm("12:15")                      # holding began 11:45; 55 - 30 - 15 = 10 spare minutes left
    assert div[0]["payload"]["fuel_minutes_extra"] == 25 and div[0]["payload"]["extra_flying_min"] == 15
    assert status_at(r.world, "WN2031", hhmm("12:15")) == "diverted"


def test_no_diverted_flight_reaches_its_alternate_below_the_fuel_floor(data):
    floor = load_config()["limits"]["fuel_buffer_min"]
    seen = 0
    for seed in range(1, 21):
        for scenario in ("random", "a31_storm"):
            for e in sim(data, seed, scenario).world.events:
                if e["type"] == "flight_diverted":
                    seen += 1
                    p = e["payload"]
                    assert p["fuel_minutes_extra"] - p["extra_flying_min"] >= floor, (seed, scenario, p)
    assert seen > 20                                          # the check really looked at diversions


def test_a31_story_comes_from_knock_on_causes(data):
    r = sim(data, 7, "a31_storm")
    assert r.metrics["flights_cancelled"] >= 5
    reasons = [e["payload"]["reason"] for e in r.world.events if e["type"] == "flight_cancelled"]
    assert any(x.startswith("schedule thinning") for x in reasons)
    assert "crew out of legal duty" in reasons
    # A31's own fuel and diversion behaviour did not change
    div = next(e for e in r.world.events if e["type"] == "flight_diverted" and e["payload"]["flight_id"] == "WN2031")
    assert (div["t"], div["station"]) == (hhmm("12:15"), "STL")


def test_maya_flight_cancelled_for_crew_hours_and_she_is_stranded_overnight(data):
    r = sim(data, 7, "a31_storm")
    w = r.world
    f = w.flights["WN1033"]                                          # Maya's flight to Baltimore
    assert f["status"] == "cancelled" and f["cancel_reason"] == "crew out of legal duty"
    out = next(e for e in w.events if e["type"] == "crew_out_of_duty" and e["payload"]["flight_id"] == "WN1033")
    assert out["payload"]["reserve_found"] is False and out["payload"]["duty_left_min"] < out["payload"]["needed_min"]
    maya = w.passengers["PAX0001"]
    assert maya["full_name"] == "Maya Torres" and maya["overnight"] is True
    booking = w.bookings["PAX0001"][-1]
    nxt = w.flights[booking["flight_id"]]
    assert booking["status"] == "rebooked" and nxt["_next_day"] and nxt["destination"] == "BWI"
    assert nxt["sched_dep"] >= 1440                                   # 7 Oct, next morning
    event = next(e for e in w.events if e["type"] == "passenger_stranded_overnight" and e["payload"]["passenger_id"] == "PAX0001")
    assert event["payload"]["lives_locally"] is False                 # recorded, nothing else decided (no hotel, no money)
    assert w.truth["PAX0001"]["status"] == "overnight"


def test_thinning_only_cancels_low_priority_flights_up_to_the_cap(data):
    th = load_config()["baseline"]["thinning"]
    for seed in (1, 4, 7):
        r = sim(data, seed, "a31_storm")
        thinned = [r.world.flights[i] for i in r.world.thinned]
        assert thinned and all(f["priority"] in th["priorities"] for f in thinned)
        assert all(f["status"] == "cancelled" for f in thinned)
        window = [f for f in r.world.departures if r.scenario.start <= f["sched_dep"] <= r.scenario.end + th["window_after_min"]]
        assert len(thinned) <= th["max_fraction_of_window"] * len(window)


def test_thinning_does_nothing_for_a_short_stop(data):
    cfg = load_config()
    short = [r for r in (sim(data, seed, "random") for seed in range(1, 21))
             if r.scenario.program == "ground_stop" and r.scenario.end - r.scenario.start < cfg["baseline"]["thinning"]["min_stop_min"]]
    # (there may be none among these seeds; the rule is also checked directly)
    assert all(not r.world.thinned for r in short)
    assert cfg["baseline"]["thinning"]["min_stop_min"] == 60


def test_reserve_crews_are_used_once_and_only_when_available(data):
    r = sim(data, 7, "a31_storm")
    used = [e["payload"]["reserve_id"] for e in r.world.events if e["type"] == "reserve_crew_assigned"]
    assert used and len(used) == len(set(used))
    usable = load_config()["baseline"]["crew"]["reserve_usable_status"]
    for rid in used:
        reserve = r.world.crews[rid]
        assert reserve["reserve_status"] == "Assigned"
    seed_status = {c["id"]: c["reserve_status"] for c in data["crews"] if c.get("is_reserve")}
    assert all(seed_status[rid] in usable for rid in used)


def test_a_diverted_aircraft_that_cannot_get_back_in_time_cancels_its_next_flight(data):
    found = False
    for seed in range(1, 21):
        for e in sim(data, seed, "random").world.events:
            if e["type"] == "flight_cancelled" and e["payload"]["reason"].startswith("aircraft out of position after diverting"):
                found = True
    assert found


def test_next_day_flights_are_on_7_oct_and_group_counts_still_add_up(data):
    r = sim(data, 7, "a31_storm")
    w = r.world
    nd = [f for f in w.flights.values() if f.get("_next_day")]
    assert nd and all(f["sched_dep"] >= 1440 and f["id"].startswith("WN3") for f in nd)
    for f in w.flights.values():
        assert sum(g["count"] for (fid, _), g in w.groups.items() if fid == f["id"]) == f["pax_total"]
    assert r.metrics["passengers_stranded_overnight"] > 0
    assert r.metrics["tracked_passengers_overnight"] == sum(p["overnight"] for p in w.passengers.values())


def test_rebooking_never_fills_a_plane_beyond_the_load_factor(data):
    r = sim(data, 7, "a31_storm")
    cap = load_config()["baseline"]["max_load_factor"]
    for f in r.world.flights.values():
        if f["_orig_pax"] < int(cap * (f.get("_seats") or (r.world.aircraft[f["aircraft_id"]]["seats"] if f["aircraft_id"] else 0))):
            seats = f.get("_seats") or r.world.aircraft[f["aircraft_id"]]["seats"]
            assert f["pax_total"] <= int(cap * seats)


def test_realism_cancellations_are_10_to_35_percent_of_departures_in_long_stops(data):
    """Across seeds 1-20, every storm / ground stop of 2+ hours cancels a believable share of the departures
    scheduled from the start of the stop to 2 hours after it ends."""
    rl = load_config()["realism"]
    assert (rl["min_cancelled_pct"], rl["max_cancelled_pct"]) == (10, 35)
    checked = []
    for seed in range(1, 21):
        for scenario in ("random", "a31_storm"):
            r = sim(data, seed, scenario)
            sc = r.scenario
            if sc.program != "ground_stop" or sc.end - sc.start < rl["min_stop_min"]:
                continue
            lo, hi = sc.start, sc.end + rl["window_after_min"]
            flights = [f for f in r.world.departures if lo <= f["sched_dep"] <= hi]
            pct = 100 * sum(f["status"] == "cancelled" for f in flights) / len(flights)
            checked.append((scenario, seed, round(pct, 1)))
            assert rl["min_cancelled_pct"] <= pct <= rl["max_cancelled_pct"], (scenario, seed, pct)
    assert len(checked) >= 25


def test_seed_7_story_is_still_five_cancellations(data):
    r = sim(data, 7, "a31_storm")
    assert r.metrics["flights_cancelled"] == 5
    assert r.world.flights["WN1033"]["cancel_reason"] == "crew out of legal duty"


def test_crews_with_illegal_rest_and_crews_short_for_our_plan_get_different_flags_at_5am_and_still_fly(data):
    w = sim(data, 7, "a31_storm").world
    rest = [e for e in w.events if e["type"] == "crew_illegal_rest"]
    plan = [e for e in w.events if e["type"] == "crew_short_for_plan"]
    assert rest and plan and all(e["t"] == hhmm("05:00") for e in rest + plan)
    assert not [e for e in w.events if e["type"] == "crew_illegal_in_source_data"]          # the old mixed flag is gone
    for group in (rest, plan):
        ids = [e["payload"]["crew_id"] for e in group]
        assert len(ids) == len(set(ids))                                                     # one event per crew per type
    short_rest = {c["id"] for c in data["crews"] if not c["is_reserve"] and c["min_rest_hours"] is not None and c["min_rest_hours"] < 10}
    flown = {c for c in short_rest if any(f["crew_id"] == c for f in w.departures)}
    assert {e["payload"]["crew_id"] for e in rest} == flown
    issues = {i["item"] for i in data["data_issues"] if i["category"] == "crew_rest"}
    for e in rest:
        p = e["payload"]
        assert p["rule"] == "pilot_min_rest_hours" and p["legal_problem"] is True and p["min_rest_hours"] < p["required_hours"]
        assert p["data_issue"] == {"category": "crew_rest", "item": p["crew_id"]} and p["crew_id"] in issues
    for e in plan:
        p = e["payload"]
        assert p["legal_problem"] is False and "not a legal limit" in p["note"] and p["data_issue"] is None
        assert all(b["duty_left_min"] < b["needed_min"] for b in p["short"])
    # today's baseline still lets them fly
    assert any(w.flights[f["id"]]["status"] in ("departed", "airborne", "arrived") for f in w.departures if f["crew_id"] in flown)


def test_story_lock_seed_7(data):
    """Frozen numbers for the class story. If this fails: the seed 7 story changed; check with the project owner before updating this test."""
    msg = "the seed 7 story changed; check with the project owner before updating this test"
    r = sim(data, 7, "a31_storm")
    w, m = r.world, r.metrics
    assert (m["flights_delayed"], m["flights_cancelled"], m["flights_diverted"]) == (17, 5, 9), msg
    assert m["passengers_stranded_overnight"] == 559, msg
    cancelled = sorted((f["id"], f["cancel_reason"]) for f in w.flights.values() if f["status"] == "cancelled")
    assert cancelled == [("WN1032", "schedule thinning: 90 min ground stop announced"),
                         ("WN1033", "crew out of legal duty"),
                         ("WN1035", "schedule thinning: 90 min ground stop announced"),
                         ("WN1038", "schedule thinning: 90 min ground stop announced"),
                         ("WN1040", "schedule thinning: 150 min ground stop announced")], msg
    div = next(e for e in w.events if e["type"] == "flight_diverted" and e["payload"]["flight_id"] == "WN2031")
    assert (div["t"], div["station"]) == (hhmm("12:15"), "STL"), msg
    land = next(e for e in w.events if e["type"] == "flight_arrived" and e["payload"]["flight_id"] == "WN2031")
    assert (land["t"], land["station"]) == (hhmm("14:10"), "MCI"), msg
    assert w.flights["WN1033"]["reserve_crew_ids"] == ["R04"], msg
    maya = w.bookings["PAX0001"][-1]
    nxt = w.flights[maya["flight_id"]]
    assert w.passengers["PAX0001"]["overnight"] is True and maya["flight_id"] == "WN3005", msg
    assert nxt["sched_dep"] == 1440 + hhmm("06:30"), msg                                      # 06:30 on 2026-10-07
    from simulator.clock import at
    assert at(nxt["sched_dep"]).date().isoformat() == "2026-10-07", msg


def test_the_rules_named_in_the_flags_exist_in_the_rules_folder():
    import yaml
    ids = {r["id"] for path in (ROOT / "config" / "rules").glob("*.yaml") for r in yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]}
    assert set(load_config()["baseline"]["crew"]["source_data_rules"].values()) <= ids


def test_a_reserve_crew_that_takes_over_is_recorded_on_the_flight_and_burns_its_own_duty(data):
    w = sim(data, 7, "a31_storm").world
    f = w.flights["WN1033"]                                              # Maya's flight keeps reserve pilots R04
    assert f["reserve_crew_ids"] == ["R04"] and f["crew_id"] == "C33"
    swap = next(e for e in w.events if e["type"] == "reserve_crew_assigned" and e["payload"]["flight_id"] == "WN1033")
    assert swap["payload"]["reserve_id"] == "R04" and swap["payload"]["role"] == "pilot" and swap["payload"]["crew_id"] == "C33"
    assert w.crews["R04"]["reserve_status"] == "Assigned"
    start_duty = next(c for c in data["crews"] if c["id"] == "R04")["pilot_duty_left_min"]
    assert w.crews["R04"]["pilot_duty_left_min"] < start_duty            # the reserve's own clock ran down while the flight waited
    audit = [a for a in w.audit_rows if a["entity_type"] == "flight" and a["entity_id"] == "WN1033" and "reserve_crew_ids" in a["after"]]
    assert audit and audit[0]["after"]["reserve_crew_ids"] == ["R04"]
    assert w.flights["WN1033"]["status"] == "cancelled" and w.flights["WN1033"]["cancel_reason"] == "crew out of legal duty"


def test_every_reserve_is_used_for_one_flight_only(data):
    for seed in (1, 4, 7):
        w = sim(data, seed, "a31_storm").world
        used = [rid for f in w.flights.values() for rid in (f.get("reserve_crew_ids") or [])]
        assert len(used) == len(set(used))
        assert all(w.crews[r]["reserve_status"] == "Assigned" for r in used)


def test_pilots_a_little_short_with_no_reserve_get_a_captain_and_airline_extension_once(data):
    seen = 0
    cap_h = None
    import yaml
    for path in (ROOT / "config" / "rules").glob("law.yaml"):
        cap_h = next(r["value"] for r in yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]
                     if r["id"] == "pilot_fdp_extension_before_takeoff_max_h")
    ext = load_config()["baseline"]["crew"]["pilot_extension_min"]
    assert 0 < ext <= 30 and ext <= cap_h * 60                            # inside the legal limit (14 CFR 117.19)
    for seed in range(1, 21):
        w = sim(data, seed, "random").world
        events = [e for e in w.events if e["type"] == "pilot_duty_extended"]
        crews = [e["payload"]["crew_id"] for e in events]
        assert len(crews) == len(set(crews))                              # once per crew
        assert all(e["payload"]["decision"] == "human" and e["payload"]["decided_by"] == "captain and crew scheduling"
                   and e["payload"]["decision_id"] == "D02" for e in events)
        seen += len(events)
    assert seen > 0


def test_flight_reserve_crews_column_is_saved(saved):
    res, _, cur = saved
    assert one(cur, "SELECT reserve_crew_ids FROM flights WHERE id='WN1033'") == ["R04"]
    assert one(cur, "SELECT count(*) FROM events WHERE type='crew_illegal_rest' AND run_id=%s", res.run_id) > 0
    assert one(cur, "SELECT count(*) FROM events WHERE type='crew_short_for_plan' AND run_id=%s", res.run_id) > 0


def test_a31_ramp_closes_inside_stop_then_reopens(data):
    w = sim(data, 7, "a31_storm").world
    assert ramp_open_at(w, hhmm("12:00")) is True
    assert ramp_open_at(w, hhmm("12:20")) is False
    assert ramp_open_at(w, hhmm("12:50")) is True


# ------------------------------------------------------------------ the gap between truth and screens

def test_screens_disagree_during_stop_and_agree_after(data):
    r = sim(data, 7, "a31_storm")
    ts = r.metrics["timeseries"]
    during = [n for t, n in zip(ts["t"], ts["pax_disagreeing"]) if hhmm("11:30") <= t <= hhmm("14:00")]
    assert max(during) > 0
    rows = count_disagreements(r.world.truth, r.channels.shown, r.channels.cause_of)
    assert all(row["cause"] == "message_failed" for row in rows)       # only lost messages are still wrong at the end
    assert r.metrics["minutes_until_all_channels_agree_after_stop"] is not None


def test_incremental_count_matches_full_scan(data):
    r = sim(data, 7, "a31_storm")
    rows = count_disagreements(r.world.truth, r.channels.shown, r.channels.cause_of)
    assert len(rows) == len(r.channels.dis)
    assert {x["passenger_id"] for x in rows} == set(r.channels.dis_pax)


def test_causes_are_the_labelled_ones(data):
    r = sim(data, 7, "a31_storm")
    assert set(r.metrics["wrong_field_minutes_by_cause"]) <= CAUSES


def test_channels_start_out_correct(data):
    """Before anything happens, every screen shows the truth (so all later gaps come from the disruption)."""
    r = simulate(7, "a31_storm", data=data, until=hhmm("11:00"))
    assert r.metrics["peak_passengers_disagreeing"] == 0
    for v in data["channel_views"]:
        assert tuple(v["shown_itinerary"]) == r.world.truth[v["passenger_id"]]["itinerary"]
        assert tuple(v["shown_bag_routing"]) == r.world.truth[v["passenger_id"]]["bag_routing"]


def test_a_copy_always_arrives_after_the_change(data):
    r = sim(data, 7, "a31_storm")
    copies = [e for e in r.world.events if e["type"] == "channel_view_updated"]
    assert copies and all(e["payload"]["lag_min"] >= 1 for e in copies)


# ------------------------------------------------------------------ rules and simulator agree on fuel

def _keys(node, out=None):
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(str(k))
            _keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _keys(v, out)
    return out


def _rule_ids_and_names():
    import yaml
    names = set()
    for path in (ROOT / "config" / "rules").glob("*.yaml"):
        for rule in yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]:
            names.add(rule["id"])
            names.add(rule["path"].replace("[]", "").split(".")[-1])
    return names


def test_simulator_yaml_does_not_redefine_numbers_that_the_rules_own():
    import yaml
    simulator = yaml.safe_load((ROOT / "config" / "simulator.yaml").read_text(encoding="utf-8"))
    shared = _rule_ids_and_names() & _keys(simulator)
    assert not shared, f"simulator.yaml defines keys that config/rules/ already owns: {sorted(shared)}"


def test_simulator_reads_its_legal_numbers_from_the_rules_folder():
    import yaml
    limits = {}
    for path in sorted((ROOT / "config" / "rules").glob("*.yaml")):
        for rule in yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]:
            if rule["path"].startswith("limits."):
                limits[rule["path"].split(".", 1)[1]] = rule["value"]
    assert load_config()["limits"] == limits and limits["fuel_buffer_min"] == 10


# ------------------------------------------------------------------ the simulator does not decide

def test_copilot_mode_not_built_yet():
    with pytest.raises(NotBuiltYet, match="not built yet"):
        run(7, "random", mode="copilot")


def test_simulator_never_touches_rules():
    for path in (ROOT / "simulator").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n.startswith("backend.rules") for n in names), path.name
    code = ("import simulator.run, simulator.report, sys; "
            "print(any(m.startswith('backend.rules') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# ------------------------------------------------------------------ saved to the database

@pytest.fixture(scope="module")
def saved():
    """Run the class story into the database, noting what the events looked like after each batch."""
    snaps = []

    def on_flush(conn):
        snaps.append(conn.execute("SELECT id, md5(payload::text || type) FROM events ORDER BY id").fetchall())

    res = run(7, "a31_storm", write=True, on_flush=on_flush)
    with connect() as conn, conn.cursor() as cur:
        yield res, snaps, cur


def test_events_only_ever_grow(saved):
    _, snaps, _ = saved
    assert len(snaps) > 3
    for before, after in zip(snaps, snaps[1:]):
        assert len(after) >= len(before)
        assert after[:len(before)] == before                  # nothing earlier changed


def test_events_cannot_be_changed_or_deleted(saved):
    _, _, cur = saved
    first = cur.execute("SELECT md5(payload::text) FROM events ORDER BY id LIMIT 1").fetchone()
    for sql in ("UPDATE events SET type='x'", "DELETE FROM events", "UPDATE audit_log SET what='x'"):
        with pytest.raises(Exception, match="append-only"):
            with connect() as c2:
                c2.execute(sql)
    assert cur.execute("SELECT md5(payload::text) FROM events ORDER BY id LIMIT 1").fetchone() == first
    assert one(cur, "SELECT count(*) FROM events WHERE actor <> 'simulator'") == 0


def test_run_saved_everything(saved):
    res, _, cur = saved
    assert one(cur, "SELECT count(*) FROM incidents WHERE run_id=%s AND status='resolved'", res.run_id) == 1
    assert one(cur, "SELECT count(*) FROM metric_runs WHERE run_id=%s AND mode='today'", res.run_id) == 1
    assert one(cur, "SELECT count(*) FROM audit_log WHERE actor='simulator'") > 100
    assert one(cur, "SELECT count(*) FROM mismatches") == 0              # detection is a later phase
    assert one(cur, "SELECT count(*) FROM events WHERE type='ground_stop_started' AND station='MCI'") == 1


def test_group_counts_still_add_up_after_rebooking(saved):
    _, _, cur = saved
    assert one(cur, "SELECT count(*) FROM bookings WHERE status='rebooked'") > 0
    assert one(cur, """SELECT count(*) FROM (SELECT f.id FROM flights f JOIN passenger_groups g ON g.flight_id=f.id
                      GROUP BY f.id, f.pax_total HAVING sum(g.count) <> f.pax_total) x""") == 0


def test_nobody_is_booked_on_a_cancelled_flight(saved):
    _, _, cur = saved
    assert one(cur, "SELECT count(*) FROM flights WHERE status='cancelled'") > 0
    assert one(cur, """SELECT count(*) FROM bookings b JOIN flights f ON f.id=b.flight_id
                      WHERE f.status='cancelled' AND b.status IN ('confirmed','rebooked')""") == 0


def test_truth_in_database_matches_the_run(saved):
    res, _, cur = saved
    db = dict(cur.execute("SELECT id, status FROM flights").fetchall())
    assert db == {f["id"]: f["status"] for f in res.world.flights.values()}
    assert one(cur, "SELECT ramp_open FROM stations WHERE code='MCI'") is True


def test_overnight_passengers_and_next_day_flights_are_saved(saved):
    res, _, cur = saved
    assert one(cur, "SELECT overnight FROM passengers WHERE id='PAX0001'") is True
    assert one(cur, "SELECT count(*) FROM passengers WHERE overnight") == res.metrics["tracked_passengers_overnight"]
    assert one(cur, "SELECT count(*) FROM flights f WHERE substr(f.id,1,3)='WN3'") == res.metrics["next_day_flights_created"] > 0
    assert one(cur, "SELECT count(*) FROM flights f WHERE substr(f.id,1,3)='WN3' AND sched_dep::date = DATE '2026-10-07'") \
        == res.metrics["next_day_flights_created"]
    assert one(cur, """SELECT count(*) FROM bookings b JOIN flights f ON f.id=b.flight_id
                      WHERE substr(f.id,1,3)='WN3' AND b.passenger_id='PAX0001'""") == 1
