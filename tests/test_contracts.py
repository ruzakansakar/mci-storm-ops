"""Agent contracts: each agent can do what its contract allows and is refused everything else."""
import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

from backend.contracts import AgentContext, ContractError, ContractViolation, StaleDataError, load_contracts
from backend.contracts.jsonschema_lite import sample, validate
from backend.contracts.loader import (AGENT_NAMES, AGENTS_DIR, AI_AGENTS, PII_AGENTS, PII_COLUMNS, REQUIRED_FIELDS, TRUTH_TABLES,
                                      WRITABLE_TABLES, schema_tables, validate_contract)
from backend.rules import load_rules
from tests.conftest import one

CONTRACTS = load_contracts()
RULES = load_rules()
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=ZoneInfo("America/Chicago"))
HUMAN_ONLY = {a for a, v in RULES["autonomy"].items() if v["level"] == "human_only"}


def raw(agent):
    return yaml.safe_load((AGENTS_DIR / f"{agent}.yaml").read_text(encoding="utf-8"))


def ctx_for(agent, cur, **kw):
    return AgentContext(agent, cur, kw.pop("now", NOW), run_id=f"contract-test-{agent}", **kw)


def violations(cur, agent):
    return one(cur, "SELECT count(*) FROM events WHERE type='contract_violation' AND run_id=%s", f"contract-test-{agent}")


# ------------------------------------------------------------------ the files themselves

def test_there_are_exactly_seven_contracts_and_each_loads():
    assert set(CONTRACTS) == set(AGENT_NAMES) and len(CONTRACTS) == 7
    assert sorted(p.stem for p in AGENTS_DIR.glob("*.yaml")) == sorted(AGENT_NAMES)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_a_contract_missing_a_field_fails_to_load(field):
    data = raw("situation")
    del data[field]
    with pytest.raises(ContractError, match=f"missing '{field}'|unknown agent"):
        validate_contract(data)


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_a_contract_with_an_empty_field_fails_to_load(agent):
    for field in ("never", "escalate_when", "success", "stores"):
        data = raw(agent)
        data[field] = []
        with pytest.raises(ContractError):
            validate_contract(data)


def test_stores_and_output_details_are_checked():
    data = raw("situation")
    del data["stores"]["permanent"]
    with pytest.raises(ContractError, match="permanent"):
        validate_contract(data)
    data = raw("situation")
    data["output"]["additionalProperties"] = True
    with pytest.raises(ContractError, match="additionalProperties"):
        validate_contract(data)
    data = raw("situation")
    data["output"]["required"].remove("why")
    with pytest.raises(ContractError, match="why"):
        validate_contract(data)
    data = raw("situation")
    data["max_data_age_sec"] = 0
    with pytest.raises(ContractError, match="max_data_age_sec"):
        validate_contract(data)
    data = raw("situation")
    data["output"]["properties"]["summary"]["pattern"] = "x"
    with pytest.raises(ContractError, match="unsupported keyword"):
        validate_contract(data)


def test_a_file_name_that_does_not_match_its_agent_fails(tmp_path):
    from backend.contracts.loader import load_contract_file
    data = raw("situation")
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ContractError, match="file name must match"):
        load_contract_file(tmp_path / "other.yaml")


def test_no_contract_lets_any_agent_write_to_a_truth_table():
    for agent, c in CONTRACTS.items():
        assert not set(c.writes) & TRUTH_TABLES, agent
        assert set(c.writes) <= WRITABLE_TABLES, agent
    for agent in AGENT_NAMES:
        for table in sorted(TRUTH_TABLES & set(schema_tables())):
            data = raw(agent)
            data["writes"][table] = {"mode": "insert"}
            with pytest.raises(ContractError, match="may not write"):
                validate_contract(data)


def test_only_the_orchestrator_writes_incidents():
    for agent, c in CONTRACTS.items():
        assert ("incidents" in c.writes) == (agent == "orchestrator"), agent
        assert ("incident_events" in c.writes) == (agent == "orchestrator"), agent
        if agent != "orchestrator":
            data = raw(agent)
            data["writes"]["incidents"] = {"mode": "insert"}
            with pytest.raises(ContractError, match="only the orchestrator"):
                validate_contract(data)


