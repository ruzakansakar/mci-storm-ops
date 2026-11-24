"""Shared state between agents (Phase 4c). The rules are in config/agents/_shared.yaml (shared_state); they are enforced HERE,
through AgentContext, never left to agent code.

  1. Agents never call each other. One agent writes a shared record; another reads it through AgentContext.
  2. Every shared record is append-only and versioned, and carries its producer, its time and its sources (SHARED below; the
     database refuses UPDATE/DELETE on situation_assessments, and only its owner may insert).
  3. A reader only sees records whose time is <= its own time T (no future, not even later in the same minute).
  4. A reader's contract (shared_reads) lists exactly which producer fields it may read. Everything else is refused, and some
     fields can never be shared at all (NEVER_SHARED).
  5. Suspicions stay suspicions: estimates and suspected causes reach the reader only inside "suspected", each with its confidence.
  6. Freshness is about the producer, not the record: a producer writes a version only when something changes, so an old version
     is not stale. A producer that has stopped RUNNING is. Each run leaves a heartbeat event (HEARTBEAT) and staleness is
     measured from that.
  7. When two agents disagree, both views are logged (DISAGREE event) and a human is asked. Resolving it is the orchestrator's job.
"""
from datetime import datetime

HEARTBEAT = "agent_heartbeat"            # event type every producer writes each time it runs (even when it writes no new version)
DISAGREE = "agents_disagree"             # event type with both agents' views, when they disagree about the same fact

# table -> how its records are shared
SHARED = {
    "situation_assessments": {
        "producer": "situation",
        "version": "version",
        "time": "assessed_at",                    # the producer's time T for this version
        "sources": ["evidence", "data_as_of"],    # where its facts came from (evidence stays with the producer, see NEVER_SHARED)
        # estimates and suspected causes -> the column holding the confidence that goes with them
        "suspected": {"type": "confidence", "likely_cause": "confidence", "expected_end_earliest": "end_confidence",
                      "expected_end_likely": "end_confidence", "expected_end_latest": "end_confidence"},
        "confidences": ["confidence", "end_confidence"],
        "conflict_keys": ["key", "flight_ids", "fields", "status", "sides", "opened_at", "side", "settled_by"],
                                                  # the most a reader may see of each conflict (never its evidence ids)
    },
}
SHARED["recovery_plans"] = {                      # Phase 4d: the Network Recovery agent's (advisory) plan
    "producer": "network_recovery",
    "version": "version",
    "time": "planned_at",
    "sources": ["sources", "data_as_of"],
    "suspected": {"estimates": None},              # estimates (e.g. passengers stranded) are not facts: no confidence column
    "confidences": [],
    "conflict_keys": [],
    "item_keys": {
        "delays": ["proposal_id", "flight_id", "new_departure", "delay_min", "protection", "crew_legal"],
        "cancels": ["proposal_id", "flight_id", "destination", "pax", "why"],
        "crew_actions": ["proposal_id", "flight_id", "action", "crew_id", "role", "report_by"],
        "approval_status": ["proposal_id", "plan_version", "status", "role", "rule_check_ok", "decided_at"],
    },
}
SHARED["care_plans"] = {                          # Phase 4e: the Passenger Care agent's plan
    "producer": "passenger_care",
    "version": "version",
    "time": "planned_at",
    "sources": ["sources", "data_as_of"],
    "suspected": {},
    "confidences": [],
    "conflict_keys": [],
    # the most a reader may see of each passenger's entry (never the segment, the compensation or anything sensitive)
    "item_keys": {"passengers": ["passenger_id", "hotel_name", "hotel_status", "hotel_decision", "shuttle", "transport",
                                 "transport_status", "transport_decision"]},
}
# fields no reader contract may list, with the reason
NEVER_SHARED = {
    ("situation_assessments", "flight_controllability"): "it decides compensation: usable only once a human has approved it "
                                                         "(a later phase)",
    ("situation_assessments", "evidence"): "evidence internals stay with the producer",
}


