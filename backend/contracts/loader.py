"""Read config/agents/*.yaml and refuse to start if any contract is incomplete or lets an agent do something it must never do."""
import re
from functools import lru_cache
from pathlib import Path

import yaml

from backend.contracts.jsonschema_lite import check_schema
from backend.settings import ROOT, SCHEMA

AGENTS_DIR = ROOT / "config" / "agents"
# The agreed setup: the orchestrator (plain code, no AI) + five agents. There is no guardrail agent and no separate crew agent:
# rule checking is plain code in backend/rules/check.py, run by the orchestrator.
AGENT_NAMES = ("orchestrator", "situation", "network_recovery", "station_staff", "passenger_care", "communication")
AGENTS = AGENT_NAMES[1:]
RULE_CHECK_COLUMNS = {"rule_check_ok", "rule_check_notes", "rule_ids"}   # only the orchestrator writes these

# The facts of the world. No agent may ever write to these directly: a truth change happens only through an approved action.
TRUTH_TABLES = {
    "stations", "aircraft", "crews", "flights", "passenger_groups", "passengers", "bookings", "bags", "hotels", "staff",
    "later_capacity", "route_tiers", "transport_options", "hotel_rate_tiers", "channel_views", "system_health",
    "decisions", "actions", "metric_runs", "data_issues", "hotel_assignments", "gate_assignments", "staff_assignments",
}
# The only tables an agent can write to: proposals, its own events, the audit log, and (orchestrator only) incidents.
WRITABLE_TABLES = {"recommendations", "events", "audit_log", "incidents", "incident_events", "situation_assessments", "mismatches",
                   "recovery_plans", "care_plans", "station_plans"}
OWN_TABLES = {"situation_assessments": {"situation"},      # shared state: only its owner writes it
              "recovery_plans": {"network_recovery"},      # shared state (Phase 4d): the Network Recovery agent's plan
              "care_plans": {"passenger_care"},            # shared state (Phase 4e): the Passenger Care agent's plan
              "station_plans": {"station_staff"},          # shared state (Phase 4f): the Station & Staff agent's plan
              "mismatches": {"communication"}}             # what the channels show wrongly: only communication records it
# Build phases in order. A contract may name, per action, the phase it is switched on in (capabilities: action -> enabled_in);
# AgentContext refuses an action whose phase has not come yet.
PHASES = ("4b-1", "4b-2", "4b-3", "4c", "4d", "4e", "4f", "later")   # "later": defined in the contract, not built yet
# Derived lookups an agent may ask AgentContext for, by name. Each reveals channel TYPES only (never an address, name or ref).
PROJECTIONS = {"passenger_screens": "which of the four screens each passenger has (app/SMS only if on file)",
               "contact_channel_types": "which supported channel types one passenger has (app, sms, email)"}
INCIDENT_TABLES = {"incidents", "incident_events"}
INCIDENT_WRITERS = {"orchestrator"}
WRITE_MODES = {"insert", "update", "insert_or_update"}

# Names and contact details. Only these two agents may read them; everyone else sees passenger ids only.
PII_COLUMNS = {"passengers": {"full_name", "booking_ref", "contact_channels"}, "channel_views": {"message"}}
PII_AGENTS = {"passenger_care", "communication"}
# A language model may only help with situation summaries and message drafts.
AI_AGENTS = {"situation", "communication"}

REQUIRED_FIELDS = ("agent", "purpose", "reads", "writes", "actions", "never", "stores", "output", "max_data_age_sec",
                   "time_budget_sec", "escalate_when", "ai_allowed", "success")
OUTPUT_FIELDS = ("title", "why", "data_as_of", "options", "decision_ids", "sources")   # every output has these


class ContractError(ValueError):
    pass


@lru_cache
def schema_tables() -> dict[str, list[str]]:
    """table -> its columns, read from backend/db/schema.sql."""
    text = re.sub(r"--[^\n]*", "", SCHEMA.read_text(encoding="utf-8"))
    tables = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", text, flags=re.S):
        cols = []
        for line in m.group(2).split("\n"):
            word = line.strip().split(" ")[0].strip(",")
            if word and word.upper() not in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT") and re.fullmatch(r"\w+", word):
                cols.append(word)
        tables[m.group(1)] = cols
    return tables


