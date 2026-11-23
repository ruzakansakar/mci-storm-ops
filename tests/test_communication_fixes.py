"""Phase 4b-2: actual delivery, identical operational truth and boundary checks."""
from copy import deepcopy
from datetime import timedelta
import random

import pytest

from backend.agents.communication.detect import Memory, scan, load_settings
from backend.agents.communication.fix import FixMemory, propose_fixes
from backend.agents.communication.state_at import clock
from backend.contracts.buffered import BufferedContext
from backend.contracts.context import ContractViolation
from backend.contracts.feeds import Feeds
from backend.execution.executor import execute
from backend.rules.channel_fix import reasons
from simulator.comparison import truth_fingerprint


@pytest.fixture(scope="module")
def pair():
    from backend.seed.build import build
    from simulator.run import simulate
    data = build()
    return simulate(7, "a31_storm", data=data), simulate(7, "a31_storm", mode="copilot", data=data)


def fixture_context(feeds=None):
    now = clock("12:00")
    ctx = BufferedContext(now, [{"id": "F1", "direction": "departure", "sched_dep": clock("12:30"),
                                 "sched_arr": clock("14:00")}],
                          [{"id": "P1", "segment": "standard", "channel_types": ["app", "sms"]}], feeds)
    initial = {"passenger_id": "P1", "itinerary": ["F1"], "status": "confirmed", "destination": "BWI", "bag_routing": ["F1"]}
    ctx.events = [{"id": 1, "occurred_at": clock("05:00"), "type": "trip_record_updated", "payload": initial},
                  {"id": 2, "occurred_at": clock("11:55"), "type": "trip_record_updated",
                   "payload": {**initial, "status": "cancelled"}}]
    return ctx


def test_identical_truth_and_non_channel_events(pair):
    assert truth_fingerprint(pair[0]) == truth_fingerprint(pair[1])


def test_maya_and_disagreement_improve_without_instant_delivery(pair):
    today, co = pair
    a = next(v for v in today.metrics["maya_versions"] if v["changed_at"] == 895)
    b = next(v for v in co.metrics["maya_versions"] if v["changed_at"] == 895)
    for ch in load_settings()["channels"]:
        assert 895 < b["catch_up"][ch] < a["catch_up"][ch]
    assert co.metrics["passenger_minutes_of_disagreement"] < today.metrics["passenger_minutes_of_disagreement"]
    fixes = [e for e in co.world.events if e["type"] == "channel_view_updated" and e["payload"].get("fix_id") is not None]
    assert fixes and all(e["payload"]["lag_min"] >= 1 for e in fixes)
    assert co.metrics["fixes_failed"] > 0


def test_rolling_spam_limit(pair):
    from collections import defaultdict
    groups = defaultdict(list)
    cfg = load_settings()["fix"]
    for e in pair[1].world.events:
        if e["type"] == "channel_fix_applied":
            p = e["payload"]["proposal"]
            groups[(p["passenger_id"], p["channel"])].append(e["t"])
    assert groups
    for times in groups.values():
        for t in times:
            assert sum(t - cfg["window_min"] < other <= t for other in times) <= cfg["max_pushes"]


def test_stale_truth_holds_all_fixes():
    ctx = fixture_context(Feeds(outages={"reservations": [(clock("11:56"), clock("13:00"))]}))
    memory = Memory()
    result = scan(ctx, memory, write=False)
    assert not propose_fixes(ctx, result, FixMemory(), memory.events.values())
    assert any(p["reason"] == "truth feed stale" for typ, p in ctx.outputs if typ == "channel_fix_held")


