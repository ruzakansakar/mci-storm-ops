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


def test_no_diverted_flight_reaches_its_alternate_below_the_fuel_floor():
    from simulator.config import load_config
    floor = load_config()["physics"]["diversion"]["fuel_floor_min"]
    seen = 0
    for seed in range(1, 21):
        for scenario in ("random", "a31_storm"):
            for e in simulate(seed, scenario).world.events:
                if e["type"] == "flight_diverted":
                    seen += 1
                    p = e["payload"]
                    assert p["fuel_minutes_extra"] - p["extra_flying_min"] >= floor, (seed, scenario, p)
    assert seen > 20                                          # the check really looked at diversions


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

def test_fuel_floor_in_simulator_equals_fuel_buffer_in_rules():
    import yaml
    rules = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))
    simulator = yaml.safe_load((ROOT / "config" / "simulator.yaml").read_text(encoding="utf-8"))
    assert simulator["physics"]["diversion"]["fuel_floor_min"] == rules["limits"]["fuel_buffer_min"]


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
    """Run a random day (seed 4: a storm with many cancellations) into the database, noting events after each batch."""
    snaps = []

    def on_flush(conn):
        snaps.append(conn.execute("SELECT id, md5(payload::text || type) FROM events ORDER BY id").fetchall())

    res = run(4, "random", write=True, on_flush=on_flush)
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