def test_agents_only_write_proposals_their_own_events_and_the_audit_log():
    for agent, c in CONTRACTS.items():
        assert {"events", "audit_log"} <= set(c.writes)
        extra = set(c.writes) - {"events", "audit_log", "recommendations", "incidents", "incident_events"}
        assert not extra, agent
    assert CONTRACTS["guardrail"].writes["recommendations"]["mode"] == "update"
    assert set(CONTRACTS["guardrail"].writes["recommendations"]["columns"]) == {"guardrail_ok", "guardrail_notes", "status"}


def test_only_two_agents_may_read_names_and_contact_details():
    assert PII_AGENTS == {"passenger_care", "communication"}
    for agent, c in CONTRACTS.items():
        reads_pii = any(col in PII_COLUMNS.get(t, ()) for t, cols in c.reads.items() for col in cols)
        assert reads_pii == (agent in PII_AGENTS), agent
        assert c.can_see_names() == (agent in PII_AGENTS)
    data = raw("network_recovery")
    data["reads"]["passengers"] = ["id", "full_name"]
    with pytest.raises(ContractError, match="names and contact details"):
        validate_contract(data)
    data = raw("station_staff")
    data["reads"]["channel_views"] = ["passenger_id", "message"]
    with pytest.raises(ContractError, match="names and contact details"):
        validate_contract(data)


def test_a_contract_cannot_read_a_column_that_does_not_exist_or_name_an_unknown_action():
    data = raw("situation")
    data["reads"]["flights"].append("secret_column")
    with pytest.raises(ContractError, match="unknown column"):
        validate_contract(data)
    data = raw("situation")
    data["actions"].append("do_anything")
    with pytest.raises(ContractError, match="not in the autonomy rules"):
        validate_contract(data)


def test_nobody_may_propose_a_human_only_decision():
    assert {"divert_flight", "keep_on_board_exception", "ramp_closure", "extend_crew_duty"} <= HUMAN_ONLY
    for agent, c in CONTRACTS.items():
        assert not set(c.actions) & HUMAN_ONLY, agent
    text = " ".join(" ".join(c["never"]) for c in CONTRACTS.values()).lower()
    assert "diversion" in text and "keep-on-board" in text and "crew" in text
    assert "promise" in " ".join(CONTRACTS["communication"]["never"]).lower()
    assert "legal duty" in " ".join(CONTRACTS["network_recovery"]["never"]).lower()


def test_a_language_model_is_only_for_situation_summaries_and_message_drafts_with_a_template_fallback():
    for agent, c in CONTRACTS.items():
        assert c.ai_allowed == (agent in AI_AGENTS), agent
        if c.ai_allowed:
            assert c["ai_allowed"]["fallback"] and "template" in c["ai_allowed"]["fallback"]
    data = raw("guardrail")
    data["ai_allowed"] = {"allowed": True, "for": ["x"], "fallback": "template"}
    with pytest.raises(ContractError, match="language model may only"):
        validate_contract(data)
    data = raw("communication")
    del data["ai_allowed"]["fallback"]
    with pytest.raises(ContractError, match="template fallback"):
        validate_contract(data)


def test_every_output_names_the_same_basic_fields_and_is_exact():
    for agent, c in CONTRACTS.items():
        assert c.output["additionalProperties"] is False
        for f in ("title", "why", "data_as_of", "options"):
            assert f in c.output["required"], (agent, f)
        assert validate(sample(c.output), c.output) == [], agent


def test_the_json_schema_checker():
    schema = {"type": "object", "additionalProperties": False, "required": ["a"],
              "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"type": ["string", "null"]},
                             "c": {"type": "array", "items": {"type": "string", "enum": ["x"]}, "minItems": 1}}}
    assert validate({"a": 1, "b": None, "c": ["x"]}, schema) == []
    assert validate({}, schema) and validate({"a": 0}, schema) and validate({"a": True}, schema)
    assert validate({"a": 1, "z": 1}, schema) and validate({"a": 1, "c": []}, schema) and validate({"a": 1, "c": ["y"]}, schema)


