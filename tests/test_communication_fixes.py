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
    assert not propose_fixes(ctx, result, FixMemory(), open_conflicts={"F1"})
    out = {"title": "x", "why": "x", "data_as_of": ctx.now.isoformat(), "options": [], "findings": [],
           "decision_ids": ["O03"], "sources": [{"source": "reservations", "as_of": ctx.now.isoformat()}]}
    for action in ("draft_message", "send_status_update", "send_mass_message"):
        with pytest.raises(ContractViolation, match="phase"):
            ctx.propose(action, out)


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
             "flight_feed_fresh": True, "conflicts_known": True}
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
    assert not delivered  # auto in the catalog, but still disabled until phase 4b-3


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
