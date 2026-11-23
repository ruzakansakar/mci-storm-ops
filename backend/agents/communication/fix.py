"""Propose confirmed screen corrections through the contract boundary.

No delivery implementation, world state, names, contact addresses or generated
message content enters this policy. Phase 4c can supply additional conflict IDs
through open_conflicts; until then contradictory published flight signals hold.
"""
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.agents.communication.detect import LEVELS, load_settings
from backend.agents.communication.state_at import FIELDS


@dataclass
class FixMemory:
    pushes: dict = field(default_factory=lambda: defaultdict(deque))
    sent_versions: dict = field(default_factory=dict)
    held: set = field(default_factory=set)


def conflicts_from_events(events):
    """Conservative local A09 check; no Situation/shared-state reads.

    Cancellation followed by departure/arrival is contradictory until an
    explicit source-conflict resolution. The caller may add conflicts in 4c.
    """
    states, conflicts = defaultdict(set), set()
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
        p, kind = e.get("payload") or {}, e["type"]
        fid = p.get("flight_id")
        if not fid:
            continue
        if kind == "source_conflict_opened":
            conflicts.add(fid)
        elif kind == "source_conflict_resolved":
            conflicts.discard(fid)
            states[fid].clear()
        elif kind in ("flight_cancelled", "flight_departed", "flight_arrived"):
            states[fid].add(kind)
            if "flight_cancelled" in states[fid] and len(states[fid]) > 1:
                conflicts.add(fid)
    return conflicts


def propose_fixes(ctx, result, memory, events=(), open_conflicts=(), settings=None):
    s = (settings or load_settings())["fix"]
    st, now = result["state"], ctx.now
    if ctx.feed_age_sec("reservations") > ctx.contract.max_data_age_sec:
        key = ("truth feed stale", ctx.feed_last("reservations"))
        if key not in memory.held:
            ctx.record_event("channel_fix_held", {"reason": "truth feed stale", "as_of": now.isoformat()})
            memory.held.add(key)
        return []
    conflicts = set(open_conflicts) | conflicts_from_events(events)
    grouped = defaultdict(list)
    for finding in result["findings"]:
        grouped[(finding["passenger_id"], finding["channel"])].append(finding)

    def priority(item):
        _, findings = item
        dep = [st.flights[x["flight_id"]].est_dep for x in findings if x.get("flight_id") in st.flights]
        return (-max(LEVELS.index(x["severity"]) for x in findings),
                min(dep, default=now + timedelta(days=s["unknown_departure_days"])),
                min(x["since"] for x in findings), item[0])

    proposals = []
    for (pid, ch), findings in sorted(grouped.items(), key=priority):
        truth = st.truth[pid]
        severity = max((x["severity"] for x in findings), key=LEVELS.index)
        flight_ids = set(truth["itinerary"]) | set(st.shown[(pid, ch)]["itinerary"])
        reason = None
        if ctx.feed_age_sec("reservations") > ctx.contract.max_data_age_sec:
            reason = "truth feed stale"
        elif ctx.feed_age_sec("flight_ops") > ctx.contract.max_data_age_sec:
            reason = "flight operations feed stale"
        elif flight_ids & conflicts:
            reason = "open source conflict (A09)"
        elif severity != "critical" and (now - truth["at"]).total_seconds() < s["settle_min"] * 60:
            reason = "trip record still settling"
        elif ctx.feed_age_sec(ch) > ctx.contract.max_data_age_sec:
            reason = "channel feed stale"
        if reason:
            key = (pid, ch, reason, truth["at"])
            if key not in memory.held:
                ctx.record_event("channel_fix_held", {"passenger_id": pid, "channel": ch, "reason": reason,
                                                      "as_of": now.isoformat()})
                memory.held.add(key)
            continue
        failed = any(x["suspected_cause"] == "failed_delivery" for x in findings)
        stuck = max(x["age_min"] for x in findings) >= s["resend_after_min"]
        targets = [(ch, "fix_channel_mismatch")]
        if failed or stuck:
            available = ctx.contact_channel_types(pid)
            alternate = next((c for c in s["alternate_order"] if c != ch and c in available), None)
            if alternate:
                targets.append((alternate, "resend_on_other_channel"))
        for target, action in targets:
            key = (pid, target)
            pushes = memory.pushes[key]
            while pushes and now - pushes[0] >= timedelta(minutes=s["window_min"]):
                pushes.popleft()
            if len(pushes) >= s["max_pushes"]:
                continue
            previous = memory.sent_versions.get(key)
            if previous and previous[0] == truth["at"] and now - previous[1] < timedelta(minutes=s["retry_min"]):
                continue
            confirmed = {f: list(truth[f]) if isinstance(truth[f], tuple) else truth[f] for f in FIELDS}
            option = {"agent": "communication", "action": action, "passenger_id": pid, "channel": target,
                      "original_channel": ch, "confirmed": confirmed, "truth_as_of": truth["at"].isoformat(),
                      "data_as_of": now.isoformat(), "decision_ids": ["O03", "O11"], "severity": severity,
                      "flight_id": findings[0].get("flight_id"), "requested_level": "auto",
                      "reason": "delivery failed" if failed else "channel stuck" if stuck else "screen differs from confirmed trip",
                      "sources": findings[0]["sources"]}
            output = {"title": f"Refresh {target} with the confirmed trip", "why": option["reason"],
                      "data_as_of": now.isoformat(), "options": [option], "sources": option["sources"],
                      "decision_ids": option["decision_ids"], "findings": findings}
            rid = ctx.propose(action, output)
            proposals.append({**option, "id": rid, "data_as_of": now})
            pushes.append(now)
            memory.sent_versions[key] = (truth["at"], now)
    return proposals
