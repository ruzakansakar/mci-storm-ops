"""AgentContext: the only way an agent (or the orchestrator) reads or writes the database.

It enforces the agent's contract and the shared AI guardrails (config/agents/_shared.yaml). Anything outside the contract raises a
ContractViolation (a clear error in plain English) AND writes a "contract_violation" event plus an audit row, so the breach is on the record.
  * reads: only the tables and columns in the contract; names and contacts only for passenger_care and communication, and only for the
    passengers they said they are working on;
  * writes: only what the contract allows; never a truth table; incidents and rule-check results only for the orchestrator;
    every write also goes to the audit log;
  * actions: only those in the contract, never a human-only one (the agent can only ask_human);
  * output: must match the schema, name its decision ids, and give a source and as_of for its facts;
  * data age: no proposal on data older than max_data_age_sec;
  * required checks: an agent must run the checks its contract names before proposing certain actions (they never count as final).
"""
import re
from datetime import datetime

from psycopg import sql
from psycopg.types.json import Jsonb

from backend.contracts import jsonschema_lite
from backend.contracts.decisions import load_decisions
from backend.contracts.loader import (INCIDENT_TABLES, INCIDENT_WRITERS, PII_AGENTS, PII_COLUMNS, TRUTH_TABLES, Contract,
                                      load_contracts, schema_tables)
from backend.rules import load_rules
from backend.rules.autonomy import autonomy_for
from backend.rules.safety import CHECKS, limits_for

PII_KEY = {"passengers": "id", "channel_views": "passenger_id"}


class ContractViolation(Exception):
    """An agent tried something its contract does not allow."""

    def __init__(self, agent: str, kind: str, detail: str):
        super().__init__(f"{agent} broke its contract ({kind}): {detail}")
        self.agent, self.kind, self.detail = agent, kind, detail


class StaleDataError(ContractViolation):
    """The data behind a recommendation is older than the agent is allowed to use."""


