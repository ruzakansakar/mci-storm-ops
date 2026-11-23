"""The Communication agent, phase 4b-1 (detect channel mismatches): its four protections, Maya's day, and the scan itself.

Most tests play seed 7 in memory (chaos/judge_communication.py: the same events the database holds, through a context that makes
the same cuts as AgentContext). The database tests check the protections where they are really enforced: in AgentContext.
"""
import ast
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from backend.agents.communication.detect import Memory, load_settings, scan
from backend.agents.communication.state_at import clock
from backend.contracts import AgentContext, ContractViolation, load_contracts
from backend.contracts.feeds import Feeds
from backend.settings import ROOT
from chaos.judge_communication import MemoryContext, judge, play, replay

T = clock


@pytest.fixture(scope="module")
def data():
    from backend.seed.build import build
    return build()


@pytest.fixture(scope="module")
def day7(data):
    return play(7, data, chaos=False)


@pytest.fixture(scope="module")
def replay7(day7):
    return replay(day7)


def scan_at(day, clock_, feeds=None):
    return scan(MemoryContext(day, T(clock_), feeds or Feeds(), {"mismatches": {}, "events": [], "recs": [], "forbidden_reads": []}),
                Memory(), write=False, full=True)


def rows_for(sink, pid):
    return sorted((r for r in sink["mismatches"].values() if r["passenger_id"] == pid), key=lambda r: (r["since"], r["channel"], r["field"]))


# ------------------------------------------------------------------ protection 1: no answer key

def test_the_agent_never_sees_why_a_channel_was_late_even_if_a_cause_is_planted(db):
    run = "comm-test-planted"
    db.execute("INSERT INTO events (occurred_at, type, actor, actor_name, payload, run_id) VALUES (%s, 'channel_view_updated', "
               "'simulator', 'channel:sms', %s, %s)",
               (T("10:00"), Jsonb({"passenger_id": "PAX0001", "channel": "sms", "kind": "main", "lag_min": 37, "cause_if_late": "stale_copy",
                                   "true_cause": "PLANTED", "shown_itinerary": ["WN2031"], "shown_status": "delayed",
                                   "shown_destination": "BWI", "shown_bag_routing": [], "message": "Flight WN2031 ..."}), run))
    db.execute("INSERT INTO events (occurred_at, type, actor, actor_name, payload, run_id) VALUES (%s, 'channel_update_failed', "
               "'simulator', 'channel:app', %s, %s)",
               (T("10:01"), Jsonb({"passenger_id": "PAX0001", "channel": "app", "kind": "main", "queued_at": 590,
                                   "lost_message": "secret text", "why": "PLANTED"}), run))
    ctx = AgentContext("communication", db, T("10:05"), run_id="comm-test", source_run_id=run)
    seen = ctx.read_events()
    assert len(seen) == 2
    for e in seen:
        for bad in ("lag_min", "cause_if_late", "true_cause", "message", "lost_message", "why"):
            assert bad not in e["payload"], bad
    assert "PLANTED" not in str(seen) and "stale_copy" not in str(seen) and "secret" not in str(seen)


def test_the_agent_code_never_touches_the_simulator_or_its_settings():
    for path in (ROOT / "backend" / "agents" / "communication").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        mods = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + \
               [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in mods if m and (m.startswith("simulator") or m.startswith("chaos"))], path
        import re
        for word in ("simulator.yaml", "cause_if_late", "lag_min", "fail_chance"):        # the simulator's settings and reasons
            assert not re.search(rf"(?<![\w.]){re.escape(word)}(?!\w)", text), (path, word)


# ------------------------------------------------------------------ protection 2: no future

@pytest.mark.parametrize("clock_", ["11:40", "14:58", "15:25"])
def test_what_it_says_at_t_is_the_same_with_every_later_event_removed(day7, clock_):
    cut = {**day7, "events": [e for e in day7["events"] if e["occurred_at"] <= T(clock_)]}
    a, b = scan_at(day7, clock_), scan_at(cut, clock_)
    strip = lambda r: [{k: v for k, v in x.items() if k != "sources"} for x in r["findings"]]   # noqa: E731
    assert strip(a) == strip(b) and a["findings"]


# ------------------------------------------------------------------ protection 3: freshness

def test_no_mismatch_is_called_on_a_stale_channel_or_when_the_truth_feed_is_stale(day7):
    sms = Feeds(outages={"sms": [(T("14:40"), T("15:30"))]})
    r = scan_at(day7, "15:00", sms)
    assert not [x for x in r["findings"] if x["channel"] == "sms" and x["since"] >= T("14:40").isoformat()]
    assert any(c.startswith("sms stale since 14:40") for c in r["cannot_check"])
    assert [x for x in r["findings"] if x["channel"] == "app"]                   # the other channels are still checked
    truth = Feeds(outages={"reservations": [(T("14:50"), T("15:30"))]})
    r2 = scan_at(day7, "15:00", truth)
    assert not [x for x in r2["findings"] if x["since"] >= T("14:50").isoformat()]
    assert any(c.startswith("reservations stale since 14:50") for c in r2["cannot_check"])


