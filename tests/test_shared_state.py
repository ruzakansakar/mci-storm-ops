"""Phase 4c: shared state between agents (Situation -> Communication) and the rules in config/agents/_shared.yaml."""
import importlib
import re
from datetime import timedelta
from pathlib import Path

import pytest

from backend.agents.communication.detect import Memory, load_settings, scan
from backend.agents.communication.fix import FixMemory, propose_fixes
from backend.agents.communication.situation_view import SituationView, read_situation
from backend.agents.communication.state_at import clock
from backend.contracts import load_contracts
from backend.contracts.buffered import BufferedContext
from backend.contracts.context import ContractViolation
from backend.contracts.decisions import load_shared
from backend.contracts.loader import ContractError, validate_contract

ROOT = Path(__file__).resolve().parents[1]
S = load_settings()


def version(n, at, conflicts=(), status="abnormal", stale=()):
    return {"version": n, "assessed_at": at, "status": status, "type": "weather", "likely_cause": "thunderstorm",
            "confidence": 0.8, "expected_end_earliest": at, "expected_end_likely": at + timedelta(hours=1),
            "expected_end_latest": at + timedelta(hours=2), "end_confidence": 0.6, "conflicts": list(conflicts),
            "stale_sources": list(stale), "data_as_of": at, "flight_controllability": [{"flight_id": "F1", "controllable": False}],
            "evidence": [{"event_id": 1}], "summary": "x", "impact": {}, "decision_ids": ["A01"], "agent_run_id": "situation"}


def two_flights(now="12:00"):
    """P1 on F1 and P2 on F2; both trips are cancelled at 11:50 and every screen still shows 'confirmed'."""
    sched = [{"id": f, "direction": "departure", "origin": "MCI", "destination": "BWI", "sched_dep": clock("12:30"),
              "sched_arr": clock("14:00")} for f in ("F1", "F2")]
    pax = [{"id": p, "segment": "standard", "channel_types": ["app", "sms"]} for p in ("P1", "P2")]
    ctx = BufferedContext(clock(now), sched, pax)
    events, i = [], 0
    for pid, fid in (("P1", "F1"), ("P2", "F2")):
        trip = {"passenger_id": pid, "itinerary": [fid], "status": "confirmed", "destination": "BWI", "bag_routing": []}
        for at, status in (("05:00", "confirmed"), ("11:50", "cancelled")):
            i += 1
            events.append({"id": i, "occurred_at": clock(at), "type": "trip_record_updated", "payload": {**trip, "status": status}})
    ctx.events = events
    return ctx


def step(ctx, now, view, fixes, memory):
    ctx.now = clock(now)
    result = scan(ctx, memory, write=False)
    read_situation(ctx, view, S)
    return propose_fixes(ctx, result, fixes, view, S)


# ------------------------------------------------------------------ the rules exist and name real code

def test_seven_shared_state_rules_each_name_a_real_function():
    rules = load_shared()["shared_state"]
    assert [r["id"][:3] for r in rules] == ["SS1", "SS2", "SS3", "SS4", "SS5", "SS6", "SS7"]
    for r in rules:
        module, name = r["checked_by"].split(":")
        obj = importlib.import_module(module)
        for part in name.split("."):
            obj = getattr(obj, part)
        assert obj is not None and r["rule"] and r["how"]


def test_agents_never_import_each_other():
    agents = ROOT / "backend" / "agents"
    for d in [p for p in agents.iterdir() if p.is_dir() and not p.name.startswith("_")]:
        for f in d.glob("*.py"):
            for other in [p.name for p in agents.iterdir() if p.is_dir() and p.name != d.name and not p.name.startswith("_")]:
                assert not re.search(rf"backend\.agents\.{other}\b", f.read_text(encoding="utf-8")), (f, other)


# ------------------------------------------------------------------ SS3, SS4, SS5

def test_communication_only_sees_versions_up_to_now_even_within_the_minute():
    ctx = two_flights()
    recs = ctx.store.records["situation_assessments"]
    recs += [version(1, clock("11:55")), version(2, clock("12:00") + timedelta(seconds=30)), version(3, clock("12:05"))]
    assert ctx.read_shared("situation_assessments")["version"] == 1                 # 12:00:00 < 12:00:30
    ctx.now = clock("12:00") + timedelta(seconds=30)
    assert ctx.read_shared("situation_assessments")["version"] == 2
    ctx.now = clock("12:04")
    assert ctx.read_shared("situation_assessments")["version"] == 2
    ctx.now = clock("11:00")
    assert ctx.read_shared("situation_assessments") is None


