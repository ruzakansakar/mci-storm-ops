"""Propose confirmed screen corrections through the contract boundary.

No delivery implementation, world state, names, contact addresses or generated
message content enters this policy. Source conflicts come from the Situation agent
(Phase 4c shared state, backend/agents/communication/situation_view.py): no fix on a
flight where Situation reports conflicting data, until a later version clears it.
"""
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.agents.communication.detect import LEVELS, load_settings, usual_lag
from backend.agents.communication.state_at import FIELDS


@dataclass
class FixMemory:
    pushes: dict = field(default_factory=lambda: defaultdict(deque))
    sent_versions: dict = field(default_factory=dict)
    held: set = field(default_factory=set)
    resent: set = field(default_factory=set)          # (passenger, other channel, trip version) already resent once


def _last_failure(findings):
    """When the newest delivery receipt (failed delivery) among these findings arrived, if any."""
    times = [datetime.fromisoformat(e["at"]) for x in findings for e in x.get("evidence", ())
             if e.get("type") == "channel_update_failed" and e.get("at")]
    return max(times, default=None)


def propose_fixes(ctx, result, memory, situation=None, settings=None):
    full = settings or load_settings()
    s = full["fix"]
    st, now = result["state"], ctx.now
    if ctx.feed_age_sec("reservations") > ctx.contract.max_data_age_sec:
        key = ("truth feed stale", ctx.feed_last("reservations"))
        if key not in memory.held:
            ctx.record_event("channel_fix_held", {"reason": "truth feed stale", "as_of": now.isoformat()})
            memory.held.add(key)
        return []
    from backend.agents.communication.situation_view import SituationView, held_fields, note_hold
    situation = situation or SituationView()
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
        reason, extra = None, {}
        conflict = held_fields(situation, flight_ids, full)          # (trip details held, flights, reason) or None
        wrong = {x["field"] for x in findings}
        hold_only = set()
        if ctx.feed_age_sec("reservations") > ctx.contract.max_data_age_sec:
            reason = "truth feed stale"
        elif ctx.feed_age_sec("flight_ops") > ctx.contract.max_data_age_sec:
            reason = "flight operations feed stale"
        elif conflict and wrong <= conflict[0]:
            # every wrong detail is one a Situation conflict is open about: hold (and ask a human, see escalate_holds)
            reason = conflict[2]
            extra = {"kind": "situation_conflict", "flight_ids": conflict[1], "fields": sorted(conflict[0]),
                     "situation_version": situation.version}
            for fid in conflict[1]:
                note_hold(situation, fid, pid, now)
        elif severity != "critical" and (now - truth["at"]).total_seconds() < s["settle_min"] * 60:
            reason = "trip record still settling"
        elif ctx.feed_age_sec(ch) > ctx.contract.max_data_age_sec:
            reason = "channel feed stale"
        if not reason and conflict:
            # only the conflicted details are held; everything else on this screen is still fixed now
            hold_only = conflict[0]
            key = (pid, ch, "partial " + conflict[2], truth["at"])
            if key not in memory.held:
                ctx.record_event("channel_fix_held", {"passenger_id": pid, "channel": ch, "reason": conflict[2], "partial": True,
                                                      "kind": "situation_conflict", "flight_ids": conflict[1],
                                                      "fields": sorted(hold_only), "as_of": now.isoformat(),
                                                      "situation_version": situation.version})
                memory.held.add(key)
            for fid in conflict[1]:
                note_hold(situation, fid, pid, now)
        if reason:
            key = (pid, ch, reason, truth["at"])
            if key not in memory.held:
                ctx.record_event("channel_fix_held", {"passenger_id": pid, "channel": ch, "reason": reason,
                                                      "as_of": now.isoformat(), **extra})
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
        failure_at = _last_failure(findings)
        for target, action in targets:
            key = (pid, target)
            pushes = memory.pushes[key]
            while pushes and now - pushes[0] >= timedelta(minutes=s["window_min"]):
                pushes.popleft()
            if len(pushes) >= s["max_pushes"]:
                continue
            if action == "resend_on_other_channel":
                # The same trip version goes to another channel once: that channel is not wrong, so repeating it is spam.
                if (pid, target, truth["at"]) in memory.resent:
                    continue
            else:
                previous = memory.sent_versions.get(key)
                if previous and previous[0] == truth["at"]:
                    # Do not repeat a version while its delivery is still expected: wait at least retry_min, and at least
                    # the channel's usual lag as the agent has measured it today, unless a failed-delivery receipt came since.
                    wait = max(s["retry_min"], usual_lag(st, target, full))
                    failed_since = failure_at is not None and failure_at > previous[1]
                    if not failed_since and now - previous[1] < timedelta(minutes=wait):
                        continue
            confirmed = {f: list(truth[f]) if isinstance(truth[f], tuple) else truth[f] for f in FIELDS if f not in hold_only}
            option = {"agent": "communication", "action": action, "passenger_id": pid, "channel": target,
                      "original_channel": ch, "confirmed": confirmed, "truth_as_of": truth["at"].isoformat(),
                      "data_as_of": now.isoformat(), "decision_ids": ["O03", "O11"], "severity": severity,
                      "situation_version": situation.version, "held_fields": sorted(hold_only),
                      "flight_id": findings[0].get("flight_id"), "requested_level": "auto",
                      "reason": "delivery failed" if failed else "channel stuck" if stuck else "screen differs from confirmed trip",
                      "sources": findings[0]["sources"]}
            output = {"title": f"Refresh {target} with the confirmed trip", "why": option["reason"],
                      "data_as_of": now.isoformat(), "options": [option], "sources": option["sources"],
                      "decision_ids": option["decision_ids"], "findings": findings}
            rid = ctx.propose(action, output)
            proposals.append({**option, "id": rid, "data_as_of": now})
            pushes.append(now)
            if action == "resend_on_other_channel":
                memory.resent.add((pid, target, truth["at"]))
            else:
                memory.sent_versions[key] = (truth["at"], now)
    return proposals
