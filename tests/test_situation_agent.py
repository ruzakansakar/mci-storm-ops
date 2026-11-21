"""Phase 4a: the Situation agent. Only enough to prove it runs and that the three protections hold."""
import ast
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from backend.agents.situation.run import at, replay
from backend.agents.situation.state_at import parse_clock
from backend.contracts import AgentContext, ContractViolation
from backend.contracts.feeds import Feeds
from backend.database import connect
from backend.settings import ROOT

RUN = "a31_storm-s7-today"


@pytest.fixture(scope="module")
def conn():
    from simulator.run import run
    run(7, "a31_storm", write=True)               # the class story, in the database
    with connect() as c:
        c.autocommit = True
        yield c


@pytest.fixture(scope="module")
def full(conn):
    return replay(conn, RUN)


def copy_run(conn, new_run, until=None, extra_sql=None):
    q = ("INSERT INTO events (occurred_at, type, actor, actor_name, station, payload, seed, run_id) "
         "SELECT occurred_at, type, actor, actor_name, station, payload, seed, %s FROM events WHERE run_id = %s")
    args = [new_run, RUN]
    if until is not None:
        q += " AND occurred_at <= %s"
        args.append(until)
    conn.execute(q, args)


def normal_form(v):
    """The assessment without event ids (a copied run has new ids for the same events)."""
    if isinstance(v, dict):
        return {k: normal_form(x) for k, x in v.items() if k not in ("event_id", "_state")}
    if isinstance(v, list):
        return [normal_form(x) for x in v]
    return v


# ------------------------------------------------------------------ 1. no answer key

def test_the_agent_answers_from_the_events_not_from_the_simulators_answer_key(conn):
    copy_run(conn, "answer-key-test")
    conn.execute("INSERT INTO incidents (kind, station, title, likely_cause, controllable, status, opened_at, opened_by, run_id) "
                 "VALUES ('it_outage', 'MCI', 'Southwest systems outage', 'Airline IT outage', true, 'open', %s, 'simulator', "
                 "'answer-key-test')", (parse_clock("11:30"),))
    # a planted field no real source would publish
    conn.execute("INSERT INTO events (occurred_at, type, actor, actor_name, station, payload, run_id) VALUES "
                 "(%s, 'ground_stop_extended', 'simulator', 'scenario', 'MCI', %s, 'answer-key-test')",
                 (parse_clock("12:05"), Jsonb({"program": "ground_stop", "expected_end": 780, "reason": "thunderstorms",
                                               "true_cause": "it_outage", "scenario": "it_outage_day"})))
    a = at(conn, "answer-key-test", parse_clock("12:20"))
    assert a["type"] == "weather" and a["incident_controllable"] is False
    with conn.cursor() as cur:
        ctx = AgentContext("situation", cur, parse_clock("12:20"), run_id="t", source_run_id="answer-key-test")
        events = ctx.read_events()
        assert events and all("true_cause" not in e["payload"] and "scenario" not in e["payload"] for e in events)
        assert all(set(e) <= {"id", "occurred_at", "type", "station", "payload", "feed"} for e in events)    # no run id, seed, actor
        for table in ("incidents", "metric_runs", "passengers", "crews", "bookings", "incident_events"):
            with pytest.raises(ContractViolation):
                ctx.read(table)
        for col in ("run_id", "seed", "actor", "actor_name"):
            with pytest.raises(ContractViolation):
                ctx.read("events", ["id", col])
        for col in ("status", "est_dep", "pax_total", "diverted_to"):              # end-of-day truth is not readable
            with pytest.raises(ContractViolation):
                ctx.read("flights", ["id", col])


def test_the_agent_code_never_touches_the_simulator():
    for path in (ROOT / "backend" / "agents").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "simulator.yaml" not in text and "metric_runs" not in text, path
        for node in ast.walk(ast.parse(text)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.startswith("simulator") for n in names), path


# ------------------------------------------------------------------ 2. no future knowledge

@pytest.mark.parametrize("clock", ["11:35", "12:20", "13:05", "14:30", "16:00"])
def test_nothing_the_agent_says_depends_on_a_later_event(conn, clock):
    t = parse_clock(clock)
    cut = f"future-test-{clock.replace(':', '')}"
    copy_run(conn, cut, until=t)
    full_day, cut_day = at(conn, RUN, t), at(conn, cut, t)
    assert normal_form(full_day) == normal_form(cut_day)
    with conn.cursor() as cur:
        ctx = AgentContext("situation", cur, t, run_id="t", source_run_id=RUN)
        assert all(e["occurred_at"] <= t for e in ctx.read_events())