def test_only_whitelisted_fields_and_suspicions_stay_suspicions():
    ctx = two_flights()
    ctx.store.records["situation_assessments"].append(version(1, clock("11:55"), conflicts=[
        {"key": "departed-during-stop:F1", "flight_ids": ["F1"], "fields": ["status"], "what": "secret detail",
         "values": [{"source": "flight_ops", "event_id": 9}]}]))
    view = ctx.read_shared("situation_assessments")
    text = repr(view)
    assert "flight_controllability" not in text and "evidence" not in text and "secret detail" not in text and "event_id" not in text
    assert view["facts"]["conflicts"] == [{"key": "departed-during-stop:F1", "flight_ids": ["F1"], "fields": ["status"]}]
    assert view["suspected"]["likely_cause"] == {"value": "thunderstorm", "confidence": 0.8, "suspected": True}
    assert view["suspected"]["expected_end_likely"]["confidence"] == 0.6
    assert "likely_cause" not in view["facts"] and "expected_end_likely" not in view["facts"]
    for field in ("flight_controllability", "evidence", "summary"):
        with pytest.raises(ContractViolation, match="shared_reads"):
            ctx.read_shared("situation_assessments", fields=["version", field])
    # every read is in the audit log with the version it read
    assert [a for a in ctx.audit if a["what"] == "read shared situation_assessments" and a["after"]["version"] == 1]


def test_a_contract_can_never_list_compensation_or_evidence_fields():
    for field in ("flight_controllability", "evidence"):
        data = dict(load_contracts()["communication"].data)
        data["shared_reads"] = {"situation_assessments": {"producer": "situation", "fields": ["version", field]}}
        with pytest.raises(ContractError, match=field):
            validate_contract(data)
    data = dict(load_contracts()["communication"].data)
    data["shared_reads"] = {"situation_assessments": {"producer": "communication", "fields": ["version"]}}
    with pytest.raises(ContractError, match="produced by situation"):
        validate_contract(data)


def test_agents_that_read_nothing_shared_are_refused():
    ctx = BufferedContext(clock("12:00"), [], [], agent="situation", rows={"flights": [], "system_health": []})
    with pytest.raises(ContractViolation):
        ctx.read_shared("situation_assessments")


# ------------------------------------------------------------------ a. conflict holds

def test_conflict_holds_only_that_flight_and_resumes_when_situation_clears_it():
    ctx, view, fixes, memory = two_flights("11:51"), SituationView(), FixMemory(), Memory()
    hb = ctx.store.heartbeats.setdefault("situation", [])
    recs = ctx.store.records["situation_assessments"]
    hb.append(clock("11:50"))
    recs.append(version(1, clock("11:50"), conflicts=[{"key": "departed-during-stop:F1", "flight_ids": ["F1"], "fields": ["status"]}]))
    out = step(ctx, "11:51", view, fixes, memory)
    assert {p["passenger_id"] for p in out} == {"P2"}                    # the other flight keeps getting fixed
    held = [p for k, p in ctx.outputs if k == "channel_fix_held" and p["passenger_id"] == "P1"]
    assert held and held[0]["reason"] == "held: Situation reports conflicting data on F1 since 11:51"
    assert held[0]["kind"] == "situation_conflict" and held[0]["situation_version"] == 1
    assert all(p["situation_version"] == 1 for p in out)                 # each decision says which version it used
    hb.append(clock("11:55"))
    assert not [p for p in step(ctx, "11:55", view, fixes, memory) if p["passenger_id"] == "P1"]   # still held
    recs.append(version(2, clock("12:00")))                              # Situation's next version clears it
    hb.append(clock("12:00"))
    out = step(ctx, "12:00", view, fixes, memory)
    assert {p["passenger_id"] for p in out if p["action"] == "fix_channel_mismatch"} >= {"P1"}
    cleared = [p for k, p in ctx.outputs if k == "conflict_hold_cleared"]
    assert cleared and cleared[-1]["flight_id"] == "F1" and cleared[-1]["version"] == 2


# ------------------------------------------------------------------ b. Situation stops running

