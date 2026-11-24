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
from backend.contracts.loader import (INCIDENT_TABLES, INCIDENT_WRITERS, OWN_TABLES, PII_AGENTS, PII_COLUMNS, TRUTH_TABLES, Contract,
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
                 contracts: dict[str, Contract] | None = None, rules: dict | None = None, source_run_id: str | None = None,
                 feeds=None, producer_runs: dict | None = None):
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
        # The run it reads events from. The agent never sees it (it would reveal the scenario name and seed).
        self.source_run_id = source_run_id
        if feeds is None:
            from backend.contracts.feeds import Feeds
            feeds = Feeds()
        self.feeds = feeds
        # Which run of each producer agent it reads shared records from (e.g. {"situation": agent_run_id}). Not given: the latest
        # run of that producer on the same source run. The agent never sees these ids.
        self.producer_runs = dict(producer_runs or {})

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

    def feed_last(self, feed: str) -> datetime:
        """When this simulated source last sent a heartbeat (every simulated minute unless it was stopped)."""
        return self.feeds.last_heartbeat(feed, self.now)

    def _projection(self, name: str):
        if not self.contract.projection_ok(name):
            self._violate("read", f"projection '{name}' is not in this agent's contract (or not switched on in its phase)")

    def contact_channel_types(self, passenger_id: str) -> list[str]:
        """Projection: channel type names only; never names or addresses."""
        self._projection("contact_channel_types")
        self.cur.execute("SELECT DISTINCT c FROM passengers CROSS JOIN LATERAL unnest(contact_channels) c "
                         "WHERE id=%s AND c IN ('app', 'sms', 'email') ORDER BY c", (passenger_id,))
        return [row[0] for row in self.cur.fetchall()]

    def passenger_screens(self) -> dict[str, tuple]:
        """Projection: which of the four screens each passenger has (backend/screens.py). Screen types only, in one query."""
        from backend.screens import screens_for
        self._projection("passenger_screens")
        self.cur.execute("SELECT id, contact_channels FROM passengers ORDER BY id")
        return {pid: screens_for(contacts) for pid, contacts in self.cur.fetchall()}

    # ------------------------------------------------------------------ shared state (backend/contracts/shared_state.py)

    def _shared_spec(self, table: str) -> dict:
        spec = (self.contract.shared_reads or {}).get(table)
        if spec is None:
            self._violate("read", f"shared table '{table}' is not in this agent's shared_reads")
        return spec

    def read_shared(self, table: str, fields: list[str] | None = None) -> dict | None:
        """The producer's latest version with time <= now, cut to this contract's fields (suspicions kept as suspicions).
        Every read is written to the audit log with the version read."""
        from backend.contracts.shared_state import SHARED, project
        spec = self._shared_spec(table)
        for f in fields or []:
            if f not in spec["fields"]:
                self._violate("read", f"{table}.{f} is not in this agent's shared_reads")
        if fields:
            spec = {**spec, "fields": list(fields)}
        meta = SHARED[table]
        row = self._shared_latest(table, meta, spec)
        view = project(row, table, spec)
        self._audit_row(f"read shared {table}", table, view["version"] if view else None, None,
                        {"producer": meta["producer"], "version": view["version"] if view else None,
                         "time": view["time"].isoformat() if view else None, "read_at": self.now.isoformat()})
        return view

    def _producer_run(self, producer: str) -> str | None:
        from backend.contracts.shared_state import SHARED
        if producer not in self.producer_runs:
            table = next(t for t, m in SHARED.items() if m["producer"] == producer)
            self.cur.execute(sql.SQL("SELECT agent_run_id FROM {} WHERE run_id IS NOT DISTINCT FROM %s "
                                     "ORDER BY created_at DESC, id DESC LIMIT 1").format(sql.Identifier(table)), (self.source_run_id,))
            row = self.cur.fetchone()
            self.producer_runs[producer] = row[0] if row else None
        return self.producer_runs[producer]

    def _shared_latest(self, table: str, meta: dict, spec: dict) -> dict | None:
        cols = sorted(set(spec["fields"]) | {meta["version"], meta["time"]} |
                      {meta["suspected"][f] for f in spec["fields"] if f in meta["suspected"]} & set(spec["fields"]))
        run = self._producer_run(meta["producer"])
        if run is None:
            return None
        self.cur.execute(sql.SQL("SELECT {} FROM {} WHERE agent_run_id = %s AND {} <= %s ORDER BY {} DESC LIMIT 1").format(
            sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier(table), sql.Identifier(meta["time"]),
            sql.Identifier(meta["version"])), (run, self.now))
        r = self.cur.fetchone()
        return dict(zip(cols, r)) if r else None

    def producer_heartbeat(self, producer: str) -> datetime | None:
        """When the producer last RAN, at or before now (rule 6: freshness is about the producer, not its last version)."""
        from backend.contracts.shared_state import HEARTBEAT
        if not any(v.get("producer") == producer for v in (self.contract.shared_reads or {}).values()):
            self._violate("read", f"this agent reads nothing from {producer}")
        run = self._producer_run(producer)
        if run is None:
            return None
        self.cur.execute("SELECT max(occurred_at) FROM events WHERE run_id = %s AND type = %s AND occurred_at <= %s",
                         (run, HEARTBEAT, self.now))
        return self.cur.fetchone()[0]

    def heartbeat(self, payload: dict | None = None) -> int:
        """A producer says 'I ran at now' (every run, even with no new version)."""
        from backend.contracts.shared_state import HEARTBEAT
        return self.record_event(HEARTBEAT, {"agent": self.agent, **(payload or {})})

    def feed_age_sec(self, feed: str) -> float:
        return (self.now - self.feed_last(feed)).total_seconds()

    def read_events(self, since: datetime | None = None, delivered_since: datetime | None = None) -> list[dict]:
        """Events of its feeds that happened after `since` (if given) and at or before now. Same enforcement as read().
        delivered_since: only events DELIVERED after this time (for incremental reading; a late event counts when it arrives)."""
        return self.read("events", since=since, delivered_since=delivered_since, order_by="occurred_at")

    def read(self, table: str, columns: list[str] | None = None, where: dict | None = None, limit: int | None = None,
             order_by: str | None = None, since: datetime | None = None, delivered_since: datetime | None = None) -> list[dict]:
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
        has_as_of = "as_of" in self.tables[table] and table not in self.contract.static_reads
        is_events = table == "events"
        extra = []
        if has_as_of and "as_of" not in columns:
            extra.append("as_of")
        if is_events:
            extra += [c for c in ("occurred_at", "type", "payload") if c not in columns]
        select = columns + extra
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(map(sql.Identifier, select)), sql.Identifier(table))
        params = []
        parts = []
        if is_events:                       # no future knowledge, only its run, only its feeds: enforced here, not in agent code
            parts.append(sql.SQL("coalesce(published_at, occurred_at) <= %s"))   # delivered (late events count when they arrive)
            params.append(self.now)
            if since is not None:
                parts.append(sql.SQL("occurred_at > %s"))
                params.append(since)
            if delivered_since is not None:
                parts.append(sql.SQL("coalesce(published_at, occurred_at) > %s"))
                params.append(delivered_since)
            if self.source_run_id is not None:
                parts.append(sql.SQL("run_id = %s"))
                params.append(self.source_run_id)
            if self.contract.event_feeds is not None:
                parts.append(sql.SQL("type = ANY(%s)"))
                params.append(sorted(self.contract.event_types))
        if where:
            for k, v in where.items():
                if isinstance(v, (list, tuple, set)):
                    parts.append(sql.SQL("{} = ANY(%s)").format(sql.Identifier(k)))
                    params.append(list(v))
                else:
                    parts.append(sql.SQL("{} = %s").format(sql.Identifier(k)))
                    params.append(v)
        if parts:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts)
        if order_by:
            query += sql.SQL(" ORDER BY {}").format(sql.Identifier(order_by))
        if limit:
            query += sql.SQL(" LIMIT {}").format(sql.Literal(int(limit)))
        self.cur.execute(query, params)
        names = [d.name for d in self.cur.description]
        rows = [dict(zip(names, r)) for r in self.cur.fetchall()]
        if is_events:
            rows = [r for r in (self._published(r) for r in rows) if r is not None]
            for r in rows:
                for c in extra:
                    if c not in columns:
                        r.pop(c, None)
        for r in rows:
            as_of = r.get("as_of")
            if isinstance(as_of, datetime) and (self.oldest_as_of is None or as_of < self.oldest_as_of):
                self.oldest_as_of = as_of
            if has_as_of and "as_of" not in columns:
                r.pop("as_of", None)
        return rows

    def _published(self, row: dict) -> dict | None:
        """Keep only what a real source publishes, and nothing from a feed after it stopped."""
        if self.contract.event_feeds is None:
            return row
        feed, fields = self.contract.event_types.get(row["type"], (None, None))
        if feed is None:
            return None
        if feed in self.contract.feed_split:                # one feed per value of a payload field (e.g. one per channel)
            field, names = self.contract.feed_split[feed]
            feed = (row.get("payload") or {}).get(field)
            if feed not in names:
                return None
        silent = self.feeds.window(feed, self.now)
        if silent and row["occurred_at"] >= silent[0]:      # the feed is silent: nothing it sent since it went quiet arrives yet
            return None
        row = dict(row)
        row["payload"] = {k: v for k, v in (row.get("payload") or {}).items() if k in fields}
        row["feed"] = feed
        return row

    def write_assessment(self, values: dict) -> int:
        """Save one version of the shared situation assessment (append-only). The run ids are filled in here, not by the agent."""
        return self.write("situation_assessments", {**values, "agent_run_id": self.run_id, "run_id": self.source_run_id})

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
        w = self._check(table, values, mode, row_id)
        return self._write_checked(table, self._with_run_ids(table, values, mode), mode, row_id, w)

    def _with_run_ids(self, table, values, mode):
        """Rows of an agent's own table carry its replay id and the run it read, filled in here (the agent never sees the run)."""
        if table in ("mismatches", "recovery_plans", "care_plans") and mode == "insert":
            return {**values, "agent_run_id": self.run_id, "run_id": self.source_run_id}
        return values

    def _check(self, table: str, values: dict, mode: str, row_id=None, check_owner: bool = True) -> dict:
        """Everything a write must pass, for one row. Returns the contract's entry for the table."""
        if table in TRUTH_TABLES:
            self._violate("write", f"'{table}' is a truth table; agents never write to it (a human-approved action does)")
        w = self.contract.writes.get(table)
        if w is None:
            self._violate("write", f"table '{table}' is not in this agent's writes")
        if table in INCIDENT_TABLES and self.agent not in INCIDENT_WRITERS:
            self._violate("write", "only the orchestrator writes incidents")
        if table in OWN_TABLES and self.agent not in OWN_TABLES[table]:
            self._violate("write", f"only {sorted(OWN_TABLES[table])} writes {table}")
        if table == "mismatches":
            if mode == "insert" and (not values.get("sources") or not values.get("decision_ids")
                                     or not all(_iso(x.get("as_of")) for x in values["sources"])):
                self._violate("output", "a mismatch needs its sources, each with an as_of time (G01), and decision ids (G08)")
            if mode == "update" and check_owner:
                self.cur.execute("SELECT agent_run_id FROM mismatches WHERE id = %s", (row_id,))
                own = self.cur.fetchone()
                if not own or own[0] != self.run_id:
                    self._violate("write", "an agent may only update its own mismatch rows (this run's)")
            self._check_decisions(values.get("decision_ids"))
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
        return w

    def _write_checked(self, table, values, mode, row_id, w):
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

    def write_many(self, table: str, rows: list[dict], mode: str = "insert", row_ids: list | None = None) -> list:
        """Many rows at once, for an agent that writes a lot (the communication agent's mismatches). Every row gets the same checks
        as write() and its own audit row; the rows go to the database in a few round trips instead of several per row."""
        if not rows:
            return []
        if mode == "update" and (row_ids is None or len(row_ids) != len(rows)):
            self._violate("write", "an update needs the id of every row")
        if mode == "update" and table == "mismatches":
            self.cur.execute("SELECT id, agent_run_id FROM mismatches WHERE id = ANY(%s)", (list(row_ids),))
            own = dict(self.cur.fetchall())
            if any(own.get(i) != self.run_id for i in row_ids):
                self._violate("write", "an agent may only update its own mismatch rows (this run's)")
        for v in rows:                             # every check of write() on every row (ownership was checked above, for all)
            self._check(table, v, mode, check_owner=False)
        if mode == "insert":
            rows = [self._with_run_ids(table, v, mode) for v in rows]
            cols = list(rows[0])
            if any(list(v) != cols for v in rows):
                self._violate("write", "write_many needs the same columns in every row")
            q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(sql.Placeholder() * len(cols)))
            self.cur.executemany(q, [[_adapt(c, v[c]) for c in cols] for v in rows], returning=True)
            ids = []
            while True:
                ids.append(self.cur.fetchone()[0])
                if not self.cur.nextset():
                    break
            audit = [(f"{self.agent} wrote {table}", table, i, None, _safe(v)) for i, v in zip(ids, rows)]
        else:
            groups = {}
            for i, v in zip(row_ids, rows):
                groups.setdefault(tuple(v), []).append((i, v))
            for cols, items in groups.items():
                q = sql.SQL("UPDATE {} SET {} WHERE id = %s").format(
                    sql.Identifier(table), sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in cols))
                self.cur.executemany(q, [[_adapt(c, v[c]) for c in cols] + [i] for i, v in items])
            ids = list(row_ids)
            audit = [(f"{self.agent} updated {table}", table, i, None, _safe(v)) for i, v in zip(ids, rows)]
        if table != "audit_log":
            self.cur.executemany(
                "INSERT INTO audit_log (at, actor, actor_name, what, entity_type, entity_id, before, after, run_id) "
                "VALUES (%s, 'agent', %s, %s, %s, %s, %s, %s, %s)",
                [(self.now, self.agent, what, et, str(i), _json(b), _json(a), self.run_id) for what, et, i, b, a in audit])
        return ids

    def record_events(self, items: list[tuple]) -> list:
        """Several of this agent's own events at once: [(type, payload)]."""
        return self.write_many("events", [{"occurred_at": self.now, "type": t, "actor": "agent", "actor_name": self.agent,
                                           "station": None, "payload": p or {}, "run_id": self.run_id} for t, p in items])

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
            if catalog[d]["proposed_by"] != self.agent and self.agent not in (catalog[d].get("also_raised_by") or []):
                self._violate("decision", f"decision {d} is raised by {catalog[d]['proposed_by']}, not {self.agent}")

    def propose(self, action: str | None, output: dict, safety_rung: int | None = None, supersedes: int | None = None) -> int:
        """Save a proposal for the orchestrator's rule check. It is never shown to a human until that check passes it."""
        if action is not None and action in self.contract.actions and not self.contract.enabled(action):
            self._violate("action", f"'{action}' is switched on in phase {self.contract.enabled_in[action]}; this is phase "
                                    f"{self.contract.phase}")
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
        owner = None
        catalog = load_decisions()
        for d in output["decision_ids"]:
            owner = owner or catalog[d]["decided_by"].get("role")
        a = autonomy_for(action, self.rules, safety_rung=rung, owner=owner) if action else \
            {"level": "needs_approval", "approver": owner}
        return self.write("recommendations", {
            "incident_id": self.incident_id, "agent": self.agent, "title": output["title"], "why": output["why"],
            "options": output["options"] if output["options"] else [{"action": action, "output": output}],
            "autonomy": a["level"], "approver_role": a.get("approver"), "safety_rung": rung, "decision_ids": output["decision_ids"],
            "data_as_of": self.oldest_as_of, "status": "pending", "run_id": self.run_id, "created_at": self.now,
            **({"supersedes": supersedes} if supersedes is not None else {})})

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
