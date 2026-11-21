"""Chaos testing: the generator, the sealing of the truth, the judge, and the crashed-run fix."""
import contextlib
import io
from datetime import datetime, timedelta

import pytest

from backend.agents.situation.state_at import parse_clock
from backend.contracts import AgentContext, ContractViolation
from backend.database import connect
from backend.settings import ROOT
from chaos.generator import overlay_events, plan_day, scenario_for, truth_for
from chaos.judge import judge


@pytest.fixture(scope="module")
def data():
    from backend.seed.build import build
    return build()


def play(seed, data):
    from simulator.run import simulate
    plan = plan_day(seed)
    res = simulate(seed, "chaos", data=data, prebuilt=scenario_for(plan), run_id=f"chaos-{seed}")
    over = overlay_events(plan, res.world.events, res.world.flights)
    return plan, res, over, truth_for(plan, res.world, over)


# ------------------------------------------------------------------ the generator

def test_same_chaos_seed_same_day(data):
    a, b = plan_day(17), plan_day(17)
    assert a.ingredients == b.ingredients and a.program == b.program and a.feed_outages == b.feed_outages
    _, r1, o1, t1 = play(5, data)
    _, r2, o2, t2 = play(5, data)
    assert [(e["t"], e["type"], e["payload"]) for e in r1.world.events] == [(e["t"], e["type"], e["payload"]) for e in r2.world.events]
    assert o1 == o2 and t1 == t2
    assert plan_day(17).ingredients != plan_day(18).ingredients or plan_day(17).program != plan_day(18).program


def test_the_truth_file_matches_the_events(data):
    seed = next(s for s in range(1, 60) if plan_day(s).program and plan_day(s).program["kind"] == "thunderstorm"
                and plan_day(s).program["end"] < 23 * 60)
    plan, res, over, truth = play(seed, data)
    ev = res.world.events
    inc = truth["incidents"][0]
    started = next(e for e in ev if e["type"] == "ground_stop_started")
    ended = next(e for e in ev if e["type"] == "ground_stop_ended")
    from simulator.clock import at
    assert at(started["t"]).isoformat() == inc["start"] and at(ended["t"]).isoformat() == inc["end"]
    assert inc["type"] == "weather" and inc["controllable"] is False
    assert len([e for e in ev if e["type"] in ("ground_stop_started", "ground_stop_extended")]) == inc["notices"]
    assert sorted(x[1] for x in truth["aftermath"]["cancelled"]) == sorted(e["payload"]["flight_id"] for e in ev if e["type"] == "flight_cancelled")


def test_quiet_days_have_no_incident(data):
    seed = next(s for s in range(1, 80) if plan_day(s).ingredients == ["quiet"])
    plan, res, over, truth = play(seed, data)
    assert truth["quiet"] and not truth["incidents"] and not truth["feed_outages"] and not truth["conflicts"]
    assert not [e for e in res.world.events if e["type"].startswith(("ground_", "ramp_")) or e["type"] == "flight_cancelled"]
    assert not over


def test_new_event_types_say_only_what_a_real_source_would(data):
    seen = {}
    for s in range(1, 60):
        p = plan_day(s)
        if p.program or p.second:
            for e in overlay_events(p, [], {}):
                seen.setdefault(e["type"], set()).update(e["payload"])
    allowed = {"airport_ops_notice": {"kind", "area", "expected_end"}, "police_tsa_notice": {"kind", "action", "area"},
               "system_status_changed": {"system", "status"}}
    for typ, keys in allowed.items():
        assert typ in seen and seen[typ] <= keys, (typ, seen.get(typ))
    for keys in seen.values():
        assert not keys & {"true_cause", "scenario", "controllable", "truth", "cause"}


# ------------------------------------------------------------------ the truth is sealed