class AgentContext:
    def __init__(self, agent: str, cur, now: datetime, run_id: str | None = None, incident_id: int | None = None,
                 contracts: dict[str, Contract] | None = None, rules: dict | None = None):
        self.contracts = contracts or load_contracts()
        if agent not in self.contracts:
            raise ContractViolation(agent, "unknown agent", "there is no contract for this agent")
        self.agent, self.contract = agent, self.contracts[agent]
        self.cur, self.now, self.run_id, self.incident_id = cur, now, run_id, incident_id
        self.rules = rules or load_rules()
        self.tables = schema_tables()
        self.oldest_as_of: datetime | None = None          # the oldest "as of" time among everything this agent has read
        self.violations: list[ContractViolation] = []
        self.scope: set[str] = set()                       # passenger ids it is working on (for names and contacts)
        self.checks_run: set[str] = set()                  # rule checks it ran itself (required before some actions; never final)
        self._temp: dict = {}                              # temporary notes: deleted when the incident closes

    # ------------------------------------------------------------------ the record of a breach
    def _violate(self, kind: str, detail: str, error=ContractViolation):
        err = error(self.agent, kind, detail)
        self.violations.append(err)
        payload = {"agent": self.agent, "kind": kind, "detail": detail, "incident_id": self.incident_id}
        self.cur.execute(
            "INSERT INTO events (occurred_at, type, actor, actor_name, station, payload, run_id) "
            "VALUES (%s, 'contract_violation', 'system', 'contract guard', NULL, %s, %s)", (self.now, Jsonb(payload), self.run_id))
        self._audit_row("contract violation refused", "agent", self.agent, None, payload)
        raise err

    def _audit_row(self, what, entity_type, entity_id, before, after):
        self.cur.execute(
            "INSERT INTO audit_log (at, actor, actor_name, what, entity_type, entity_id, before, after, run_id) "
            "VALUES (%s, 'agent', %s, %s, %s, %s, %s, %s, %s)",
            (self.now, self.agent, what, entity_type, str(entity_id), _json(before), _json(after), self.run_id))

    # ------------------------------------------------------------------ reading
    def working_on(self, passenger_ids) -> None:
        """Say which passengers this agent is working on. Names and contacts can only be read for these."""
        self.scope = set(passenger_ids)

    def read(self, table: str, columns: list[str] | None = None, where: dict | None = None, limit: int | None = None,
             order_by: str | None = None) -> list[dict]:
        allowed = self.contract.reads.get(table)
        if allowed is None:
            self._violate("read", f"table '{table}' is not in this agent's reads")
        columns = list(columns or allowed)
        for c in columns + list((where or {}).keys()) + ([order_by] if order_by else []):
            if c not in allowed:
                if c in PII_COLUMNS.get(table, ()) and self.agent not in PII_AGENTS:
                    self._violate("read", f"{table}.{c} is a name or contact detail; this agent sees passenger ids only")
                self._violate("read", f"column '{table}.{c}' is not in this agent's reads")
        pii = [c for c in columns if c in PII_COLUMNS.get(table, ())]
        if pii:
            key = PII_KEY[table]
            wanted = (where or {}).get(key)
            wanted = set(wanted) if isinstance(wanted, (list, tuple, set)) else ({wanted} if wanted is not None else None)
            if not wanted or not wanted <= self.scope:
                self._violate("read", f"names and contacts ({', '.join(pii)}) can only be read for the passengers it is working on; "
                                      f"call working_on() and filter by {key}")
        has_as_of = "as_of" in self.tables[table]
        select = columns + (["as_of"] if has_as_of and "as_of" not in columns else [])
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(map(sql.Identifier, select)), sql.Identifier(table))
        params = []
        if where:
            parts = []
            for k, v in where.items():
                if isinstance(v, (list, tuple, set)):
                    parts.append(sql.SQL("{} = ANY(%s)").format(sql.Identifier(k)))
                    params.append(list(v))
                else:
                    parts.append(sql.SQL("{} = %s").format(sql.Identifier(k)))
                    params.append(v)
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts)
        if order_by:
            query += sql.SQL(" ORDER BY {}").format(sql.Identifier(order_by))
        if limit:
            query += sql.SQL(" LIMIT {}").format(sql.Literal(int(limit)))
        self.cur.execute(query, params)
        names = [d.name for d in self.cur.description]
        rows = [dict(zip(names, r)) for r in self.cur.fetchall()]
        for r in rows:
            as_of = r.get("as_of")
            if isinstance(as_of, datetime) and (self.oldest_as_of is None or as_of < self.oldest_as_of):
                self.oldest_as_of = as_of
            if has_as_of and "as_of" not in columns:
                r.pop("as_of", None)
        return rows

    def data_age_sec(self) -> float | None:
        return None if self.oldest_as_of is None else (self.now - self.oldest_as_of).total_seconds()

    def check_fresh(self):
        age = self.data_age_sec()
        if age is not None and age > self.contract.max_data_age_sec:
            self._violate("stale data", f"its data is {age:.0f} s old and it may use data up to {self.contract.max_data_age_sec} s old",
                          error=StaleDataError)

    # ------------------------------------------------------------------ its own checks (required first; never final)
    def run_check(self, name: str, option: dict):
        """Run one rule check on an option (e.g. crew legality before a delay). The result helps the agent; it is never final."""
        if name not in CHECKS:
            self._violate("check", f"there is no rule check named '{name}'")
        self.checks_run.add(name)
        return CHECKS[name](option, limits_for(self.rules))

    # ------------------------------------------------------------------ writing
    def write(self, table: str, values: dict, mode: str = "insert", row_id=None):
        """Insert (or update one row of) a table the contract allows. Every write also goes to the audit log."""
        if table in TRUTH_TABLES:
            self._violate("write", f"'{table}' is a truth table; agents never write to it (a human-approved action does)")
        w = self.contract.writes.get(table)
        if w is None:
            self._violate("write", f"table '{table}' is not in this agent's writes")
        if table in INCIDENT_TABLES and self.agent not in INCIDENT_WRITERS:
            self._violate("write", "only the orchestrator writes incidents")
        if w["mode"] != "insert_or_update" and w["mode"] != mode:
            self._violate("write", f"this agent may only {w['mode']} on '{table}', not {mode}")
        allowed = w.get("columns")
        if allowed is not None:
            bad = [c for c in values if c not in allowed]
            if bad:
                self._violate("write", f"columns {bad} of '{table}' are not in this agent's writes")
        if table == "events" and values.get("type") not in w["types"]:
            self._violate("write", f"event type '{values.get('type')}' is not one this agent may write ({w['types']})")
        if table == "recommendations" and mode == "update":
            status = values.get("status")
            if status is not None and status not in w.get("status_values", []):
                self._violate("write", f"status '{status}' is not one this agent may set")
        cols = list(values)
        if mode == "insert":
            q = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(sql.Placeholder() * len(cols)))
            has_id = "id" in self.tables[table]
            if has_id:
                q += sql.SQL(" RETURNING id")
            self.cur.execute(q, [_adapt(c, v) for c, v in values.items()])
            new_id = self.cur.fetchone()[0] if has_id else None
            if table != "audit_log":
                self._audit_row(f"{self.agent} wrote {table}", table, new_id if new_id is not None else "-", None, _safe(values))
            return new_id
        if mode == "update":
            if row_id is None:
                self._violate("write", "an update needs the id of the row")
            q = sql.SQL("UPDATE {} SET {} WHERE id = %s").format(
                sql.Identifier(table), sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in cols))
            self.cur.execute(q, [_adapt(c, v) for c, v in values.items()] + [row_id])
            self._audit_row(f"{self.agent} updated {table}", table, row_id, None, _safe(values))
            return row_id
        self._violate("write", f"unknown write mode '{mode}'")

    def record_event(self, type_: str, payload: dict | None = None, station: str | None = None) -> int:
        """One of this agent's own events (only the types listed in its contract)."""
        return self.write("events", {"occurred_at": self.now, "type": type_, "actor": "agent", "actor_name": self.agent,
                                     "station": station, "payload": payload or {}, "run_id": self.run_id})

    def audit(self, what: str, entity_type: str, entity_id, before=None, after=None) -> int:
        return self.write("audit_log", {"at": self.now, "actor": "agent", "actor_name": self.agent, "what": what,
                                        "entity_type": entity_type, "entity_id": str(entity_id), "before": before, "after": after,
                                        "run_id": self.run_id})

    # ------------------------------------------------------------------ proposing
    def _check_decisions(self, ids):
        catalog = load_decisions()
        for d in ids or []:
            if d not in catalog:
                self._violate("decision", f"'{d}' is not a decision in config/decisions.yaml")
            if catalog[d]["proposed_by"] != self.agent:
                self._violate("decision", f"decision {d} is raised by {catalog[d]['proposed_by']}, not {self.agent}")

    def propose(self, action: str | None, output: dict, safety_rung: int | None = None) -> int:
        """Save a proposal for the orchestrator's rule check. It is never shown to a human until that check passes it."""
        if action is not None and action not in self.contract.actions:
            if action in self.rules["autonomy"] and self.rules["autonomy"][action]["level"] == "human_only":
                self._violate("action", f"'{action}' is a human-only decision; this agent can only ask_human with the facts")
            self._violate("action", f"'{action}' is not an action this agent may propose ({self.contract.actions})")
        problems = jsonschema_lite.validate(output, self.contract.output)
        if problems:
            self._violate("output", "; ".join(problems))
        self._check_decisions(output["decision_ids"])
        for s in output["sources"]:
            if not _iso(s.get("as_of")):
                self._violate("output", f"source '{s.get('source')}' has no usable as_of time ('{s.get('as_of')}')")
        missing = [n for n in self.contract.required_checks.get(action, []) if n not in self.checks_run]
        if missing:
            self._violate("required checks", f"'{action}' needs these checks first: {missing}")
        self.check_fresh()
        rung = safety_rung or output.get("safety_rung")
        a = autonomy_for(action, self.rules, safety_rung=rung) if action else {"level": "needs_approval", "approver": "station_manager"}
        return self.write("recommendations", {
            "incident_id": self.incident_id, "agent": self.agent, "title": output["title"], "why": output["why"],
            "options": output["options"] if output["options"] else [{"action": action, "output": output}],
            "autonomy": a["level"], "approver_role": a.get("approver"), "safety_rung": rung, "decision_ids": output["decision_ids"],
            "data_as_of": self.oldest_as_of, "status": "pending", "run_id": self.run_id, "created_at": self.now})

    def ask_human(self, decision_id: str, facts: dict, reason: str) -> int:
        """For a human-only decision, or when unsure: hand the facts to the named human. Never a recommendation it can act on."""
        catalog = load_decisions()
        if decision_id not in catalog:
            self._violate("decision", f"'{decision_id}' is not a decision in config/decisions.yaml")
        d = catalog[decision_id]
        return self.record_event("human_decision_needed", {"decision_id": decision_id, "question": d["question"],
                                                           "decided_by": d["decided_by"], "facts": facts, "reason": reason})

    # ------------------------------------------------------------------ the orchestrator records the rule check
    def record_rule_check(self, recommendation_id: int, result: dict) -> None:
        """The rule check's verdict on a proposal. Only the orchestrator's contract allows this."""
        ok = result["result"] == "pass"
        values = {"rule_check_ok": ok, "rule_check_notes": result["reasons"] or None, "rule_ids": result["rule_ids"] or None}
        if not ok:
            values["status"] = "blocked_by_rule_check"
        self.write("recommendations", values, mode="update", row_id=recommendation_id)
        self.record_event("rule_check_passed" if ok else "rule_check_blocked",
                          {"recommendation_id": recommendation_id, "reasons": result["reasons"], "rule_ids": result["rule_ids"],
                           "needs_human": result["needs_human"]})

    # ------------------------------------------------------------------ language-model text (G10)
    def ai_text(self, draft: str | None, facts: dict, template: str) -> dict:
        """Check a language model's text against the facts. If it is missing or says anything not in the facts, use the template."""
        if not self.contract.ai_allowed:
            self._violate("ai", "this agent may not use a language model")
        bad = [] if draft else ["no text from the model"]
        if draft:
            known = " ".join(str(v) for v in _flatten(facts))
            for token in re.findall(r"\b(?:WN\d+|\d{1,2}:\d{2}|\$?\d+(?:\.\d+)?)\b", draft):
                if token.lstrip("$") not in known:
                    bad.append(f"'{token}' is not in the facts")
        used_template = bool(bad)
        text = template.format(**{k: v for k, v in facts.items() if not isinstance(v, (dict, list))}) if used_template else draft
        self._audit_row("language-model text checked", "ai_text", "-", {"draft": draft},
                        {"used_template": used_template, "problems": bad, "text": text})
        return {"text": text, "used_template": used_template, "problems": bad}

    # ------------------------------------------------------------------ temporary notes
    def remember(self, key: str, value) -> None:
        self._temp[key] = value

    def recall(self, key: str, default=None):
        return self._temp.get(key, default)

    def close_incident(self) -> int:
        """The incident is over: temporary notes are deleted. (Recommendations and the audit log stay.)"""
        n = len(self._temp)
        self._temp.clear()
        return n


TEXT_ARRAYS = {"rule_check_notes", "rule_ids", "decision_ids", "reserve_crew_ids"}


def _adapt(col, v):
    if col in TEXT_ARRAYS:
        return list(v) if v is not None else None
    return Jsonb(v) if isinstance(v, (dict, list)) else v


def _json(v):
    return Jsonb(v) if v is not None else None


def _safe(values: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in values.items()}


def _iso(text) -> bool:
    try:
        datetime.fromisoformat(str(text))
        return True
    except ValueError:
        return False


def _flatten(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _flatten(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _flatten(x)
    else:
        yield v