class Contract:
    def __init__(self, data: dict):
        self.data = data
        self.agent = data["agent"]
        self.purpose = data["purpose"]
        self.reads: dict[str, list[str]] = data["reads"]
        self.writes: dict[str, dict] = data["writes"]
        self.actions: list[str] = data["actions"]
        self.output: dict = data["output"]
        self.max_data_age_sec: int = data["max_data_age_sec"]
        self.time_budget_sec: int = data["time_budget_sec"]
        self.ai_allowed: bool = data["ai_allowed"]["allowed"]
        self.required_checks: dict = data.get("required_checks") or {}
        self.static_reads: set = set(data.get("static_reads") or [])
        self.event_feeds: dict | None = data.get("event_feeds")
        # event type -> (feed, payload fields it may see). Keys starting with "_" describe the feed itself, not an event type.
        self.event_types: dict = {t: (feed, fields) for feed, types in (self.event_feeds or {}).items() for t, fields in types.items()
                                  if not t.startswith("_")}
        # feed -> (payload field, allowed values): one feed per value of that field (e.g. one per channel), each with its own heartbeat
        self.feed_split: dict = {feed: (types["_split_by"], list(types["_feeds"])) for feed, types in (self.event_feeds or {}).items()
                                 if "_split_by" in types}
        self.phase: str | None = data.get("phase")
        self.enabled_in: dict = {c["action"]: c["enabled_in"] for c in data.get("capabilities") or [] if c.get("action")}
        self.projections: dict = dict(data.get("projections") or {})            # name -> phase it is available from
        self.shared_reads: dict = dict(data.get("shared_reads") or {})          # shared table -> {producer, fields, ...}

    def projection_ok(self, name: str) -> bool:
        if name not in self.projections:
            return False
        return not self.phase or PHASES.index(self.projections[name]) <= PHASES.index(self.phase)

    def enabled(self, action: str) -> bool:
        """Is this action switched on in the contract's current phase? (No phase or no entry: always.)"""
        if not self.phase or action not in self.enabled_in:
            return True
        return PHASES.index(self.enabled_in[action]) <= PHASES.index(self.phase)

    def __getitem__(self, key):
        return self.data[key]

    def can_see_names(self) -> bool:
        return self.agent in PII_AGENTS


