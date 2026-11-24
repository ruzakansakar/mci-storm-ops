"""The decision catalog (config/decisions.yaml)."""
import collections

import pytest

from backend.contracts import load_contracts
from backend.contracts.decisions import DECISION_FIELDS, load_catalog, load_decisions
from backend.rules import load_rules
from backend.rules.autonomy import autonomy_for

D = load_decisions()
R = load_rules()
RULE_IDS = {e["id"] for e in R["entries"]}
CONTRACTS = load_contracts()
AGENTS = {"orchestrator", "situation", "network_recovery", "station_staff", "passenger_care", "communication"}
ASKED = ("A01-A10 B01-B14 C01-C08 D01-D13 E01-E12 F01-F10 G01-G10 H01-H06 I01-I06 J01-J10 K01-K10 L01-L08 M01-M10 N01-N08 "
         "O01-O12 P01-P08 Q01-Q08 R01-R05 S01-S06 T01-T05")


def asked_ids():
    out = []
    for span in ASKED.split():
        a, b = span.split("-")
        out += [f"{a[0]}{n:02d}" for n in range(int(a[1:]), int(b[1:]) + 1)]
    return out


def test_every_requested_decision_is_there_and_there_are_at_least_150():
    missing = [i for i in asked_ids() if i not in D]
    assert not missing, missing
    assert len(D) >= 150


@pytest.mark.parametrize("did", sorted(D))
def test_every_decision_has_every_field(did):
    d = D[did]
    for f in DECISION_FIELDS:
        assert f in d and d[f] not in (None, ""), (did, f)
    assert d["status"] == "defined"
    assert d["checked_by"][0] == "orchestrator rule check"
    assert d["safety_rung"] == "none" or d["safety_rung"] in range(1, 9)
    assert isinstance(d["in_story"], bool) and isinstance(d["rules"], list) and d["data_needed"] and d["affects"]


def test_every_rule_agent_and_action_named_exists():
    for d in D.values():
        assert d["proposed_by"] in AGENTS, d["id"]
        assert d["action"] in R["autonomy"], d["id"]
        for r in d["rules"] + [c for c in d["checked_by"][1:]]:
            assert r in RULE_IDS, (d["id"], r)


def test_decided_by_matches_the_autonomy_rules():
    for d in D.values():
        from scripts.build_decisions import owner_for
        a = autonomy_for(d["action"], R, safety_rung=None if d["safety_rung"] == "none" else d["safety_rung"], owner=owner_for(d["id"]))
        assert d["decided_by"]["level"] == a["level"], d["id"]
        if a["level"] != "auto":
            assert d["decided_by"]["role"] == a["approver"], d["id"]


HUMAN_ONLY_ROLES = {v["approver"] for v in R["autonomy"].values() if v["level"] == "human_only"}
FAMILY_ROLES = {
    "B": {"network_operations_control"}, "C": {"maintenance_control"}, "D": {"crew_scheduling"},
    "E": {"station_manager", "ramp_supervisor"}, "G": {"station_manager", "ramp_supervisor"},
    "K": {"customer_service_supervisor"}, "M": {"customer_service_supervisor"}, "N": {"customer_service_supervisor"},
    "J": {"customer_service_supervisor"}, "O": {"customer_service_supervisor"},
}
EXCEPTIONS = {"F09": {"station_manager", "ramp_supervisor"}, "J04": {"station_manager", "ramp_supervisor"},
              "J08": {"station_manager", "ramp_supervisor"},
              # Phase 4f: the station manager approves the staff member who supervises a child alone (autonomy.yaml approver)
              "J01": {"station_manager"}, "J03": {"station_manager"}, "Q02": {"network_operations_control"}, "Q04": {"network_operations_control"},
              "O10": {"corporate_communications"}}


def test_no_crew_decision_is_approved_by_the_station_manager():
    assert not [d["id"] for d in D.values() if d["id"].startswith("D") and d["decided_by"].get("role") == "station_manager"]
    for did in ("D01", "D05", "D09", "D14", "D15"):
        assert D[did]["decided_by"] == {"level": "needs_approval", "role": "crew_scheduling"}, did
    assert D["D02"]["decided_by"] == {"level": "human_only", "role": "captain_and_crew_scheduling"}


def test_aircraft_decisions_are_approved_by_maintenance_control_only():
    for d in D.values():
        if d["id"].startswith("C") and d["decided_by"]["level"] != "auto":
            role = d["decided_by"]["role"]
            assert role == "maintenance_control" or (d["decided_by"]["level"] == "human_only" and role in HUMAN_ONLY_ROLES), d["id"]
    assert D["C02"]["decided_by"]["role"] == "maintenance_control"


