"""The shared-state audit (Phase 5a), every minute.

For each agent that ran: which shared versions it read (from its own audit rows: every read through AgentContext is logged
with the version), whether each was the newest one available when it read (written before it, time <= T), and whether the
versions its own new output records are the ones it read. A read older than what was available is a STALE CHAIN (e.g.
Communication using recovery plan v60 when v66 existed).
"""
from backend.contracts.shared_state import SHARED

# what each producer's rows record about the versions they were built on
RECORDS = {
    "network_recovery": {"situation_assessments": "situation_version", "station_plans": "station_plan_version"},
    "passenger_care": {"situation_assessments": "situation_version", "recovery_plans": "recovery_plan_version"},
    "station_staff": {"situation_assessments": "situation_version", "recovery_plans": "recovery_plan_version",
                      "care_plans": "care_plan_version"},
}
TABLE_OF = {meta["producer"]: table for table, meta in SHARED.items()}


def newest(store, now) -> dict:
    """table -> the newest version visible at now, as it is right now in the store (call it before the reader runs)."""
    out = {}
    for table, meta in SHARED.items():
        ok = [r[meta["version"]] for r in store.records[table] if r[meta["time"]] <= now]
        out[table] = max(ok) if ok else None
    return out


def reads_from(audits) -> dict:
    """table -> the LAST version this agent read this minute (from its audit rows)."""
    out = {}
    for a in audits:
        what = a.get("what") or ""
        if what.startswith("read shared "):
            out[what[len("read shared "):]] = (a.get("after") or {}).get("version")
    return out


def audit_agent(agent, read, available, written_rows) -> list:
    """read: {table: version it read}; available: {table: newest version available before it ran}; written_rows: the rows it
    wrote to its own shared table this minute. -> findings (empty = a clean chain)."""
    findings = []
    for table, v in read.items():
        best = available.get(table)
        if best is not None and (v is None or v < best):
            findings.append({"kind": "stale_read", "agent": agent, "table": table, "read": v, "newest": best,
                             "detail": f"{agent} used {table} v{v} when v{best} existed"})
    for row in written_rows:
        for table, col in RECORDS.get(agent, {}).items():
            if table not in read:
                continue
            if col not in row:
                findings.append({"kind": "missing_version", "agent": agent, "table": table,
                                 "detail": f"{agent} v{row.get('version')} does not record which {table} version it used"})
            elif row[col] != read[table]:
                findings.append({"kind": "stale_record", "agent": agent, "table": table, "recorded": row[col], "read": read[table],
                                 "detail": f"{agent} v{row.get('version')} records {table} v{row[col]} but read v{read[table]}"})
    return findings