def test_situation_heartbeat_stops_logged_fixes_continue_escalated_after_m_minutes():
    ctx, view, fixes, memory = two_flights("11:51"), SituationView(), FixMemory(), Memory()
    ctx.store.records["situation_assessments"].append(version(1, clock("11:50")))
    ctx.store.heartbeats["situation"] = [clock("11:50")]                 # ... and then it never runs again
    n = S["situation"]["unavailable_after_min"]
    m = S["situation"]["escalate_after_min"]
    step(ctx, "11:51", view, fixes, memory)
    assert view.available
    t = (clock("11:50") + timedelta(minutes=n + 1)).strftime("%H:%M")
    ctx.outputs.clear()
    step(ctx, t, view, fixes, memory)
    assert [p for k, p in ctx.outputs if k == "situation_unavailable"]
    assert view.picture["version"] == 1                                   # the last real picture is kept, nothing invented
    # the trips are still wrong: fixes carry on (after the anti-spam wait), and the supervisor hears after M minutes
    t2 = (clock("11:50") + timedelta(minutes=m + 1)).strftime("%H:%M")
    out = step(ctx, t2, view, fixes, memory)
    assert out, "fixes continue without Situation"
    esc = [r for (tab, _), r in ctx.saved.items() if tab == "recommendations" and r["title"].startswith("The Situation agent has not run")]
    assert len(esc) == 1
    ctx.store.heartbeats["situation"].append(clock(t2))                   # it is back
    step(ctx, t2, view, fixes, memory)
    assert [p for k, p in ctx.outputs if k == "situation_available_again"]


# ------------------------------------------------------------------ c. cannot assess, and SS7 disagreements

def test_cannot_assess_is_recorded_without_changing_fixes():
    ctx, view, fixes, memory = two_flights("11:51"), SituationView(), FixMemory(), Memory()
    ctx.store.records["situation_assessments"].append(version(1, clock("11:50"), status="cannot_assess: faa_feed stale since 11:40"))
    ctx.store.heartbeats["situation"] = [clock("11:50")]
    out = step(ctx, "11:51", view, fixes, memory)
    assert view.cannot_assess.startswith("cannot_assess") and {p["passenger_id"] for p in out} == {"P1", "P2"}
    assert [p for k, p in ctx.outputs if k == "situation_cannot_assess"]


def test_a_lasting_disagreement_logs_both_views_and_escalates():
    ctx, view, fixes, memory = two_flights("11:51"), SituationView(), FixMemory(), Memory()
    ctx.store.records["situation_assessments"].append(version(1, clock("11:50"), stale=[{"source": "flight_ops", "since": "11:40"}]))
    hb = ctx.store.heartbeats["situation"] = [clock("11:50")]
    for minute in range(0, S["situation"]["disagree_after_min"] + 2):
        now = clock("11:51") + timedelta(minutes=minute)
        hb.append(now)
        step(ctx, now.strftime("%H:%M"), view, fixes, memory)
    logged = [p for k, p in ctx.outputs if k == "agents_disagree"]
    assert len(logged) == 1 and logged[0]["reader_view"]["flight_ops_stale"] is False
    assert logged[0]["producer_view"]["flight_ops_stale"] is True
    assert [r for (tab, _), r in ctx.saved.items() if tab == "recommendations" and "disagree" in r["title"]]


# ------------------------------------------------------------------ in the minute loop (seed 7, in memory)

@pytest.fixture(scope="module")
def data():
    from backend.seed.build import build
    return build()


def test_copilot_runs_situation_first_and_communication_reads_it(data):
    from simulator.run import simulate
    r = simulate(7, "a31_storm", mode="copilot", data=data)
    sit = r.metrics["copilot_situation"]
    assert sit["situation_versions"] >= 15 and sit["situation_runs"] > 200 and sit["situation_errors"] == 0
    assert r.metrics["contract_violations"] == 0
    fixes = [e["payload"]["proposal"] for e in r.world.events if e["type"] == "channel_fix_applied"]
    assert fixes and all(p.get("situation_version") for p in fixes if p["data_as_of"] >= "2026-10-06T05:00")
    reads = [a for a in r.world.audit_rows if a["actor_name"] == "communication" and a["what"] == "read shared situation_assessments"]
    assert len(reads) >= 1000                                             # every minute, each with the version it read