def test_every_non_auto_decision_has_an_approver_that_matches_its_family():
    for d in D.values():
        if d["decided_by"]["level"] != "needs_approval":
            continue                                    # human-only ones keep their role from autonomy.yaml (diversion, extension...)
        allowed = EXCEPTIONS.get(d["id"]) or FAMILY_ROLES.get(d["id"][0])
        if allowed:
            assert d["decided_by"]["role"] in allowed, (d["id"], d["decided_by"])
    for d in D.values():
        if d["decided_by"]["level"] == "human_only":
            assert d["decided_by"]["role"] in HUMAN_ONLY_ROLES, d["id"]
    assert D["B08"]["decided_by"] == {"level": "human_only", "role": "dispatcher_and_captain"}
    assert D["C08"]["decided_by"] == {"level": "human_only", "role": "maintenance_control"}
    assert D["D07"]["decided_by"] == {"level": "human_only", "role": "crew_scheduling"}


def test_no_role_approves_more_than_40_percent_of_decisions():
    counts = collections.Counter(d["decided_by"]["role"] for d in D.values() if d["decided_by"]["level"] != "auto")
    assert max(counts.values()) <= 0.4 * len(D), counts


def test_an_auto_action_with_no_owner_fails_loudly_instead_of_guessing():
    import copy
    import pytest as _pytest
    from backend.rules.autonomy import NoOwnerRole
    from backend.rules.loader import RulesError, validate
    rules = copy.deepcopy(R)
    rules["autonomy"]["flag_crew_legality"] = {"level": "auto"}
    with _pytest.raises(NoOwnerRole):
        autonomy_for("flag_crew_legality", rules, safety_rung=4)
    with _pytest.raises(RulesError, match="owner"):
        validate(rules)
    assert autonomy_for("flag_crew_legality", R, safety_rung=4)["approver"] == "crew_scheduling"      # the action's own owner
    assert autonomy_for("flag_crew_legality", R, safety_rung=4, owner="maintenance_control")["approver"] == "maintenance_control"
    assert all(v.get("owner") for v in R["autonomy"].values() if v["level"] == "auto")
    import inspect
    from backend.rules import autonomy
    assert '"approver": "station_manager"}' not in inspect.getsource(autonomy.autonomy_for)


def test_each_of_the_five_agents_and_the_orchestrator_proposes_at_least_one():
    counts = collections.Counter(d["proposed_by"] for d in D.values())
    assert set(counts) == AGENTS and min(counts.values()) >= 1


def test_all_crew_decisions_are_proposed_by_network_recovery():
    assert all(d["proposed_by"] == "network_recovery" for d in D.values() if d["id"].startswith("D"))


def test_the_proposing_agent_may_propose_the_action_unless_it_is_human_only():
    for d in D.values():
        if d["decided_by"]["level"] != "human_only":
            assert d["action"] in CONTRACTS[d["proposed_by"]].actions, d["id"]
        else:
            assert d["action"] not in CONTRACTS[d["proposed_by"]].actions, d["id"]     # it can only ask_human


def test_no_decision_on_rungs_1_to_4_is_automatic():
    bad = [d["id"] for d in D.values() if d["safety_rung"] in (1, 2, 3, 4) and d["decided_by"]["level"] == "auto"]
    assert not bad


def test_these_are_human_only():
    for did in ("B08", "C01", "C08", "D02", "D07", "E01", "E02", "F07", "O10", "T01"):
        assert D[did]["decided_by"]["level"] == "human_only", did
        assert D[did]["decided_by"]["role"]
    assert D["D02"]["decided_by"]["role"] == "captain_and_crew_scheduling"
    assert D["F07"]["decided_by"]["role"] == "captain"


def test_every_enforced_rule_is_used_by_at_least_one_decision():
    used = {r for d in D.values() for r in d["rules"] + d["checked_by"]}
    unused = [e["id"] for e in R["entries"] if e["enforced"] and e["id"] not in used]
    assert not unused, unused


def test_in_story_matches_the_simulator_events_of_seed_7(data):
    from simulator.run import simulate
    story = {e["type"] for e in simulate(7, "a31_storm", data=data).world.events}
    for d in D.values():
        assert d["in_story"] == (d["simulator_event"] != "missing" and d["simulator_event"] in story), d["id"]
    assert sum(d["in_story"] for d in D.values()) > 50


def test_the_gaps_list_is_complete():
    gaps = load_catalog()["gaps"]
    assert set(gaps["decisions_the_simulator_cannot_trigger"]) == {d["id"] for d in D.values() if d["simulator_event"] == "missing"}
    for rid in gaps["rules_added_as_assumptions"]:
        assert next(e for e in R["entries"] if e["id"] == rid)["source"] == "assumption"


def test_the_script_prints_the_summary(capsys):
    from scripts.decisions import main
    main()
    out = capsys.readouterr().out
    for word in ("PER FAMILY", "PER LEVEL", "PER AGENT", "IN THE A31_STORM STORY", "GAPS"):
        assert word in out


@pytest.fixture(scope="module")
def data():
    from backend.seed.build import build
    return build()