def test_conflict_holds_and_messages_still_disabled():
    ctx = fixture_context()
    mem = Memory()
    result = scan(ctx, mem, write=False)
    from backend.agents.communication.situation_view import SituationView
    view = SituationView(version=1, conflicts={"F1": {"since": ctx.now, "version": 1, "key": "departed-during-stop:F1"}})
    assert not propose_fixes(ctx, result, FixMemory(), view)            # Situation reports conflicting data on F1
    out = {"title": "x", "why": "x", "data_as_of": ctx.now.isoformat(), "options": [], "findings": [],
           "decision_ids": ["O03"], "sources": [{"source": "reservations", "as_of": ctx.now.isoformat()}]}
    # phase gating still works: under the 4b-2 contract, message actions are refused ("switched on in phase 4b-3")
    from copy import deepcopy
    from backend.contracts import load_contracts
    from backend.contracts.loader import Contract
    contracts = dict(load_contracts())
    data = deepcopy(contracts["communication"].data)
    data["phase"] = "4b-2"
    contracts["communication"] = Contract(data)
    old = BufferedContext(ctx.now, [], [])
    old.contracts, old.contract = contracts, contracts["communication"]
    for action in ("draft_message", "send_status_update", "send_mass_message"):
        with pytest.raises(ContractViolation, match="phase"):
            old.propose(action, out)
    # and in 4b-3 a public statement is still human only: the agent may only hand over facts
    with pytest.raises(ContractViolation, match="human-only|not an action"):
        ctx.propose("public_statement", out)


def test_no_names_no_contacts_and_future_payload_stripped():
    ctx = fixture_context()
    for col in ("full_name", "contact_channels", "booking_ref"):
        with pytest.raises(ContractViolation):
            ctx.read("passengers", ["id", col])
    assert ctx.contact_channel_types("P1") == ["app", "sms"]
    ctx.events[0]["payload"]["cause_if_late"] = "secret"
    future = deepcopy(ctx.events[0])
    future.update(id=3, occurred_at=clock("13:00"))
    ctx.events.append(future)
    seen = ctx.read_events()
    assert len(seen) == 2 and "secret" not in str(seen)


def test_executor_only_auto_and_pass_can_deliver():
    called, records = [], []
    p = {"agent": "communication", "action": "send_mass_message", "decision_ids": ["O09"], "data_as_of": clock("12:00")}
    out = execute(p, {"now": clock("12:00")}, called.append, lambda *x: records.append(x))
    assert out["status"] == "pending" and not called
    p["action"] = "fix_channel_mismatch"
    out = execute(p, {"now": clock("12:00")}, called.append, lambda *x: records.append(x))
    assert out["verdict"]["result"] == "block" and not called
    assert records[-1][0] == "rule_check_blocked"


def test_structured_copy_rejects_invented_fields_and_stale_versions():
    snap = {"itinerary": ["F1"], "status": "cancelled", "destination": "BWI", "bag_routing": []}
    p = {"action": "fix_channel_mismatch", "passenger_id": "P1", "channel": "app", "confirmed": snap, "truth_as_of": "v1"}
    world = {"confirmed_trips": {"P1": snap}, "trip_versions": {"P1": "v1"}, "truth_feed_fresh": True,
             "flight_feed_fresh": True, "conflicts_known": True, "screens": {"P1": ("app", "gate_screen", "call_center")}}
    assert not reasons(p, world)
    assert reasons({**p, "confirmed": {**snap, "hotel": "promised"}}, world)
    assert reasons({**p, "message": {"text": "Free hotel"}}, world)
    assert reasons({**p, "truth_as_of": "old"}, world)
    p.update(agent="communication", decision_ids=["O03", "O11"], data_as_of=clock("12:00"))
    world["now"] = clock("12:00")
    delivered, records = [], []
    result = execute(p, world, delivered.append, lambda *x: records.append(x))
    assert result["status"] == "applied" and delivered == [p]
    assert [r[0] for r in records] == ["rule_check_passed", "channel_fix_applied"]
    p["action"] = "send_status_update"
    delivered.clear()
    assert execute(p, world, delivered.append, lambda *x: None)["status"] == "pending"
    assert not delivered  # a message action is on in 4b-3, but one with no message to fact-check is refused