class SharedStateError(ValueError):
    pass


def validate_shared_reads(reader: str, shared_reads: dict, tables: dict) -> None:
    """Called by the contract loader: a reader may only list fields that exist and may be shared, from the table's producer."""
    for table, spec in (shared_reads or {}).items():
        if table not in SHARED:
            raise SharedStateError(f"{reader}: '{table}' is not a shared table")
        meta = SHARED[table]
        if spec.get("producer") != meta["producer"]:
            raise SharedStateError(f"{reader}: {table} is produced by {meta['producer']}, not {spec.get('producer')}")
        if reader == meta["producer"]:
            raise SharedStateError(f"{reader}: an agent does not read its own shared records through shared_reads")
        for f in spec.get("fields") or []:
            if (table, f) in NEVER_SHARED:
                raise SharedStateError(f"{reader}: may not read {table}.{f}: {NEVER_SHARED[(table, f)]}")
            if f not in tables[table]:
                raise SharedStateError(f"{reader}: {table}.{f} is not a column")
        for k in spec.get("conflict_fields") or []:
            if k not in meta["conflict_keys"]:
                raise SharedStateError(f"{reader}: may not read conflict detail '{k}'")
        for col, keys in (spec.get("item_fields") or {}).items():
            for k in keys:
                if k not in meta.get("item_keys", {}).get(col, []):
                    raise SharedStateError(f"{reader}: may not read {table}.{col}.{k}")


def visible(rows: list[dict], table: str, now: datetime) -> dict | None:
    """The latest version whose time is <= now (rule 3)."""
    meta = SHARED[table]
    ok = [r for r in rows if r[meta["time"]] <= now]
    return max(ok, key=lambda r: r[meta["version"]]) if ok else None


def project(row: dict | None, table: str, spec: dict) -> dict | None:
    """What the reader gets (rules 4 and 5): only its contract's fields; suspicions only with their confidence."""
    if row is None:
        return None
    meta = SHARED[table]
    fields = list(spec["fields"])
    out = {"producer": meta["producer"], "table": table, "version": row[meta["version"]], "time": row[meta["time"]],
           "facts": {}, "suspected": {}}
    for f in fields:
        if f in (meta["version"], meta["time"]):
            continue
        if f in meta["suspected"]:
            conf = meta["suspected"][f]
            out["suspected"][f] = {"value": row.get(f), "confidence": _num(row.get(conf)) if conf and conf in fields else None,
                                   "suspected": True}
        elif f in meta["confidences"]:
            continue                                     # a confidence travels only with the suspicion it belongs to
        elif f == "conflicts":
            keys = spec.get("conflict_fields") or ["key"]
            out["facts"]["conflicts"] = [{k: c[k] for k in keys if k in c} for c in (row.get("conflicts") or [])]
        elif f in (spec.get("item_fields") or {}):            # a list of entries: only the listed keys of each
            keys = spec["item_fields"][f]
            out["facts"][f] = [{k: e[k] for k in keys if k in e} for e in (row.get(f) or [])]
        elif f in meta.get("item_keys", {}):
            out["facts"][f] = []                                 # entries whose keys the reader's contract does not list
        else:
            out["facts"][f] = row.get(f)
    return out


def _num(v):
    return None if v is None else float(v)


def heartbeat_age_min(last: datetime | None, now: datetime) -> float | None:
    """Rule 6: how long since the producer last ran (None = it has never run)."""
    return None if last is None else (now - last).total_seconds() / 60


def disagreement(ctx, topic: str, mine: dict, theirs: dict, producer: str) -> int:
    """Rule 7: log both views. The caller escalates through its own escalation action; nobody picks a side here."""
    return ctx.record_event(DISAGREE, {"topic": topic, "reader": ctx.agent, "reader_view": mine, "producer": producer,
                                       "producer_view": theirs, "resolved_by": "nobody yet: a human (the orchestrator later)"})
