"""AgentContext: the only way an agent reads or writes the database.

It enforces the agent's contract. Anything outside the contract raises a ContractViolation (a clear error in plain English) AND writes a
"contract_violation" event, so the breach is on the record. What it checks:
  * reads: only the tables and columns in the contract; names and contact details only for passenger_care and communication;
  * writes: only the tables and columns in the contract; never a truth table; incidents only for the orchestrator;
  * actions: only those in the contract (and in the autonomy rules);
  * output: must match the contract's output schema exactly;
  * data age: no recommendation if the data it was built on is older than max_data_age_sec.
"""
from datetime import datetime

from psycopg import sql
from psycopg.types.json import Jsonb

from backend.contracts import jsonschema_lite
from backend.contracts.loader import (INCIDENT_TABLES, INCIDENT_WRITERS, PII_AGENTS, PII_COLUMNS, TRUTH_TABLES, Contract,
                                      load_contracts, schema_tables)
from backend.rules import load_rules
from backend.rules.autonomy import autonomy_for


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
        self._temp: dict = {}                              # temporary notes: deleted when the incident closes

    # ------------------------------------------------------------------ the record of a breach
    def _violate(self, kind: str, detail: str, error=ContractViolation):
        err = error(self.agent, kind, detail)
        self.violations.append(err)
        self.cur.execute(
            "INSERT INTO events (occurred_at, type, actor, actor_name, station, payload, run_id) "
            "VALUES (%s, 'contract_violation', 'system', 'contract guard', NULL, %s, %s)",
            (self.now, Jsonb({"agent": self.agent, "kind": kind, "detail": detail, "incident_id": self.incident_id}), self.run_id))
        raise err

    # ------------------------------------------------------------------ reading
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
        has_as_of = "as_of" in self.tables[table]
        select = columns + (["as_of"] if has_as_of and "as_of" not in columns else [])
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(map(sql.Identifier, select)), sql.Identifier(table))
        params = []
        if where:
            parts = []
            for k, v in where.items():
                if isinstance(v, (list, tuple)):
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

    # ------------------------------------------------------------------ writing
    def write(self, table: str, values: dict, mode: str = "insert", row_id=None):
        """Insert (or update one row of) a table the contract allows. Returns the new id when there is one."""
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
        bad = [c for c in values if allowed is not None and c not in allowed and not (table == "events" and c in ("payload", "type", "occurred_at", "station", "actor", "actor_name", "run_id"))
               and not (table == "audit_log" and c in self.tables["audit_log"])]
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
            self.cur.execute(q, [_adapt(v) for v in values.values()])
            return self.cur.fetchone()[0] if has_id else None
        if mode == "update":
            if row_id is None:
                self._violate("write", "an update needs the id of the row")
            q = sql.SQL("UPDATE {} SET {} WHERE id = %s").format(
                sql.Identifier(table), sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in cols))
            self.cur.execute(q, [_adapt(v) for v in values.values()] + [row_id])
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
    def propose(self, action: str | None, output: dict, safety_rung: int | None = None) -> int:
        """Save a recommendation. The action must be in the contract and the output must match its schema; the data must be fresh."""
        if action is not None and action not in self.contract.actions:
            self._violate("action", f"'{action}' is not an action this agent may propose ({self.contract.actions})")
        problems = jsonschema_lite.validate(output, self.contract.output)
        if problems:
            self._violate("output", "; ".join(problems))
        self.check_fresh()
        a = autonomy_for(action, self.rules, safety_rung=safety_rung or output.get("safety_rung")) if action else \
            {"level": "needs_approval", "approver": "station_manager"}
        rec_id = self.write("recommendations", {
            "incident_id": self.incident_id, "agent": self.agent, "title": output["title"], "why": output["why"],
            "options": output["options"] if output["options"] else [{"action": action, "output": output}],
            "autonomy": a["level"], "approver_role": a.get("approver"), "safety_rung": safety_rung or output.get("safety_rung"),
            "data_as_of": self.oldest_as_of, "status": "pending", "run_id": self.run_id, "created_at": self.now})
        self.audit(f"{self.agent} proposed {action or 'a summary'}", "recommendation", rec_id,
                   after={"title": output["title"], "action": action})
        return rec_id

    def review(self, recommendation_id: int, ok: bool, notes: list[str]) -> None:
        """The guardrail's verdict on someone else's recommendation (it may block, never edit the proposal)."""
        values = {"guardrail_ok": ok, "guardrail_notes": notes or None}
        if not ok:
            values["status"] = "blocked_by_guardrail"
        self.write("recommendations", values, mode="update", row_id=recommendation_id)
        self.audit(f"guardrail {'passed' if ok else 'blocked'} recommendation", "recommendation", recommendation_id, after=values)

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


def _adapt(v):
    return Jsonb(v) if isinstance(v, (dict, list)) and not _is_text_array(v) else v


def _is_text_array(v) -> bool:
    """guardrail_notes is a text[]; lists of plain strings go in as arrays, everything else as json."""
    return isinstance(v, list) and all(isinstance(x, str) for x in v) and bool(v)
