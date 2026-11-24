"""Agent contracts: the orchestrator + five agents can do what their contracts allow and are refused everything else."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

from backend.contracts import AgentContext, ContractError, ContractViolation, StaleDataError, load_contracts
from backend.contracts.decisions import load_decisions
from backend.contracts.jsonschema_lite import sample, validate
from backend.contracts.loader import (AGENT_NAMES, AGENTS_DIR, AI_AGENTS, PII_AGENTS, PII_COLUMNS, REQUIRED_FIELDS, TRUTH_TABLES,
                                      WRITABLE_TABLES, schema_tables, validate_contract)
from backend.rules import load_rules
from tests.conftest import one

CONTRACTS = load_contracts()
RULES = load_rules()
DECISIONS = load_decisions()
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=ZoneInfo("America/Chicago"))
HUMAN_ONLY = {a for a, v in RULES["autonomy"].items() if v["level"] == "human_only"}


def raw(agent):
    return yaml.safe_load((AGENTS_DIR / f"{agent}.yaml").read_text(encoding="utf-8"))


def ctx_for(agent, cur, **kw):
    return AgentContext(agent, cur, kw.pop("now", NOW), run_id=f"contract-test-{agent}", **kw)


def violations(cur, agent):
    return one(cur, "SELECT count(*) FROM events WHERE type='contract_violation' AND run_id=%s", f"contract-test-{agent}")


def good_output(agent, action=None):
    """A valid output: the smallest one the schema allows, naming a decision this agent raises, with a source and time."""
    out = sample(CONTRACTS[agent].output)
    mine = [d for d in DECISIONS.values() if d["proposed_by"] == agent]
    match = [d for d in mine if d["action"] == action] or mine
    out["decision_ids"] = [match[0]["id"]]
    out["sources"] = [{"source": "flights (shared live view)", "as_of": NOW.isoformat(), "fact": "WN1033 status"}]
    return out


def ready(ctx, action):
    for name in ctx.contract.required_checks.get(action, []):
        ctx.run_check(name, {})


# ------------------------------------------------------------------ the files themselves

def test_there_are_six_contracts_the_orchestrator_and_five_agents():
    assert set(CONTRACTS) == set(AGENT_NAMES) == {"orchestrator", "situation", "network_recovery", "station_staff",
                                                   "passenger_care", "communication"}
    assert sorted(p.stem for p in AGENTS_DIR.glob("*.yaml") if not p.name.startswith("_")) == sorted(AGENT_NAMES)


def test_there_is_no_guardrail_agent_and_no_crew_agent_anywhere():
    from backend.settings import ROOT
    assert not (AGENTS_DIR / "guardrail.yaml").exists() and "guardrail" not in AGENT_NAMES and "crew" not in AGENT_NAMES
    for c in CONTRACTS.values():
        text = yaml.safe_dump(c.data).lower()
        assert "agent: guardrail" not in text and "guardrail_ok" not in text
    assert not any("guardrail" in str(v.get("approver")) for v in RULES["autonomy"].values())
    assert "guardrail" not in (ROOT / "backend" / "db" / "schema.sql").read_text(encoding="utf-8")
    for path in list((ROOT / "backend").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert '"guardrail"' not in text and "guardrail_ok" not in text, path


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
    for field in ("why", "decision_ids", "sources"):
        data = raw("situation")
        data["output"]["required"].remove(field)
        with pytest.raises(ContractError, match=field):
            validate_contract(data)
    data = raw("situation")
    data["output"]["additionalProperties"] = True
    with pytest.raises(ContractError, match="additionalProperties"):
        validate_contract(data)
    data = raw("situation")
    data["max_data_age_sec"] = 0
    with pytest.raises(ContractError, match="max_data_age_sec"):
        validate_contract(data)


def test_a_file_name_that_does_not_match_its_agent_fails(tmp_path):
    from backend.contracts.loader import load_contract_file
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(raw("situation")), encoding="utf-8")
    with pytest.raises(ContractError, match="file name must match"):
        load_contract_file(tmp_path / "other.yaml")


def test_no_contract_lets_any_agent_write_to_a_truth_table():
    for agent, c in CONTRACTS.items():
        assert not set(c.writes) & TRUTH_TABLES and set(c.writes) <= WRITABLE_TABLES, agent
    for agent in AGENT_NAMES:
        for table in sorted(TRUTH_TABLES & set(schema_tables())):
            data = raw(agent)
            data["writes"][table] = {"mode": "insert"}
            with pytest.raises(ContractError, match="may not write"):
                validate_contract(data)


def test_only_the_orchestrator_writes_incidents_and_rule_check_results():
    for agent, c in CONTRACTS.items():
        assert ("incidents" in c.writes) == (agent == "orchestrator"), agent
        assert (c.writes["recommendations"]["mode"] != "insert") == (agent == "orchestrator"), agent
    assert {"rule_check_ok", "rule_check_notes", "rule_ids"} <= set(CONTRACTS["orchestrator"].writes["recommendations"]["columns"])
    for agent in AGENT_NAMES[1:]:
        data = raw(agent)
        data["writes"]["incidents"] = {"mode": "insert"}
        with pytest.raises(ContractError, match="only the orchestrator"):
            validate_contract(data)
        data = raw(agent)
        data["writes"]["recommendations"] = {"mode": "insert_or_update", "columns": ["rule_check_ok"]}
        with pytest.raises(ContractError, match="only the orchestrator"):
            validate_contract(data)


def test_the_orchestrator_contract_runs_the_rule_check_on_everything_and_uses_no_ai():
    c = CONTRACTS["orchestrator"]
    assert c["runs_rule_check_on_every_proposal"] is True and c.ai_allowed is False
    never = " ".join(c["never"]).lower()
    assert "rule check" in never and "override" in never and "truth table" in never and "human only" in never
    assert {"rule_check_passed", "rule_check_blocked", "conflict_found"} <= set(c.writes["events"]["types"])


def test_network_recovery_owns_the_crew_work():
    c = CONTRACTS["network_recovery"]
    assert {"call_in_reserve_crew", "deadhead_crew", "book_crew_hotel", "flag_crew_for_tomorrow", "prepare_faa_extension_report"} <= set(c.actions)
    a = RULES["autonomy"]
    assert a["call_in_reserve_crew"] == {"level": "needs_approval", "approver": "crew_scheduling"}
    assert a["deadhead_crew"] == {"level": "needs_approval", "approver": "crew_scheduling"}
    assert a["prepare_faa_extension_report"] == {"level": "needs_approval", "approver": "crew_scheduling"}
    assert a["book_crew_hotel"]["level"] == "auto" and a["book_crew_hotel"]["max_rooms"] > 0
    assert a["book_crew_hotel"]["over_limit_approver"] == "crew_scheduling"
    assert a["extend_crew_duty"] == {"level": "human_only", "approver": "captain_and_crew_scheduling"}
    assert a["remove_crew_for_fatigue"]["level"] == "human_only"
    assert "extend_crew_duty" not in c.actions and "remove_crew_for_fatigue" not in c.actions
    never = " ".join(c["never"]).lower()
    for words in ("law.yaml crew rule", "duty extension", "fatigue", "leave"):
        assert words in never
    for action in ("delay_flight", "swap_aircraft", "call_in_reserve_crew"):
        assert {"crew_duty_left", "crew_rest", "pilot_fdp_limit", "fa_duty_limit"} <= set(c.required_checks[action])
    assert "availability" in c.reads["crews"]


def test_crew_hotels_are_automatic_only_up_to_the_room_limit():
    from backend.rules.autonomy import autonomy_for
    limit = RULES["autonomy"]["book_crew_hotel"]["max_rooms"]
    assert autonomy_for("book_crew_hotel", RULES, rooms=limit)["level"] == "auto"
    over = autonomy_for("book_crew_hotel", RULES, rooms=limit + 1)
    assert over["level"] == "needs_approval" and over["approver"] == "crew_scheduling"


def test_only_two_agents_may_read_names_and_contact_details():
    assert PII_AGENTS == {"passenger_care", "communication"}
    for agent, c in CONTRACTS.items():
        reads_pii = any(col in PII_COLUMNS.get(t, ()) for t, cols in c.reads.items() for col in cols)
        # communication may see names only from phase 4b-3 (messages); in 4b-1 (detection) its contract lists none
        expected = agent in PII_AGENTS and not (agent == "communication" and c.phase in ("4b-1", "4b-2"))
        assert reads_pii == expected, agent
    data = raw("network_recovery")
    data["reads"]["passengers"] = ["id", "full_name"]
    with pytest.raises(ContractError, match="names and contact details"):
        validate_contract(data)


def test_nobody_may_propose_a_human_only_decision():
    assert {"divert_flight", "keep_on_board_exception", "ramp_closure", "extend_crew_duty", "remove_crew_for_fatigue"} <= HUMAN_ONLY
    for agent, c in CONTRACTS.items():
        assert not set(c.actions) & HUMAN_ONLY, agent
    data = raw("network_recovery")
    data["actions"].append("divert_flight")
    with pytest.raises(ContractError, match="human-only"):
        validate_contract(data)


def test_a_language_model_is_only_for_situation_summaries_and_message_drafts_with_a_template_fallback():
    for agent, c in CONTRACTS.items():
        assert c.ai_allowed == (agent in AI_AGENTS), agent
        if c.ai_allowed:
            assert "template" in c["ai_allowed"]["fallback"]
    data = raw("orchestrator")
    data["ai_allowed"] = {"allowed": True, "for": ["x"], "fallback": "template"}
    with pytest.raises(ContractError, match="language model may only"):
        validate_contract(data)


def test_every_output_has_the_same_basic_fields_and_is_exact():
    for agent, c in CONTRACTS.items():
        assert c.output["additionalProperties"] is False
        for f in ("title", "why", "data_as_of", "options", "decision_ids", "sources"):
            assert f in c.output["required"], (agent, f)
        assert validate(good_output(agent), c.output) == [], agent


def test_the_json_schema_checker():
    schema = {"type": "object", "additionalProperties": False, "required": ["a"],
              "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"type": ["string", "null"]},
                             "c": {"type": "array", "items": {"type": "string", "enum": ["x"]}, "minItems": 1}}}
    assert validate({"a": 1, "b": None, "c": ["x"]}, schema) == []
    assert validate({}, schema) and validate({"a": 0}, schema) and validate({"a": True}, schema)
    assert validate({"a": 1, "z": 1}, schema) and validate({"a": 1, "c": []}, schema) and validate({"a": 1, "c": ["y"]}, schema)


# ------------------------------------------------------------------ what each one can do

@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_can_read_everything_its_contract_allows(db, agent):
    ctx = ctx_for(agent, db)
    ctx.working_on(["PAX0001"])
    before = violations(db, agent)
    for table, cols in CONTRACTS[agent].reads.items():
        pii = any(c in PII_COLUMNS.get(table, ()) for c in cols)
        where = ({"id": "PAX0001"} if table == "passengers" else {"passenger_id": "PAX0001"}) if pii else None
        rows = ctx.read(table, where=where, limit=1)
        if rows:
            assert set(rows[0]) - {"feed"} == set(cols)        # exactly the allowed columns (+ which feed an event came from)
    assert violations(db, agent) == before


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_can_write_its_own_event_types_and_nothing_else(db, agent):
    ctx = ctx_for(agent, db)
    types = CONTRACTS[agent].writes["events"]["types"]
    count = "SELECT count(*) FROM events WHERE run_id=%s AND actor='agent' AND actor_name=%s"
    before = one(db, count, ctx.run_id, agent)
    for t in types:
        assert ctx.record_event(t, {"note": "test"})
    assert one(db, count, ctx.run_id, agent) - before == len(types)
    others = {t for c in CONTRACTS.values() for t in c.writes["events"]["types"]} - set(types)
    for t in sorted(others)[:3] + ["made_up_event"]:
        with pytest.raises(ContractViolation, match="event type"):
            ctx.record_event(t)


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_can_propose_each_of_its_actions_and_it_waits_for_the_rule_check(db, agent):
    c = CONTRACTS[agent]
    ctx = ctx_for(agent, db)
    for action in c.actions:
        ready(ctx, action)
        if not c.enabled(action):                          # switched on in a later phase: refused until then
            with pytest.raises(ContractViolation, match="phase"):
                ctx.propose(action, good_output(agent, action))
            continue
        rec_id = ctx.propose(action, good_output(agent, action))
        row = db.execute("SELECT agent, status, autonomy, rule_check_ok, decision_ids FROM recommendations WHERE id=%s", (rec_id,)).fetchone()
        assert row[0] == agent and row[1] == "pending" and row[3] is None and row[4]       # unchecked until the orchestrator runs check()
        assert row[2] in ("auto", "needs_approval")
        assert one(db, "SELECT count(*) FROM audit_log WHERE run_id=%s AND actor_name=%s AND entity_id=%s", ctx.run_id, agent, str(rec_id)) >= 1


def test_network_recovery_must_run_the_crew_checks_before_a_delay_swap_or_reserve(db):
    # swap_crews, not swap_aircraft: since 4d only the built actions are switched on (swap_aircraft is "defined, not built")
    for action in ("delay_flight", "swap_crews", "call_in_reserve_crew"):
        ctx = ctx_for("network_recovery", db)
        with pytest.raises(ContractViolation, match="needs these checks first"):
            ctx.propose(action, good_output("network_recovery", action))
        ready(ctx, action)
        assert ctx.propose(action, good_output("network_recovery", action))


def test_only_the_orchestrator_records_the_rule_check_and_humans_only_see_checked_proposals(db):
    from backend.contracts.review import run_rule_check, shown_to_humans
    sit = ctx_for("situation", db)
    rec = sit.propose("assess_situation", good_output("situation", "assess_situation"))
    assert rec not in shown_to_humans(db, sit.run_id)                     # nobody sees it before the rule check
    for agent in AGENT_NAMES[1:]:
        with pytest.raises(ContractViolation):
            ctx_for(agent, db).record_rule_check(rec, {"result": "pass", "reasons": [], "rule_ids": [], "needs_human": None})
    orch = ctx_for("orchestrator", db)
    proposal = {"agent": "situation", "action": "assess_situation", "decision_ids": ["A01"], "data_as_of": NOW}
    assert run_rule_check(orch, rec, proposal, [], {"now": NOW})["result"] == "pass"
    assert db.execute("SELECT rule_check_ok, status FROM recommendations WHERE id=%s", (rec,)).fetchone() == (True, "pending")
    assert rec in shown_to_humans(db, sit.run_id)
    assert one(db, "SELECT count(*) FROM events WHERE run_id=%s AND type='rule_check_passed'", orch.run_id) >= 1
    blocked = sit.propose("assess_situation", good_output("situation", "assess_situation"))
    stale = {**proposal, "data_as_of": NOW - timedelta(hours=1)}
    assert run_rule_check(orch, blocked, stale, [], {"now": NOW})["result"] == "block"
    assert db.execute("SELECT rule_check_ok, status, rule_ids FROM recommendations WHERE id=%s", (blocked,)).fetchone() == \
        (False, "blocked_by_rule_check", ["missing_or_stale_data_blocks"])
    assert blocked not in shown_to_humans(db, sit.run_id)
    with pytest.raises(PermissionError):
        run_rule_check(ctx_for("situation", db), rec, proposal, [], {"now": NOW})


def test_only_the_orchestrator_can_open_an_incident(db):
    o = ctx_for("orchestrator", db)
    inc = o.write("incidents", {"kind": "weather", "station": "MCI", "title": "Storm", "status": "open", "opened_at": NOW,
                                "opened_by": "orchestrator", "run_id": o.run_id})
    ev = o.record_event("incident_opened", {"incident_id": inc}, station="MCI")
    o.write("incident_events", {"incident_id": inc, "event_id": ev})
    o.write("incidents", {"status": "monitoring"}, mode="update", row_id=inc)
    assert one(db, "SELECT status FROM incidents WHERE id=%s", inc) == "monitoring"
    for agent in AGENT_NAMES[1:]:
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
        for c in sorted(set(schema_tables()[t]) - set(cols))[:2]:
            with pytest.raises(ContractViolation, match="not in this agent's reads|name or contact"):
                ctx.read(t, [c])


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_agent_is_refused_every_write_to_a_truth_table(db, agent):
    ctx = ctx_for(agent, db)
    before = {t: one(db, f"SELECT count(*) FROM {t}") for t in ("flights", "bookings", "crews", "passengers", "staff", "aircraft")}
    for t in sorted(TRUTH_TABLES & set(schema_tables())):
        with pytest.raises(ContractViolation, match="truth table"):
            ctx.write(t, {"id": "x"}, mode="insert")
    with pytest.raises(ContractViolation, match="truth table"):
        ctx.write("flights", {"status": "cancelled"}, mode="update", row_id="WN1033")
    assert before == {t: one(db, f"SELECT count(*) FROM {t}") for t in before}


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_an_action_outside_the_contract_is_refused_and_human_only_ones_go_to_a_human(db, agent):
    c = CONTRACTS[agent]
    ctx = ctx_for(agent, db)
    refused = [a for a in RULES["autonomy"] if a not in c.actions]
    for a in refused[:8] + ["do_anything"]:
        with pytest.raises(ContractViolation, match="not an action|human-only"):
            ctx.propose(a, good_output(agent))
    for a in sorted(HUMAN_ONLY):
        with pytest.raises(ContractViolation, match="human-only"):
            ctx.propose(a, good_output(agent))
    mine = next(d for d in DECISIONS.values() if d["proposed_by"] == agent)
    ev = ctx.ask_human(mine["id"], {"flight_id": "WN1033"}, "unsure")
    payload = db.execute("SELECT payload FROM events WHERE id=%s", (ev,)).fetchone()[0]
    assert payload["decision_id"] == mine["id"] and payload["decided_by"] == mine["decided_by"]


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_output_that_does_not_match_the_schema_is_refused(db, agent):
    c = CONTRACTS[agent]
    ctx = ctx_for(agent, db)
    action = next(a for a in c.actions if c.enabled(a))          # an action switched on in the agent's current phase
    ready(ctx, action)
    good = good_output(agent, action)
    before = violations(db, agent)
    saved_before = one(db, "SELECT count(*) FROM recommendations WHERE run_id=%s", ctx.run_id)
    for field in c.output["required"]:
        with pytest.raises(ContractViolation, match="missing"):
            ctx.propose(action, {k: v for k, v in good.items() if k != field})
    with pytest.raises(ContractViolation, match="not in the contract"):
        ctx.propose(action, {**good, "sneaky_extra_field": 1})
    with pytest.raises(ContractViolation, match="must be"):
        ctx.propose(action, {**good, "title": 123})
    assert violations(db, agent) - before == len(c.output["required"]) + 2
    assert one(db, "SELECT count(*) FROM recommendations WHERE run_id=%s", ctx.run_id) == saved_before


def test_a_refusal_is_a_clear_error_an_event_and_an_audit_row(db):
    ctx = ctx_for("situation", db)
    with pytest.raises(ContractViolation) as e:
        ctx.read("hotels", ["name"])
    assert e.value.agent == "situation" and e.value.kind == "read" and "hotels" in e.value.detail
    row = db.execute("SELECT actor, payload FROM events WHERE type='contract_violation' AND run_id=%s ORDER BY id DESC LIMIT 1",
                     (ctx.run_id,)).fetchone()
    assert row[0] == "system" and row[1]["agent"] == "situation" and row[1]["kind"] == "read"
    assert one(db, "SELECT count(*) FROM audit_log WHERE run_id=%s AND what='contract violation refused'", ctx.run_id) >= 1


# ------------------------------------------------------------------ names are hidden

def test_passenger_names_are_hidden_from_agents_that_may_not_see_them_and_scoped_for_those_that_may(db):
    for agent in AGENT_NAMES:
        ctx = ctx_for(agent, db)
        if agent == "communication" and CONTRACTS[agent].phase in ("4b-1", "4b-2"):
            ctx.working_on(["PAX0001"])                    # even for a passenger it says it works on: no names before 4b-3
            for col in ("full_name", "booking_ref", "contact_channels"):
                with pytest.raises(ContractViolation):
                    ctx.read("passengers", ["id", col], where={"id": "PAX0001"})
        elif agent in PII_AGENTS:
            with pytest.raises(ContractViolation, match="working on"):
                ctx.read("passengers", ["id", "full_name"], where={"id": "PAX0001"})          # hasn't said who it works on
            ctx.working_on(["PAX0001"])
            assert ctx.read("passengers", ["id", "full_name"], where={"id": "PAX0001"})[0]["full_name"] == "Maya Torres"
            with pytest.raises(ContractViolation, match="working on"):
                ctx.read("passengers", ["full_name"], where={"id": "PAX0002"})                # someone else
            with pytest.raises(ContractViolation, match="working on"):
                ctx.read("passengers", ["full_name"])                                         # everybody
            plain = [c for c in CONTRACTS[agent].reads["passengers"] if c not in ("full_name", "booking_ref", "contact_channels")]
            assert ctx.read("passengers", plain, limit=3)                                   # the other columns are fine
        else:
            for col in ("full_name", "booking_ref", "contact_channels"):
                with pytest.raises(ContractViolation):
                    ctx.read("passengers", [col])


def test_network_recovery_is_refused_passenger_names_and_truth_writes(db):
    ctx = ctx_for("network_recovery", db)
    with pytest.raises(ContractViolation):
        ctx.read("passengers", ["full_name"])
    with pytest.raises(ContractViolation, match="truth table"):
        ctx.write("crews", {"pilot_duty_left_min": 999}, mode="update", row_id="C33")
    with pytest.raises(ContractViolation, match="truth table"):
        ctx.write("flights", {"crew_id": "R01"}, mode="update", row_id="WN1033")


# ------------------------------------------------------------------ stale data

def test_stale_data_is_refused_and_fresh_data_is_accepted(db):
    # an agent that reads a LIVE table (station_staff reads stations with their as_of time). Since 4d/4e Network Recovery and
    # Passenger Care read only data published before the day plus events; their freshness is the feeds' heartbeats.
    agent, action = "station_staff", "adjust_checkin_cutoffs"
    limit = CONTRACTS[agent].max_data_age_sec
    probe = ctx_for(agent, db)
    probe.read("stations", ["code"], limit=1)
    as_of = probe.oldest_as_of
    out = good_output(agent, action)
    fresh = ctx_for(agent, db, now=as_of + timedelta(seconds=limit))
    fresh.read("stations", ["code"], limit=1)
    assert fresh.propose(action, out)
    stale = ctx_for(agent, db, now=as_of + timedelta(seconds=limit + 1))
    stale.read("stations", ["code"], limit=1)
    with pytest.raises(StaleDataError, match="old"):
        stale.propose(action, out)


def test_temporary_notes_are_deleted_when_the_incident_closes(db):
    ctx = ctx_for("situation", db)
    ctx.remember("guess", {"cause": "weather"})
    assert ctx.close_incident() == 1 and ctx.recall("guess") is None


def test_the_script_prints_a_summary_of_all_six(capsys):
    from scripts.contracts import main
    main()
    out = capsys.readouterr().out
    for agent in AGENT_NAMES:
        assert agent in out
    assert "no guardrail agent" in out