def test_a_quiet_channel_is_not_stale(day7):
    assert not scan_at(day7, "08:00")["cannot_check"]                            # nothing changed for hours: heartbeats are fresh


def test_over_the_day_no_mismatch_is_called_on_a_silent_feed(day7):
    feeds = Feeds(outages={"gate_screen": [(T("14:00"), T("14:45"))]})
    sc = judge(day7, replay(day7, feeds), feeds)
    assert sc["stale_calls"] == 0 and sc["recall"] == 1.0 and sc["precision"] == 1.0


# ------------------------------------------------------------------ protection 4: privacy

def test_no_names_contacts_or_booking_refs_are_reachable_in_4b1(db):
    c = load_contracts()["communication"]
    assert set(c.reads["passengers"]) == {"id", "segment"} and "channel_views" not in c.reads and "bookings" not in c.reads
    ctx = AgentContext("communication", db, T("12:00"), run_id="comm-test")
    ctx.working_on(["PAX0001"])
    for col in ("full_name", "booking_ref", "contact_channels"):
        with pytest.raises(ContractViolation):
            ctx.read("passengers", ["id", col], where={"id": "PAX0001"})
    with pytest.raises(ContractViolation):
        ctx.read("channel_views", ["message"])
    assert ctx.read("passengers")[0].keys() == {"id", "segment"}


def test_a_whole_day_reads_no_name_or_contact(replay7):
    assert replay7["forbidden_reads"] == []


def test_the_segment_never_appears_in_any_text(replay7):
    text = str([r.get("shown_value") for r in replay7["mismatches"].values()]) + str(replay7["events"]) + str(replay7["recs"])
    for word in ("wheelchair", "child_alone", "child alone"):
        assert word not in text


# ------------------------------------------------------------------ Maya's day

def test_maya_cancellation_at_14_55_is_detected_on_all_four_channels_at_once_and_clears_when_each_catches_up(replay7):
    mine = rows_for(replay7, "PAX0001")
    at_1455 = [r for r in mine if r["since"] == T("14:55") and r["field"] == "itinerary"]
    assert sorted(r["channel"] for r in at_1455) == ["app", "call_center", "gate_screen", "sms"]
    assert all(r["detected_at"] == T("14:55") and "critical" in r["severities"] for r in at_1455)
    cleared = {r["channel"]: r["cleared_at"] for r in at_1455}
    assert cleared == {"app": T("15:12"), "sms": T("15:14"), "gate_screen": T("15:20"), "call_center": T("15:32")}


def test_maya_call_center_stuck_on_diverted_is_seen_as_a_failed_delivery(replay7):
    cc = next(r for r in rows_for(replay7, "PAX0001") if r["channel"] == "call_center" and r["field"] == "status" and r["since"] == T("14:10"))
    assert cc["shown_value"] == "diverted" and cc["cleared_at"] == T("15:32")
    assert cc["causes"][-1] == "failed_delivery" and set(cc["causes"]) <= {"waiting_for_update", "stuck", "failed_delivery"}


# ------------------------------------------------------------------ the scan

def test_an_incremental_scan_gives_the_same_result_as_a_full_scan(day7):
    a, b = replay(day7), replay(day7, full_every=True)
    key = lambda s: sorted((r["passenger_id"], r["channel"], r["field"], r["since"], r.get("cleared_at"), tuple(r["severities"]),
                            tuple(r["causes"])) for r in s["mismatches"].values())                     # noqa: E731
    assert key(a) == key(b)


def test_no_duplicate_open_rows_per_passenger_channel_and_field(replay7):
    seen = set()
    for r in replay7["mismatches"].values():
        k = (r["passenger_id"], r["channel"], r["field"], r["since"])
        assert k not in seen
        seen.add(k)
    stretches = {}
    for r in replay7["mismatches"].values():
        stretches.setdefault((r["passenger_id"], r["channel"], r["field"]), []).append((r["since"], r.get("cleared_at")))
    for k, s in stretches.items():
        s.sort()
        for (a0, a1), (b0, _) in zip(s, s[1:]):
            assert a1 is not None and a1 <= b0, k                                   # one open stretch at a time


def test_seed_7_scores_against_the_answer_key(day7, replay7):
    sc = judge(day7, replay7, Feeds())
    assert sc["recall"] >= 0.98 and sc["precision"] >= 0.98 and sc["delay_max"] <= 1
    assert sc["critical_flagged"] == sc["critical_real"] > 0 and sc["errors"] == 0


