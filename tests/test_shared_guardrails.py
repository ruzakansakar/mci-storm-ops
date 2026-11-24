"""The shared AI guardrails (config/agents/_shared.yaml): each one is enforced where the file says, and works."""
import importlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.contracts import AgentContext, ContractViolation, StaleDataError
from backend.contracts.decisions import load_shared
from backend.rules.check import check
from tests.test_contracts import NOW, good_output, ready

G = {g["id"]: g for g in load_shared()["guardrails"]}


def ctx_for(agent, cur, **kw):
    return AgentContext(agent, cur, kw.pop("now", NOW), run_id=f"shared-test-{agent}", **kw)


def test_there_are_eleven_guardrails_and_each_names_a_real_function():
    assert len(G) == 11
    for g in G.values():
        assert g["rule"] and g["how"] and g["enforced_in"] in ("AgentContext", "orchestrator rule check",
                                                               "AgentContext and orchestrator rule check")
        module, name = g["checked_by"].split(":")
        obj = importlib.import_module(module)
        for part in name.split("."):
            obj = getattr(obj, part)
        assert callable(obj), g["id"]


def test_the_loader_skips_the_shared_file():
    from backend.contracts import load_contracts
    assert "_shared" not in load_contracts()


def test_g01_every_fact_needs_a_source_and_an_as_of_time(db):
    ctx = ctx_for("situation", db)
    out = good_output("situation", "assess_situation")
    with pytest.raises(ContractViolation, match="missing 'sources'"):
        ctx.propose("assess_situation", {k: v for k, v in out.items() if k != "sources"})
    with pytest.raises(ContractViolation, match="needs at least 1"):
        ctx.propose("assess_situation", {**out, "sources": []})
    with pytest.raises(ContractViolation, match="missing 'as_of'"):
        ctx.propose("assess_situation", {**out, "sources": [{"source": "flights"}]})
    with pytest.raises(ContractViolation, match="no usable as_of"):
        ctx.propose("assess_situation", {**out, "sources": [{"source": "flights", "as_of": "this morning!!"}]})
    assert ctx.propose("assess_situation", out)


def test_g02_old_data_is_refused_by_the_context_and_by_the_rule_check(db):
    # an agent that reads a live table (station_staff's contract with the stations table read live; since 4f every agent reads
    # only what was published before the day plus events, whose freshness the feed heartbeats check)
    from tests.test_contracts import live_reader_contracts
    ctx = ctx_for("station_staff", db, now=NOW + timedelta(days=1), contracts=live_reader_contracts())
    ctx.read("stations", ["code"], limit=1)
    action = "request_wheelchair_help"
    ready(ctx, action)
    with pytest.raises(StaleDataError):
        ctx.propose(action, good_output("station_staff", action))
    old = {"agent": "network_recovery", "action": "delay_flight", "decision_ids": ["B02"], "data_as_of": NOW - timedelta(minutes=5)}
    assert check(old, [], {"now": NOW})["result"] == "block"


def test_g03_no_human_only_decision_and_no_safety_exception(db):
    ctx = ctx_for("network_recovery", db)
    with pytest.raises(ContractViolation, match="human-only"):
        ctx.propose("divert_flight", good_output("network_recovery"))
    with pytest.raises(ContractViolation, match="human-only"):
        ctx.propose("extend_crew_duty", good_output("network_recovery"))
    p = {"agent": "station_staff", "action": "start_deplaning", "decision_ids": ["F05"], "data_as_of": NOW, "safety_rung": 1,
         "option": {"relies_on_tarmac_exception": True}}
    assert check(p, [], {"now": NOW})["result"] == "block"
    assert ctx.ask_human("B08", {"fuel_minutes_extra": 25, "alternate": "STL"}, "fuel nearing the floor")   # facts go to the humans


def test_g04_no_unapproved_promises():
    p = {"agent": "communication", "action": "send_status_update", "decision_ids": ["O14"], "data_as_of": NOW, "safety_rung": 8,
         "message": {"promises": [{"what": "a $75 voucher", "backed_by": None}]}}
    r = check(p, [], {"now": NOW, "approved": set()})
    assert r["result"] == "block" and "offer_needs_approved_decision" in r["rule_ids"]


