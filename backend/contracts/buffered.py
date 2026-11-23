"""AgentContext with a bounded, published-input store and batched output.

Used by the live minute loop and in-memory comparisons alike. Proposal, phase,
schema, decision, write permissions and privacy checks remain AgentContext's.
The simulator supplies published events, not world objects, to this boundary.

Several agents in one minute loop share one SharedStore: the published source events, and the shared records (e.g. Situation's
assessments) and heartbeats agents wrote. Agents never call each other; they only meet in the store, through their contexts,
under the shared-state rules (backend/contracts/shared_state.py).
"""
from copy import deepcopy

from backend.contracts.context import AgentContext
from backend.contracts.loader import PII_COLUMNS
from backend.contracts.shared_state import HEARTBEAT, SHARED, visible


class SharedStore:
    def __init__(self):
        self.events = []                                       # published source events, each with a run-local id
        self.records = {table: [] for table in SHARED}         # shared, append-only records agents wrote
        self.heartbeats = {}                                   # producer agent -> [times it ran]


class BufferedContext(AgentContext):
    def __init__(self, now, schedule=None, passengers=None, feeds=None, agent="communication", store=None, rows=None):
        super().__init__(agent, None, now, run_id=agent, feeds=feeds)
        self.rows = rows if rows is not None else {"flights": deepcopy(schedule), "passengers": deepcopy(passengers)}
        self.store = store or SharedStore()
        self.outputs = []
        self.audit = []
        self.saved = {}
        self.read_columns = set()

    def _violate(self, kind, detail, error=None):
        from backend.contracts.context import ContractViolation
        err = (error or ContractViolation)(self.agent, kind, detail)
        self.violations.append(err)
        self.outputs.append(("contract_violation", {"kind": kind, "detail": detail}))
        self._audit_row("contract violation refused", "agent", self.agent, None, {"kind": kind})
        raise err

    @property
    def events(self):
        return self.store.events

    @events.setter
    def events(self, value):
        self.store.events = value

    # shared state: the same rules as AgentContext, over the store instead of the database
    def _shared_latest(self, table, meta, spec):
        return visible(self.store.records[table], table, self.now)

    def producer_heartbeat(self, producer):
        if not any(v.get("producer") == producer for v in (self.contract.shared_reads or {}).values()):
            self._violate("read", f"this agent reads nothing from {producer}")
        return max((t for t in self.store.heartbeats.get(producer, []) if t <= self.now), default=None)

    def passenger_screens(self):
        from backend.screens import screens_for
        self._projection("passenger_screens")
        return {p["id"]: screens_for(p.get("channel_types", ())) for p in self.rows["passengers"]}

    def contact_channel_types(self, passenger_id):
        self._projection("contact_channel_types")
        return sorted({c for p in self.rows["passengers"] if p["id"] == passenger_id
                       for c in p.get("channel_types", ()) if c in ("app", "sms", "email")})

    def _audit_row(self, what, entity_type, entity_id, before, after):
        self.audit.append({"at": self.now, "what": what, "entity_type": entity_type,
                           "entity_id": str(entity_id), "before": deepcopy(before), "after": deepcopy(after)})

    def read(self, table, columns=None, where=None, limit=None, order_by=None, since=None, delivered_since=None):
        allowed = self.contract.reads.get(table)
        if allowed is None:
            self._violate("read", f"table '{table}' is not in this agent's reads")
        columns = list(columns or allowed)
        for col in columns + list(where or {}) + ([order_by] if order_by else []):
            if col not in allowed:
                self._violate("read", f"column '{table}.{col}' is not in this agent's reads")
        if any(c in PII_COLUMNS.get(table, ()) for c in columns):
            self._violate("read", "names and contact details are unavailable here")
        self.read_columns.update((table, c) for c in columns)
        if table == "events":
            rows = []
            for event in self.events:
                delivered = event.get("published_at") or event["occurred_at"]
                if delivered > self.now or event["occurred_at"] > self.now:
                    continue
                if since is not None and event["occurred_at"] <= since:
                    continue
                if delivered_since is not None and delivered <= delivered_since:
                    continue
                row = self._published(event)
                if row is not None:
                    rows.append({**{c: deepcopy(row.get(c)) for c in columns}, "feed": row["feed"]})
        else:
            rows = [{c: deepcopy(row.get(c)) for c in columns} for row in self.rows[table]]
        for k, v in (where or {}).items():
            rows = [r for r in rows if r.get(k) in v] if isinstance(v, (list, tuple, set)) else [r for r in rows if r.get(k) == v]
        if order_by:
            rows.sort(key=lambda r: r[order_by])
        return rows[:limit] if limit else rows

    def _check(self, table, values, mode, row_id=None, check_owner=True):
        if mode == "update" and table == "mismatches" and (table, row_id) not in self.saved:
            self._violate("write", "an agent may only update its own mismatch rows")
        return super()._check(table, values, mode, row_id, check_owner=False)

    def _write_checked(self, table, values, mode, row_id, w):
        if mode == "insert":
            row_id = len(self.saved) + 1
            self.saved[(table, row_id)] = deepcopy(values)
        else:
            self.saved[(table, row_id)].update(deepcopy(values))
        self._audit_row(f"{self.agent} wrote {table}", table, row_id, None, values)
        if table in self.store.records:
            self.store.records[table].append(deepcopy(values))           # visible to readers from its own time on
        if table == "events":
            self.outputs.append((values["type"], deepcopy(values.get("payload"))))
            if values["type"] == HEARTBEAT:
                self.store.heartbeats.setdefault(self.agent, []).append(self.now)
        return row_id

    def write_many(self, table, rows, mode="insert", row_ids=None):
        ids = row_ids if row_ids is not None else [None] * len(rows)
        return [self.write(table, row, mode, rid) for row, rid in zip(rows, ids)]