# ------------------------------------------------------------------ what the agent can do

@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_can_read_everything_its_contract_allows(db, agent):
    ctx = ctx_for(agent, db)
    for table, cols in CONTRACTS[agent].reads.items():
        rows = ctx.read(table, limit=1)
        assert len(rows) <= 1
        if rows:
            assert set(rows[0]) == set(cols)                   # exactly the allowed columns, nothing extra
    assert violations(db, agent) == 0


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_can_write_its_own_event_types_and_nothing_else(db, agent):
    ctx = ctx_for(agent, db)
    types = CONTRACTS[agent].writes["events"]["types"]
    for t in types:
        assert ctx.record_event(t, {"note": "test"})
    assert one(db, "SELECT count(*) FROM events WHERE run_id=%s AND actor='agent' AND actor_name=%s", ctx.run_id, agent) == len(types)
    others = {t for c in CONTRACTS.values() for t in c.writes["events"]["types"]} - set(types)
    for t in sorted(others)[:3] + ["made_up_event"]:
        with pytest.raises(ContractViolation, match="event type"):
            ctx.record_event(t)


@pytest.mark.parametrize("agent", [a for a in AGENT_NAMES if a != "guardrail"])      # (the guardrail only reviews; tested below)
def test_an_agent_can_propose_each_of_its_actions(db, agent):
    c = CONTRACTS[agent]
    ctx = ctx_for(agent, db)
    for action in c.actions or [None]:
        rec_id = ctx.propose(action, sample(c.output))
        row = db.execute("SELECT agent, status, autonomy FROM recommendations WHERE id=%s", (rec_id,)).fetchone()
        assert row[0] == agent and row[1] == "pending"
        if action:
            assert row[2] == RULES["autonomy"][action]["level"]
        assert one(db, "SELECT count(*) FROM audit_log WHERE run_id=%s AND actor_name=%s AND entity_id=%s", ctx.run_id, agent, str(rec_id)) >= 1


def test_the_guardrail_can_only_pass_or_block_a_recommendation(db):
    rec = ctx_for("situation", db).propose("send_status_update", sample(CONTRACTS["situation"].output))
    g = ctx_for("guardrail", db)
    before = violations(db, "guardrail")
    with pytest.raises(ContractViolation, match="may only update"):
        g.propose(None, sample(CONTRACTS["guardrail"].output))                    # it cannot write proposals of its own
    g.review(rec, False, ["fails rung 4"])
    assert db.execute("SELECT status, guardrail_ok, guardrail_notes FROM recommendations WHERE id=%s", (rec,)).fetchone() == \
        ("blocked_by_guardrail", False, ["fails rung 4"])
    with pytest.raises(ContractViolation, match="not in this agent's writes|columns"):
        g.write("recommendations", {"title": "rewritten"}, mode="update", row_id=rec)
    with pytest.raises(ContractViolation, match="status"):
        g.write("recommendations", {"status": "approved"}, mode="update", row_id=rec)
    with pytest.raises(ContractViolation):
        g.write("recommendations", {"title": "x"}, mode="insert")
    assert violations(db, "guardrail") - before == 4


def test_only_the_orchestrator_can_open_an_incident(db):
    o = ctx_for("orchestrator", db)
    inc = o.write("incidents", {"kind": "weather", "station": "MCI", "title": "Storm", "status": "open", "opened_at": NOW,
                                "opened_by": "orchestrator", "run_id": o.run_id})
    ev = o.record_event("incident_opened", {"incident_id": inc}, station="MCI")
    o.write("incident_events", {"incident_id": inc, "event_id": ev})
    o.write("incidents", {"status": "monitoring"}, mode="update", row_id=inc)
    assert one(db, "SELECT status FROM incidents WHERE id=%s", inc) == "monitoring"
    for agent in AGENT_NAMES:
        if agent == "orchestrator":
            continue
        with pytest.raises(ContractViolation, match="incidents"):
            ctx_for(agent, db).write("incidents", {"kind": "weather", "title": "x", "opened_at": NOW, "opened_by": agent})