def test_the_agent_cannot_reach_the_truth_file_or_the_chaos_config(db):
    for path in (ROOT / "backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "chaos" not in text.lower(), path
    ctx = AgentContext("situation", db, parse_clock("12:00"), run_id="t", source_run_id="chaos-1")
    for table in ("truth", "chaos", "metric_runs", "incidents", "incident_events"):
        with pytest.raises(ContractViolation):
            ctx.read(table)
    assert not [m for m in dir(ctx) if "file" in m.lower() or "path" in m.lower() or "open" == m]   # no way to open files
    from chaos.generator import TRUTH_DIR
    assert (ROOT / ".gitignore").read_text(encoding="utf-8").count("chaos/truth/") == 1 and TRUTH_DIR.name == "truth"


# ------------------------------------------------------------------ the judge catches planted mistakes

def _truth(**kw):
    base = {"run_id": "chaos-test", "chaos_seed": 0, "ingredients": ["thunderstorm"], "summary": "test", "quiet": False,
            "incidents": [{"n": 1, "kind": "thunderstorm", "type": "weather", "controllable": False, "start": "2026-10-06T12:00:00-05:00",
                           "end": "2026-10-06T13:00:00-05:00", "first_signal": "2026-10-06T12:00:00-05:00", "has_end": True}],
            "feed_outages": [], "conflicts": [], "security": [], "crew_cancellations_needing_review": [],
            "aftermath": {"cancelled": [], "stranded_overnight_first": None, "diverted": []}, "messy": {}, "normal_variation": []}
    base.update(kw)
    return base


def _v(n, clock, status="abnormal", typ="weather", conf=0.75, end=("13:00", "14:00"), ctrl=False):
    e0, e2 = (parse_clock(end[0]), parse_clock(end[1])) if end else (None, None)
    return {"version": n, "assessed_at": parse_clock(clock), "status": status, "type": typ, "likely_cause": typ, "confidence": conf,
            "incident_controllable": ctrl, "flight_controllability": [], "expected_end_earliest": e0, "expected_end_likely": e0,
            "expected_end_latest": e2, "end_confidence": 0.75 if end else None, "impact": {}, "conflicts": [], "stale_sources": [],
            "decision_ids": ["A01"]}


def _out(versions, recs=(), events=()):
    return {"agent_run_id": "x", "versions": list(versions), "recs": list(recs), "events": list(events),
            "timing": {"seconds": [["2026-10-06T12:00:00-05:00", 0.2]], "errors": []}}


def result(score, name):
    return next(c for c in score["checks"] if c["name"] == name)


def test_the_judge_passes_a_good_answer():
    s = judge(_truth(), _out([_v(1, "12:00"), _v(2, "13:00", status="recovery", end=None)]))
    for name in ("detection", "cause", "overconfidence", "expected end", "controllable"):
        assert result(s, name)["result"] == "PASS", (name, result(s, name))


def test_the_judge_fails_an_overconfident_wrong_cause():
    s = judge(_truth(), _out([_v(1, "12:00", typ="it_outage", conf=0.9, ctrl=True), _v(2, "12:30", typ="weather", conf=0.9)]))
    assert result(s, "overconfidence")["result"] == "FAIL" and "it_outage" in result(s, "overconfidence")["reason"]
    assert s["verdict"] == "FAIL"


def test_the_judge_fails_a_false_alarm():
    s = judge(_truth(quiet=True, incidents=[]), _out([_v(1, "09:00")]))
    assert result(s, "detection")["result"] == "FAIL" and "false alarm" in result(s, "detection")["reason"]


def test_the_judge_fails_a_guess_while_a_feed_is_silent():
    t = _truth(feed_outages=[{"feed": "faa_feed", "start": "2026-10-06T12:10:00-05:00", "end": "2026-10-06T12:40:00-05:00", "why": "x"}])
    v = [_v(1, "12:00"), _v(2, "12:13", status="cannot_assess: faa_feed stale since 12:10", typ=None, conf=None, end=None),
         _v(3, "12:20", typ="weather")]
    s = judge(t, _out(v))
    assert result(s, "stale feeds")["result"] == "FAIL" and "while faa_feed was silent" in result(s, "stale feeds")["reason"]
    late = judge(t, _out([_v(1, "12:00"), _v(2, "12:20", status="cannot_assess: faa_feed stale since 12:10", typ=None, conf=None, end=None)]))
    assert result(late, "stale feeds")["result"] == "FAIL"                   # noticed too late and never proposed S01


def test_the_judge_fails_a_missed_security_hand_over_and_human_clutter():
    t = _truth(incidents=[{"n": 1, "kind": "security", "type": "security", "controllable": False, "start": "2026-10-06T12:00:00-05:00",
                           "end": "2026-10-06T12:40:00-05:00", "first_signal": "2026-10-06T12:00:00-05:00", "has_end": False}],
               security=[{"at": "2026-10-06T12:00:00-05:00"}])
    recs = [{"id": i, "created_at": parse_clock("12:05"), "decision_ids": ["A02"], "supersedes": None, "autonomy": "needs_approval",
             "title": "x"} for i in range(3)]
    s = judge(t, _out([_v(1, "12:00", typ="security")], recs=recs))
    assert result(s, "security")["result"] == "FAIL" and result(s, "human clutter")["result"] == "FAIL"


# ------------------------------------------------------------------ Part 0: a crashed run doesn't break the agent

def test_a_crashed_run_then_the_agent_replay_has_no_duplicate_ids(db, data):
    from backend.agents.situation.run import replay
    from simulator.run import simulate
    from simulator.store import Writer
    flushes = []

    def crash(conn):
        flushes.append(1)
        if len(flushes) == 3:
            raise RuntimeError("the run crashed halfway")

    with connect() as conn:
        with pytest.raises(RuntimeError, match="crashed"):
            simulate(7, "a31_storm", data=data, writer=Writer(conn, "crash-test", 7), on_flush=crash, run_id="crash-test")
    with connect() as conn:
        conn.autocommit = True
        r = replay(conn, "crash-test", until=parse_clock("07:00"))
        assert not r["errors"]
        conn.execute("INSERT INTO events (occurred_at, type, actor, actor_name, payload) VALUES (now(), 'probe', 'test', 'test', '{}')")
        from simulator.report import collect
        with pytest.raises(SystemExit, match="did not finish"):
            collect(conn, "crash-test")