def test_every_finding_has_sources_with_times_and_decision_ids(replay7):
    for r in replay7["mismatches"].values():
        assert r["sources"] and all(x["as_of"] for x in r["sources"]) and r["decision_ids"]


# ------------------------------------------------------------------ AgentContext: phases, own rows only

def test_actions_of_later_phases_are_refused(db):
    from copy import deepcopy
    from backend.contracts.loader import Contract
    contracts = dict(load_contracts())
    original_phase = deepcopy(contracts["communication"].data)
    original_phase["phase"] = "4b-1"
    contracts["communication"] = Contract(original_phase)
    ctx = AgentContext("communication", db, T("12:00"), run_id="comm-test", contracts=contracts)
    out = {"title": "x", "why": "x", "data_as_of": T("12:00").isoformat(), "options": [], "findings": [], "decision_ids": ["O03"],
           "sources": [{"source": "reservations", "as_of": T("12:00").isoformat()}]}
    for action in ("fix_channel_mismatch", "resend_on_other_channel", "draft_message", "send_mass_message"):
        with pytest.raises(ContractViolation, match="phase"):
            ctx.propose(action, out)
    with pytest.raises(ContractViolation, match="human-only"):
        ctx.propose("public_statement", out)
    assert ctx.propose("escalate_channel_problem", out)


def test_only_communication_writes_mismatches_and_only_its_own_rows(db):
    row = {"passenger_id": "PAX0001", "channel": "app", "field": "status", "shown_value": "a", "true_value": "b", "cause": "stuck",
           "cause_confidence": 0.6, "severity": "high", "flight_id": None, "status": "open", "since": T("12:00"), "detected_at": T("12:00"),
           "evidence": [], "sources": [{"source": "reservations", "as_of": T("12:00").isoformat()}], "decision_ids": ["O03"]}
    mine = AgentContext("communication", db, T("12:00"), run_id="comm-test-a")
    rid = mine.write("mismatches", row)
    assert db.execute("SELECT agent_run_id FROM mismatches WHERE id=%s", (rid,)).fetchone()[0] == "comm-test-a"
    other = AgentContext("communication", db, T("12:00"), run_id="comm-test-b")
    with pytest.raises(ContractViolation, match="own"):
        other.write("mismatches", {"status": "cleared"}, mode="update", row_id=rid)
    with pytest.raises(ContractViolation, match="sources"):
        mine.write("mismatches", {**row, "sources": []})
    with pytest.raises(ContractViolation):
        AgentContext("situation", db, T("12:00"), run_id="x").write("mismatches", row)


# ------------------------------------------------------------------ in the database: one replay, a failure mid-scan

@pytest.fixture(scope="module")
def seed7_in_db():
    import contextlib
    import io
    from scripts.reset_db import main as reset_db
    from simulator.run import run
    run(7, "a31_storm")
    yield "a31_storm-s7-today"
    with contextlib.redirect_stdout(io.StringIO()):
        reset_db()                                  # leave the database as the later tests expect it (freshly seeded)


def test_a_failure_mid_scan_rolls_back_and_writes_one_agent_error(seed7_in_db):
    from backend.agents.communication.run import replay as db_replay
    from backend.database import connect
    with connect() as conn:
        conn.autocommit = True
        res = db_replay(conn, seed7_in_db, until=T("11:35"), fail_at=T("11:30"))
        arid = res["agent_run_id"]
        errors = conn.execute("SELECT count(*) FROM events WHERE run_id=%s AND type='agent_error'", (arid,)).fetchone()[0]
        at_fail = conn.execute("SELECT count(*) FROM mismatches WHERE agent_run_id=%s AND detected_at=%s", (arid, T("11:30"))).fetchone()[0]
        after = conn.execute("SELECT count(*) FROM mismatches WHERE agent_run_id=%s AND since=%s", (arid, T("11:30"))).fetchone()[0]
    assert errors == 1 and at_fail == 0 and after > 0           # nothing partial at 11:30; the next scan found them (since 11:30)


# ------------------------------------------------------------------ Part 0: the Situation agent's end confidence

def test_situation_end_confidence_is_never_above_0_95():
    from backend.agents.situation.assess import end_confidence, load_settings as situation_settings
    s = situation_settings()
    for key in s["expected_end"]["confidence_by_group"]:
        parts = key.split("|") + ["0", "day", "clean"][len(key.split("|")) - 1:]
        f = {"cause": parts[0], "changes": parts[1], "time": parts[2], "messy": parts[3] == "messy"}
        assert end_confidence(f, s) <= 0.95, key