def test_corrections_use_channel_delay_and_failure_draws():
    from simulator.channels import Channels
    from simulator.config import load_config
    cfg = deepcopy(load_config())
    cfg["channels"]["app"].update(base_min=5, jitter_min=0, per_100_changes_min=0, fail_chance=1)
    truth = {"P1": {"itinerary": ("F1",), "status": "cancelled", "destination": "BWI", "bag_routing": (), "message": "existing text"}}
    channels = Channels(cfg, random.Random(1), truth, 300)
    channels.fix_rng = random.Random(2)
    p = {"id": 1, "passenger_id": "P1", "channel": "app", "confirmed": {k: v for k, v in truth["P1"].items() if k != "message"}}
    channels.deliver_fix(600, p)
    channels.apply_due(604)
    assert not channels.events
    channels.apply_due(605)
    assert channels.events[0]["type"] == "channel_update_failed"
    assert channels.events[0]["payload"]["queued_at"] == 600
    old = {**p, "confirmed": {**p["confirmed"], "status": "confirmed"}}
    queued = len(channels.queue)
    assert channels.deliver_fix(606, old)["accepted"] is False
    assert len(channels.queue) == queued


def test_noncritical_wait_and_critical_skips_it():
    ctx = fixture_context()
    ctx.events[1]["occurred_at"] = ctx.now
    r = scan(ctx, Memory(), write=False)
    assert propose_fixes(ctx, r, FixMemory())
    ctx.events[1]["payload"]["status"] = "delayed"
    r = scan(ctx, Memory(), write=False)
    assert not propose_fixes(ctx, r, FixMemory())


# ------------------------------------------------------------------ seed 7, with co-pilot: what was actually applied

def _applied(result):
    return [e for e in result.world.events if e["type"] == "channel_fix_applied"]


def test_maya_stuck_call_center_diverted_view_is_fixed(pair):
    today, co = pair
    t_v = next(v for v in today.metrics["maya_versions"] if v["changed_at"] == 850)          # 14:10 back from STL: "delayed"
    c_v = next(v for v in co.metrics["maya_versions"] if v["changed_at"] == 850)
    assert "call_center" not in t_v["catch_up"]                     # today the lost update leaves it on "diverted"
    assert 850 < c_v["catch_up"]["call_center"] < 895              # with co-pilot it shows "delayed" before the 14:55 change
    fixes = [e["payload"]["proposal"] for e in _applied(co) if e["payload"]["proposal"]["passenger_id"] == "PAX0001"
             and e["payload"]["proposal"]["original_channel"] == "call_center" and 850 <= e["t"] < 895]
    assert fixes and all(p["confirmed"]["status"] == "delayed" for p in fixes)


def test_every_applied_fix_is_exactly_the_confirmed_trip_at_that_minute(pair):
    co = pair[1]
    latest, checked = {}, 0
    for e in co.world.events:                                        # events are in time order
        if e["type"] == "trip_record_updated":
            p = e["payload"]
            latest[p["passenger_id"]] = {k: p[k] for k in ("itinerary", "status", "destination", "bag_routing")}
        elif e["type"] == "channel_fix_applied":
            prop = e["payload"]["proposal"]
            assert prop["confirmed"] == latest[prop["passenger_id"]]
            assert not any(k in prop for k in ("message", "offer", "draft_text", "promises"))
            checked += 1
    assert checked > 100


def test_no_names_contacts_or_booking_refs_anywhere_in_agent_output(pair):
    from backend.seed.build import build
    co = pair[1]
    secret = {p["full_name"] for p in build()["passengers"]} | {p["booking_ref"] for p in build()["passengers"]}
    text = repr([e for e in co.world.events if e["actor"] == "agent"]) + repr([a for a in co.world.audit_rows if a["actor"] == "agent"])
    assert not [s for s in secret if s in text]
    assert co.metrics["contract_violations"] == 0


def test_every_check_and_apply_is_in_events_and_audit(pair):
    co = pair[1]
    kinds = ("rule_check_passed", "rule_check_blocked", "channel_fix_applied", "channel_fix_blocked")
    events = Counter_of(e["type"] for e in co.world.events if e["actor"] == "agent" and e["type"] in kinds)
    audits = Counter_of(a["what"] for a in co.world.audit_rows if a["actor"] == "agent" and a["what"] in kinds)
    assert events == audits and events["channel_fix_applied"] > 0
    assert events["rule_check_passed"] + events["rule_check_blocked"] >= events["channel_fix_applied"]