def validate_contract(data: dict, rules: dict | None = None, tables: dict | None = None) -> Contract:
    """Raise ContractError, in plain words, if the contract is missing something or allows something it must not."""
    if not isinstance(data, dict):
        raise ContractError("a contract must be a mapping")
    for f in REQUIRED_FIELDS:
        if f not in data or data[f] in (None, "", [], {}) and f not in ("actions", "reads"):
            raise ContractError(f"contract {data.get('agent', '?')} is missing '{f}'")
    name = data["agent"]
    if name not in AGENT_NAMES:
        raise ContractError(f"unknown agent '{name}'")
    if rules is None:
        from backend.rules import load_rules
        rules = load_rules()
    tables = tables or schema_tables()

    if not isinstance(data["reads"], dict) or not data["reads"]:
        raise ContractError(f"{name}: reads must list at least one table and its columns")
    for table, cols in data["reads"].items():
        if table not in tables:
            raise ContractError(f"{name}: reads unknown table '{table}'")
        for c in cols or []:
            if c not in tables[table]:
                raise ContractError(f"{name}: reads unknown column {table}.{c}")
            if c in PII_COLUMNS.get(table, ()) and name not in PII_AGENTS:
                raise ContractError(f"{name}: may not read {table}.{c} (names and contact details are only for {sorted(PII_AGENTS)})")

    for proj, phase in (data.get("projections") or {}).items():
        if proj not in PROJECTIONS:
            raise ContractError(f"{name}: unknown projection '{proj}'")
        if phase not in PHASES:
            raise ContractError(f"{name}: projection '{proj}' names unknown phase '{phase}'")

    from backend.contracts.shared_state import SharedStateError, validate_shared_reads
    try:
        validate_shared_reads(name, data.get("shared_reads"), tables)
    except SharedStateError as err:
        raise ContractError(str(err)) from None

    writes = data["writes"]
    if not isinstance(writes, dict) or not writes:
        raise ContractError(f"{name}: writes must list what it may write")
    for table, w in writes.items():
        if table in TRUTH_TABLES:
            raise ContractError(f"{name}: may not write to '{table}'; truth changes happen only through an approved action")
        if table not in WRITABLE_TABLES:
            raise ContractError(f"{name}: writes unknown table '{table}'")
        if table in INCIDENT_TABLES and name not in INCIDENT_WRITERS:
            raise ContractError(f"{name}: only the orchestrator writes incidents")
        if table in OWN_TABLES and name not in OWN_TABLES[table]:
            raise ContractError(f"{name}: only {sorted(OWN_TABLES[table])} writes {table}")
        if w.get("mode") not in WRITE_MODES:
            raise ContractError(f"{name}: writes.{table}.mode must be one of {sorted(WRITE_MODES)}")
        for c in w.get("columns", []):
            if c not in tables[table]:
                raise ContractError(f"{name}: writes unknown column {table}.{c}")
        if table == "recommendations" and RULE_CHECK_COLUMNS & set(w.get("columns", [])) and name != "orchestrator":
            raise ContractError(f"{name}: only the orchestrator records the rule check")
        if w.get("mode") in ("update", "insert_or_update") and table == "recommendations" and name != "orchestrator":
            raise ContractError(f"{name}: only the orchestrator may update a recommendation (to record the rule check)")
        if table == "events" and not w.get("types"):
            raise ContractError(f"{name}: writes.events needs the list of event types it may write")
    if "events" not in writes or "audit_log" not in writes:
        raise ContractError(f"{name}: every agent must be able to write its own events and the audit log")

    for t in data.get("static_reads") or []:
        if t not in data["reads"]:
            raise ContractError(f"{name}: static_reads names '{t}', which it doesn't read")
    feeds = data.get("event_feeds")
    if feeds is not None:
        if "events" not in data["reads"]:
            raise ContractError(f"{name}: event_feeds needs events in reads")
        seen = set()
        for feed, types in feeds.items():
            if "_split_by" in types and not types.get("_feeds"):
                raise ContractError(f"{name}: event_feeds.{feed} splits by '{types['_split_by']}' but lists no _feeds")
            for t, fields in types.items():
                if t.startswith("_"):
                    continue
                if t in seen:
                    raise ContractError(f"{name}: event type '{t}' is listed under two feeds")
                if not isinstance(fields, list):
                    raise ContractError(f"{name}: event_feeds.{feed}.{t} must list the payload fields it may see")
                seen.add(t)
    if data.get("phase") is not None and data["phase"] not in PHASES:
        raise ContractError(f"{name}: phase must be one of {PHASES}")
    for cap in data.get("capabilities") or []:
        if cap.get("enabled_in") not in PHASES:
            raise ContractError(f"{name}: capability {cap.get('action') or cap.get('decision')} needs enabled_in, one of {PHASES}")
        if cap.get("action") and cap["action"] not in data["actions"]:
            raise ContractError(f"{name}: capability names action '{cap['action']}', which is not in its actions")
    unknown = [a for a in data["actions"] if a not in rules["autonomy"]]
    if unknown:
        raise ContractError(f"{name}: actions not in the autonomy rules: {unknown}")
    human = [a for a in data["actions"] if rules["autonomy"][a]["level"] == "human_only"]
    if human:
        raise ContractError(f"{name}: may not propose human-only decisions {human}; it can only show facts (ask_human)")
    from backend.rules.safety import CHECKS
    for action, names in (data.get("required_checks") or {}).items():
        if action not in data["actions"]:
            raise ContractError(f"{name}: required_checks names '{action}', which it can't propose")
        bad = [n for n in names if n not in CHECKS]
        if bad:
            raise ContractError(f"{name}: required_checks names unknown checks {bad}")

    if not isinstance(data["never"], list) or not all(isinstance(x, str) and x.strip() for x in data["never"]):
        raise ContractError(f"{name}: never must be a list of plain-English sentences")

    stores = data["stores"]
    if not isinstance(stores, dict) or not stores.get("temporary") or not stores.get("permanent"):
        raise ContractError(f"{name}: stores needs both 'temporary' and 'permanent'")
    for key in ("what", "kept_until"):
        if not stores["temporary"].get(key):
            raise ContractError(f"{name}: stores.temporary needs '{key}'")
    for item in stores["permanent"]:
        if not item.get("table") or not item.get("kept"):
            raise ContractError(f"{name}: each permanent store needs a 'table' and how long it is 'kept'")

    out = data["output"]
    problems = check_schema(out)
    if problems:
        raise ContractError(f"{name}: " + "; ".join(problems))
    if out.get("type") != "object" or out.get("additionalProperties") is not False:
        raise ContractError(f"{name}: output must be an object with additionalProperties: false (the exact fields)")
    for f in OUTPUT_FIELDS:
        if f not in out.get("required", []) or f not in out.get("properties", {}):
            raise ContractError(f"{name}: output must require '{f}'")

    for k in ("max_data_age_sec", "time_budget_sec"):
        if not isinstance(data[k], int) or isinstance(data[k], bool) or data[k] <= 0:
            raise ContractError(f"{name}: {k} must be a positive whole number")
    if not isinstance(data["escalate_when"], list) or not data["escalate_when"]:
        raise ContractError(f"{name}: escalate_when must list when it hands over to a human")

    ai = data["ai_allowed"]
    if not isinstance(ai, dict) or not isinstance(ai.get("allowed"), bool):
        raise ContractError(f"{name}: ai_allowed.allowed must be true or false")
    if ai["allowed"]:
        if name not in AI_AGENTS:
            raise ContractError(f"{name}: a language model may only be used by {sorted(AI_AGENTS)}")
        if not ai.get("for") or not ai.get("fallback"):
            raise ContractError(f"{name}: ai_allowed needs what it is for and a template fallback")

    if not isinstance(data["success"], list) or not data["success"] or not all(s.get("metric") and "target" in s for s in data["success"]):
        raise ContractError(f"{name}: success needs at least one metric with a target")
    return Contract(data)


def load_contract_file(path: Path, rules: dict | None = None) -> Contract:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    contract = validate_contract(data, rules)
    if Path(path).stem != contract.agent:
        raise ContractError(f"{Path(path).name} describes agent '{contract.agent}'; the file name must match")
    return contract


@lru_cache
def load_contracts(directory: Path = AGENTS_DIR) -> dict[str, Contract]:
    """All seven contracts, by agent name. Fails if one is missing, broken, or unexpected."""
    found = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        if path.name.startswith("_"):          # _shared.yaml: the rules every agent follows, not a contract
            continue
        c = load_contract_file(path)
        found[c.agent] = c
    missing = [a for a in AGENT_NAMES if a not in found]
    if missing:
        raise ContractError(f"missing contract files for: {missing}")
    return found