def test_situation_stops_in_the_loop_fixes_continue_and_the_supervisor_is_told(data):
    from simulator.run import simulate
    from simulator.clock import at, hhmm
    r = simulate(7, "a31_storm", mode="copilot", data=data, copilot_options={"situation_until": at(hhmm("12:00"))})
    ev = [e for e in r.world.events if e["actor"] == "agent"]
    down = [e for e in ev if e["type"] == "situation_unavailable"]
    assert len(down) == 1 and down[0]["t"] == hhmm("12:00") + S["situation"]["unavailable_after_min"] + 1
    assert [e for e in ev if e["type"] == "channel_fix_applied" and e["t"] > hhmm("12:30")]
    assert not [e for e in ev if e["actor_name"] == "situation" and e["t"] > hhmm("12:00")]


# ------------------------------------------------------------------ Part 2: same versions as the standalone replay (database)

def _comparable(v):
    """A version without the ids the database assigns itself (row id, run ids, event ids inside evidence/conflicts). Times are
    compared as instants: the database hands them back in UTC, the minute loop keeps Chicago time (same moment, other spelling)."""
    from datetime import datetime

    def strip(x):
        if isinstance(x, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?([+-]\d\d:\d\d)", x):
            return datetime.fromisoformat(x)
        if isinstance(x, dict):
            return {k: strip(y) for k, y in x.items() if k not in ("event_id", "event_ids", "assessment_id")}
        if isinstance(x, list):
            return [strip(y) for y in x]
        return float(x) if hasattr(x, "as_tuple") else x
    return strip({k: val for k, val in v.items() if k not in ("id", "agent_run_id", "run_id", "created_at")})


def test_situation_versions_in_copilot_equal_its_standalone_replay_and_db_reads_work(data):
    import contextlib
    import io
    from backend.agents.situation.run import replay
    from backend.contracts.context import AgentContext
    from backend.database import connect
    from scripts.reset_db import main as reset_db
    from simulator.run import run, simulate
    try:
        run(7, "a31_storm")
        with connect() as conn:
            conn.autocommit = True
            res = replay(conn, "a31_storm-s7-today")
            cols = [c for c in version(1, clock("05:00")) if c != "agent_run_id"] + ["incident_controllable"]
            rows = conn.execute(f"SELECT {', '.join(cols)} FROM situation_assessments WHERE agent_run_id=%s ORDER BY version",
                                (res["agent_run_id"],)).fetchall()
            standalone = [dict(zip(cols, r)) for r in rows]
            beats = conn.execute("SELECT count(*) FROM events WHERE run_id=%s AND type='agent_heartbeat'",
                                 (res["agent_run_id"],)).fetchone()[0]
            # the database path of the shared read: the latest version <= T, and the heartbeat
            with conn.cursor() as cur:
                ctx = AgentContext("communication", cur, clock("14:58"), run_id="comm-shared-test", source_run_id="a31_storm-s7-today")
                pic = ctx.read_shared("situation_assessments")
                assert pic["version"] == max(v["version"] for v in standalone if v["assessed_at"] <= clock("14:58"))
                assert ctx.producer_heartbeat("situation") == clock("14:55")
                assert "flight_controllability" not in repr(pic)
        co = simulate(7, "a31_storm", mode="copilot", data=data)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            reset_db()
    copilot_versions = [{c: v[c] for c in cols} for v in co.copilot.store.records["situation_assessments"]]
    assert [_comparable(v) for v in copilot_versions] == [_comparable(v) for v in standalone]
    assert co.metrics["copilot_situation"]["situation_runs"] == beats == res["assessments"]



def test_today_mode_is_byte_for_byte_unchanged_by_the_copilot_and_shared_state(data):
    """Today mode runs no agent. Its complete event stream and audit trail are locked here. Last deliberate change: Phase 4c
    part 0 (app/SMS screens only for passengers who have them, Maya gets app + SMS); the world truth was identical before it."""
    import hashlib
    import json
    from simulator.run import simulate
    r = simulate(7, "a31_storm", data=data)
    assert r.copilot is None and not [e for e in r.world.events if e["actor"] == "agent"]
    h = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()
    assert h(r.world.events) == "6d0956db5f0cda0ddfbca66ae2cb26435d22bf29815ad32d300af77608fda019"
    assert h(r.world.audit_rows) == "c9ca769bb68dea8c14d0eec3f179046d13328a35349a7659a3cc36cf9ca8f9e4"