def test_g05_names_only_for_the_passengers_it_works_on(db):
    care = ctx_for("passenger_care", db)
    with pytest.raises(ContractViolation, match="working on"):
        care.read("passengers", ["full_name"], where={"id": "PAX0001"})
    care.working_on(["PAX0001"])
    assert care.read("passengers", ["full_name"], where={"id": "PAX0001"})
    with pytest.raises(ContractViolation):
        ctx_for("station_staff", db).read("passengers", ["full_name"])


def test_g06_output_must_match_the_schema(db):
    ctx = ctx_for("situation", db)
    with pytest.raises(ContractViolation, match="must be"):
        ctx.propose("assess_situation", {**good_output("situation"), "confidence": "very"})


def test_g07_blocks_and_doubts_go_to_a_named_human(db):
    p = {"agent": "network_recovery", "action": "delay_flight", "decision_ids": ["B02"], "data_as_of": NOW, "flight_id": "WN1039"}
    r = check(p, [{**p, "id": "other", "action": "cancel_flight"}], {"now": NOW})
    assert r["result"] == "block" and r["needs_human"] == "network_operations_control" and r["reasons"]
    ev = ctx_for("situation", db).ask_human("A09", {"app": "delayed", "gate_screen": "on time"}, "two systems disagree")
    assert db.execute("SELECT payload->>'reason' FROM events WHERE id=%s", (ev,)).fetchone()[0] == "two systems disagree"


def test_g08_every_recommendation_names_its_decisions(db):
    ctx = ctx_for("network_recovery", db)
    ready(ctx, "delay_flight")
    out = good_output("network_recovery", "delay_flight")
    with pytest.raises(ContractViolation, match="needs at least 1"):
        ctx.propose("delay_flight", {**out, "decision_ids": []})
    with pytest.raises(ContractViolation, match="not a decision"):
        ctx.propose("delay_flight", {**out, "decision_ids": ["Z99"]})
    with pytest.raises(ContractViolation, match="raised by passenger_care"):
        ctx.propose("delay_flight", {**out, "decision_ids": ["K01"]})
    rec = ctx.propose("delay_flight", out)
    assert db.execute("SELECT decision_ids FROM recommendations WHERE id=%s", (rec,)).fetchone()[0] == out["decision_ids"]


def test_g09_an_agents_own_check_never_counts(db):
    from backend.contracts.review import shown_to_humans
    ctx = ctx_for("network_recovery", db)
    ready(ctx, "delay_flight")                                   # it ran its own crew checks...
    rec = ctx.propose("delay_flight", good_output("network_recovery", "delay_flight"))
    assert db.execute("SELECT rule_check_ok FROM recommendations WHERE id=%s", (rec,)).fetchone()[0] is None
    assert rec not in shown_to_humans(db, ctx.run_id)            # ...but nobody sees it until the orchestrator's rule check passes it


def test_g10_language_model_text_is_checked_against_the_facts(db):
    com = ctx_for("communication", db)
    facts = {"flight": "WN1033", "new_time": "06:30", "city": "Baltimore"}
    template = "Your flight {flight} to {city} is now at {new_time} tomorrow."
    ok = com.ai_text("Good news: WN1033 to Baltimore now leaves at 06:30.", facts, template)
    assert ok["used_template"] is False and ok["text"].startswith("Good news")
    bad = com.ai_text("WN1033 now leaves at 05:45 and you get $200.", facts, template)
    assert bad["used_template"] is True and bad["text"] == "Your flight WN1033 to Baltimore is now at 06:30 tomorrow."
    assert any("05:45" in p for p in bad["problems"])
    assert com.ai_text(None, facts, template)["used_template"] is True                      # model down: template
    with pytest.raises(ContractViolation, match="language model"):
        ctx_for("network_recovery", db).ai_text("x", facts, template)


def test_g11_everything_is_written_to_the_audit_log(db):
    ctx = ctx_for("station_staff", db)
    count = "SELECT count(*) FROM audit_log WHERE run_id=%s"
    before = db.execute(count, (ctx.run_id,)).fetchone()[0]
    ctx.record_event("staffing_gap_found", {"gate": "B12"})
    rec = ctx.propose("request_wheelchair_help", good_output("station_staff", "request_wheelchair_help"))
    with pytest.raises(ContractViolation):
        ctx.read("crews", ["id"])
    assert db.execute(count, (ctx.run_id,)).fetchone()[0] - before == 3      # event, proposal, refusal
    assert rec