# ------------------------------------------------------------------ everything else is refused

@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_is_refused_every_table_and_column_outside_its_reads(db, agent):
    ctx = ctx_for(agent, db)
    allowed = CONTRACTS[agent].reads
    outside = [t for t in schema_tables() if t not in allowed]
    for t in outside:
        with pytest.raises(ContractViolation, match="not in this agent's reads"):
            ctx.read(t, [schema_tables()[t][0]])
    for t, cols in allowed.items():
        for c in sorted(set(schema_tables()[t]) - set(cols))[:2]:          # (two examples per table keeps the test quick)
            with pytest.raises(ContractViolation, match="not in this agent's reads|name or contact"):
                ctx.read(t, [c])
            with pytest.raises(ContractViolation):
                ctx.read(t, [cols[0]], where={c: "x"})                           # not even to filter by it
    assert violations(db, agent) >= len(outside)


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_is_refused_every_write_to_a_truth_table(db, agent):
    ctx = ctx_for(agent, db)
    before = {t: one(db, f"SELECT count(*) FROM {t}") for t in ("flights", "bookings", "crews", "passengers", "staff", "aircraft")}
    for t in sorted(TRUTH_TABLES & set(schema_tables())):
        with pytest.raises(ContractViolation, match="truth table"):
            ctx.write(t, {"id": "x"}, mode="insert")
    # even the flight table the agents read cannot be changed
    with pytest.raises(ContractViolation, match="truth table"):
        ctx.write("flights", {"status": "cancelled"}, mode="update", row_id="WN1033")
    after = {t: one(db, f"SELECT count(*) FROM {t}") for t in before}
    assert before == after
    assert violations(db, agent) >= len(TRUTH_TABLES & set(schema_tables())) + 1


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_action_outside_the_contract_is_refused(db, agent):
    c = CONTRACTS[agent]
    ctx = ctx_for(agent, db)
    refused = [a for a in RULES["autonomy"] if a not in c.actions]
    assert refused                                                   # every agent is missing some actions
    for a in refused:
        with pytest.raises(ContractViolation, match="not an action"):
            ctx.propose(a, sample(c.output))
    with pytest.raises(ContractViolation, match="not an action"):
        ctx.propose("do_anything", sample(c.output))
    for a in HUMAN_ONLY:
        with pytest.raises(ContractViolation, match="not an action"):
            ctx.propose(a, sample(c.output))
    assert violations(db, agent) >= len(refused) + 1


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_output_that_does_not_match_the_schema_is_refused(db, agent):
    c = CONTRACTS[agent]
    ctx = ctx_for(agent, db)
    action = c.actions[0] if c.actions else None
    good = sample(c.output)
    before = violations(db, agent)
    saved_before = one(db, "SELECT count(*) FROM recommendations WHERE run_id=%s", ctx.run_id)
    for field in c.output["required"]:
        broken = {k: v for k, v in good.items() if k != field}
        with pytest.raises(ContractViolation, match="missing"):
            ctx.propose(action, broken)
    with pytest.raises(ContractViolation, match="not in the contract"):
        ctx.propose(action, {**good, "sneaky_extra_field": 1})
    with pytest.raises(ContractViolation, match="must be"):
        ctx.propose(action, {**good, "title": 123})
    assert violations(db, agent) - before == len(c.output["required"]) + 2
    # nothing was saved for any of the refused outputs
    assert one(db, "SELECT count(*) FROM recommendations WHERE run_id=%s", ctx.run_id) == saved_before


def test_a_refusal_is_a_clear_error_and_an_event_on_the_record(db):
    ctx = ctx_for("situation", db)
    with pytest.raises(ContractViolation) as e:
        ctx.read("hotels", ["name"])
    assert e.value.agent == "situation" and e.value.kind == "read" and "hotels" in e.value.detail
    row = db.execute("SELECT actor, payload FROM events WHERE type='contract_violation' AND run_id=%s ORDER BY id DESC LIMIT 1",
                     (ctx.run_id,)).fetchone()
    assert row[0] == "system" and row[1]["agent"] == "situation" and row[1]["kind"] == "read"