# ------------------------------------------------------------------ 3. freshness that means something

def test_a_quiet_period_is_not_stale(conn):
    a = at(conn, RUN, parse_clock("08:00"))          # nothing special happens: all feeds have heartbeats
    assert a["status"] == "normal" and not a["stale_sources"]


def test_a_stopped_feed_means_cannot_assess_flag_a08_and_propose_s01_after_5_minutes(conn):
    stop = Feeds({"faa_feed": parse_clock("12:00")})
    early = at(conn, RUN, parse_clock("12:01"), stop)
    assert early["status"] == "normal" or not early["status"].startswith("cannot_assess")      # 60 s old: still fresh
    a = at(conn, RUN, parse_clock("12:03"), stop)
    assert a["status"] == "cannot_assess: faa_feed stale since 12:00" and a["decision_ids"] == ["A08"]
    assert a["type"] is None and a["end_likely"] is None                                      # no guessing
    r = replay(conn, RUN, feeds=Feeds({"faa_feed": parse_clock("12:00"), "flight_ops": parse_clock("12:00")}),
               until=parse_clock("12:20"))
    props = [h for h in r["memory"].history if h["kind"] == "proposal"]
    assert any(p["decision"] == "S01" and "faa_feed" in p["title"] for p in props)
    assert any(p["decision"] == "A09" for p in props)                # system health says up, but flight_ops stopped
    first_s01 = min(p["at"] for p in props if p["decision"] == "S01")
    assert first_s01 - parse_clock("12:00") > timedelta(minutes=5)
    rows = conn.execute("SELECT status, decision_ids FROM situation_assessments WHERE agent_run_id=%s ORDER BY version",
                        (r["agent_run_id"],)).fetchall()
    assert any(s.startswith("cannot_assess") and "A08" in d for s, d in rows)


# ------------------------------------------------------------------ the whole day runs

def test_the_seed_7_replay_runs_end_to_end(conn, full):
    assert not full["errors"]
    rows = conn.execute("SELECT version, status, type, flight_controllability, decision_ids, summary FROM situation_assessments "
                        "WHERE agent_run_id=%s ORDER BY version", (full["agent_run_id"],)).fetchall()
    assert any(s == "abnormal" and t == "weather" for _, s, t, _, _, _ in rows)
    last_abnormal = [r for r in rows if r[1] in ("abnormal", "recovery")][-1]
    assert rows[-1][1] == "recovery"                                  # operations are back but the aftermath remains
    fc = {f["flight_id"]: f["proposed"] for f in last_abnormal[3]}
    assert fc["WN1033"] == "needs_human_review"
    assert [fc[f] for f in ("WN1032", "WN1035", "WN1038", "WN1040")] == ["uncontrollable"] * 4
    assert all(r[4] and r[5] for r in rows)                          # every version names its decisions and has a summary
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE situation_assessments SET summary='x' WHERE agent_run_id=%s", (full["agent_run_id"],))


def test_proposals_wait_for_the_rule_check_and_go_to_the_catalog_roles(conn, full):
    rows = conn.execute("SELECT decision_ids, approver_role, status, rule_check_ok FROM recommendations WHERE run_id=%s",
                        (full["agent_run_id"],)).fetchall()
    assert rows and all(s == "pending" and ok is None for _, _, s, ok in rows)
    roles = {d[0]: r for d, r, _, _ in rows}
    assert roles["A02"] == "network_operations_control" and roles["A03"] == "customer_service_supervisor"
    assert roles["A06"] == "network_operations_control" and roles["P02"] == "network_operations_control"


def test_two_replays_never_mix(conn, full):
    other = replay(conn, RUN, until=parse_clock("11:40"))
    assert other["agent_run_id"] != full["agent_run_id"]
    n = conn.execute("SELECT count(*) FROM situation_assessments WHERE agent_run_id=%s", (other["agent_run_id"],)).fetchone()[0]
    assert n == sum(1 for h in other["memory"].history if h["kind"] == "version")


def test_a_failure_mid_assessment_rolls_back_and_writes_one_agent_error(conn):
    t = parse_clock("11:30")
    r = replay(conn, RUN, until=parse_clock("11:35"), fail_at=t)
    assert len(r["errors"]) == 1
    errs = conn.execute("SELECT count(*) FROM events WHERE run_id=%s AND type='agent_error'", (r["agent_run_id"],)).fetchone()[0]
    assert errs == 1
    at_t = conn.execute("SELECT count(*) FROM situation_assessments WHERE agent_run_id=%s AND assessed_at=%s",
                        (r["agent_run_id"], t)).fetchone()[0]
    assert at_t == 0                                                  # no partial assessment