def Counter_of(items):
    from collections import Counter
    return Counter(items)


def test_no_repeat_of_a_version_too_soon_and_one_resend_per_version(pair):
    co, cfg = pair[1], load_settings()["fix"]
    failed = [(e["t"], e["payload"]["passenger_id"], e["payload"]["channel"]) for e in co.world.events
              if e["type"] == "channel_update_failed"]
    last, resends = {}, set()
    for e in _applied(co):
        p = e["payload"]["proposal"]
        if p["action"] == "resend_on_other_channel":
            key = (p["passenger_id"], p["channel"], p["truth_as_of"])
            assert key not in resends
            resends.add(key)
            continue
        key = (p["passenger_id"], p["channel"], p["truth_as_of"])
        if key in last and e["t"] - last[key] < cfg["retry_min"]:   # only allowed straight after a failed delivery receipt
            assert any(last[key] < t <= e["t"] and pid == key[0] and ch == key[1] for t, pid, ch in failed)
        last[key] = e["t"]


def test_both_modes_survive_in_the_database_and_today_is_untouched():
    import contextlib
    import io
    from backend.database import connect
    from scripts.reset_db import main as reset_db
    from simulator.run import run

    def snapshot(conn):
        return (conn.execute("SELECT md5(string_agg(f::text, '|' ORDER BY id)) FROM flights f").fetchone()[0],
                conn.execute("SELECT md5(string_agg(b::text, '|' ORDER BY passenger_id, seq)) FROM bookings b").fetchone()[0],
                conn.execute("SELECT md5(string_agg(v::text, '|' ORDER BY passenger_id, channel)) FROM channel_views v").fetchone()[0],
                conn.execute("SELECT count(*) FROM events WHERE run_id='a31_storm-s7-today'").fetchone()[0])
    try:
        run(7, "a31_storm", until="12:20")
        with connect() as conn:
            before = snapshot(conn)
        with pytest.raises(ValueError):
            run(7, "random", mode="copilot", until="12:20")        # no matching today run
        run(7, "a31_storm", mode="copilot", until="12:20")
        with connect() as conn:
            assert snapshot(conn) == before                         # today's truth, screens and events are all still there
            modes = [r[0] for r in conn.execute("SELECT mode FROM metric_runs WHERE seed=7 AND scenario='a31_storm' ORDER BY id")]
            assert modes == ["today", "copilot"]
            assert conn.execute("SELECT count(*) FROM events WHERE run_id='a31_storm-s7-copilot' AND type='channel_fix_applied'"
                                ).fetchone()[0] > 0
            assert conn.execute("SELECT count(*) FROM recommendations WHERE run_id='a31_storm-s7-copilot'").fetchone()[0] > 0
        with pytest.raises(ValueError, match="already exists"):
            run(7, "a31_storm", mode="copilot", until="12:20")
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            reset_db()


def test_nobody_gets_a_screen_a_fix_or_a_detection_they_do_not_have(pair):
    from backend.screens import screens_for
    from backend.seed.build import build
    has = {p["id"]: set(screens_for(p["contact_channels"])) for p in build()["passengers"]}
    assert has["PAX0001"] == {"app", "sms", "gate_screen", "call_center"}           # Maya: app + SMS (story override)
    assert any("app" not in s for s in has.values()) and any("sms" not in s for s in has.values())
    for r in pair:
        assert {(p, c) for p, c in r.channels.shown} == {(p, c) for p, s in has.items() for c in s}
        for e in r.world.events:
            p = e["payload"] or {}
            if e["type"].startswith("channel_") and "channel" in p and "passenger_id" in p:
                assert p["channel"] in has[p["passenger_id"]], e
            if e["type"] == "channel_fix_applied":
                assert p["proposal"]["channel"] in has[p["proposal"]["passenger_id"]]