# ------------------------------------------------------------------ names are hidden

def test_passenger_names_and_contacts_are_hidden_from_agents_that_may_not_see_them(db):
    sees = {"passenger_care", "communication"}
    for agent in AGENT_NAMES:
        ctx = ctx_for(agent, db)
        if agent in sees:
            rows = ctx.read("passengers", ["id", "full_name", "contact_channels"], where={"id": "PAX0001"})
            assert rows[0]["full_name"] == "Maya Torres" and rows[0]["contact_channels"]
        else:
            for col in ("full_name", "booking_ref", "contact_channels"):
                with pytest.raises(ContractViolation):
                    ctx.read("passengers", [col])
            assert "passengers" not in CONTRACTS[agent].reads or "full_name" not in CONTRACTS[agent].reads["passengers"]
    with pytest.raises(ContractViolation, match="passenger ids only"):
        _ctx_with_custom_reads(db, {"flights": ["id"], "passengers": ["id"]}).read("passengers", ["full_name"])


def _ctx_with_custom_reads(cur, reads):
    """A context whose contract allows reading passengers.id (to prove names are still refused for an agent that may not see them)."""
    contracts = dict(CONTRACTS)
    c = copy.copy(CONTRACTS["situation"])
    c.reads = reads
    contracts["situation"] = c
    return AgentContext("situation", cur, NOW, run_id="contract-test-situation", contracts=contracts)


def test_channel_messages_can_name_people_so_only_the_two_agents_read_them(db):
    assert "message" in CONTRACTS["communication"].reads["channel_views"]
    for agent in AGENT_NAMES:
        if agent not in PII_AGENTS:
            assert "channel_views" not in CONTRACTS[agent].reads


# ------------------------------------------------------------------ stale data

def test_stale_data_is_refused_and_fresh_data_is_accepted(db):
    agent = "passenger_care"
    c = CONTRACTS[agent]
    limit = c.max_data_age_sec
    probe = ctx_for(agent, db)
    probe.read("hotels", ["id"], limit=1)
    as_of = probe.oldest_as_of
    assert as_of is not None
    out = sample(c.output)

    fresh = ctx_for(agent, db, now=as_of + timedelta(seconds=limit))
    fresh.read("hotels", ["id"], limit=1)
    assert fresh.propose("rebook_passenger", out)

    stale = ctx_for(agent, db, now=as_of + timedelta(seconds=limit + 1))
    stale.read("hotels", ["id"], limit=1)
    with pytest.raises(StaleDataError, match="old"):
        stale.propose("rebook_passenger", out)
    assert violations(db, agent) >= 1
    assert db.execute("SELECT payload->>'kind' FROM events WHERE type='contract_violation' AND run_id=%s ORDER BY id DESC LIMIT 1",
                      (stale.run_id,)).fetchone()[0] == "stale data"


def test_every_agent_has_a_data_age_and_a_time_budget():
    for agent, c in CONTRACTS.items():
        assert 0 < c.max_data_age_sec <= 300 and 0 < c.time_budget_sec <= 60, agent


# ------------------------------------------------------------------ temporary notes go when the incident closes

def test_temporary_notes_are_deleted_when_the_incident_closes(db):
    ctx = ctx_for("situation", db)
    ctx.remember("guess", {"cause": "weather"})
    assert ctx.recall("guess") == {"cause": "weather"}
    assert ctx.close_incident() == 1
    assert ctx.recall("guess") is None
    rec = ctx.propose("send_status_update", sample(CONTRACTS["situation"].output))
    assert one(db, "SELECT count(*) FROM recommendations WHERE id=%s", rec) == 1       # permanent records stay


def test_the_script_prints_a_summary_of_all_seven(capsys):
    from scripts.contracts import main
    main()
    out = capsys.readouterr().out
    for agent in AGENT_NAMES:
        assert agent in out
