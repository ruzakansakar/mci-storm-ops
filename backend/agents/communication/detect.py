"""The Communication agent, phase 4b-1: DETECT channel mismatches. It fixes nothing and writes no messages.

Each scan (every simulated minute): read what was delivered since the last scan, update what it knows (state_at.py), compare each
tracked passenger's four channels with the truth field by field, and keep one row per wrong stretch in `mismatches` (opened when
it starts, updated while it lasts, cleared when the channel matches again). For each open one: a suspected cause from what a real
system can see (its own observed lags and delivery receipts, never the simulator's reasons), with a confidence, and a severity
from the passenger's point of view. Per flight it counts how many passengers are wrong and escalates to the customer service
supervisor when the contract says so. No AI. Every row has sources with as_of times (G01) and decision ids (G08).

It reads and writes only through AgentContext. Stale feeds: nothing is compared on a channel whose feed is stale (or anywhere, if
the truth feed is stale); the scan says "cannot_check: <feed> stale since HH:MM" instead.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

import yaml

from backend.channel_causes import canonical
from backend.agents.communication.state_at import DAY0, KIND, MAIN, CommState, apply_events, build_state, compare, hhmm, new_state, show
from backend.settings import ROOT

SETTINGS_FILE = ROOT / "config" / "agent_settings" / "communication.yaml"
DECISION = {"failed_delivery": ["O03", "O11"]}             # a failed delivery also answers O11 (spot failed deliveries)
LEVELS = ["low", "medium", "high", "critical"]


@lru_cache
def load_settings() -> dict:
    return yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8"))


@dataclass
class Memory:
    """Working memory during one replay (temporary: dropped when the replay or the incident ends)."""
    state: CommState | None = None
    schedule: list | None = None
    segments: dict | None = None
    screens: dict | None = None                      # pid -> screens this passenger has (screen types only)
    events: dict = field(default_factory=dict)             # event id -> event (everything read so far)
    last_scan: datetime | None = None
    stale: set = field(default_factory=set)               # feeds stale at the last scan
    rows: dict = field(default_factory=dict)              # (pid, ch, field, since) -> {"id", "severity", "cause", "shown", "true"}
    escalated: dict = field(default_factory=dict)         # escalation key -> recommendation id
    last_summary: datetime | None = None
    counts: dict = field(default_factory=dict)
    receipts_seen: set = field(default_factory=set)       # delivery receipts already reported
    closed_done: set = field(default_factory=set)         # (key, since) of stretches already reported as cleared
    history: list = field(default_factory=list)           # what it found, for the printouts


# ---------------------------------------------------------------- cause and severity (no AI, rules only)

def usual_lag(st: CommState, ch: str, s: dict) -> float:
    c = s["cause"]
    seen = sorted(st.lags.get(ch, []))
    if len(seen) < c["min_lag_observations"]:
        return c["default_usual_lag_min"]
    return seen[min(len(seen) - 1, int(c["usual_lag_percentile"] * len(seen)))]


def cause_of(st: CommState, key: tuple, since: datetime, s: dict) -> tuple[str, float, list]:
    """What a real system can tell about why this channel is wrong: its own update history and delivery receipts."""
    pid, ch, f = key
    conf = s["cause"]["confidence"]
    kind = KIND[f]
    last = st.last_update.get((pid, ch, kind))
    failed = [r for r in st.receipts.get((pid, ch, kind), []) if last is None or r[0] >= last[0]]
    if failed:
        r = failed[-1]
        return "failed_delivery", conf["failed_delivery"], [{"event_id": r[1], "at": r[0].isoformat(), "type": "channel_update_failed"}]
    if kind == "bag":
        return "bag_system", conf["bag_system"], []
    shown = st.shown[(pid, ch)][f]
    if all(snap[f] != shown for _, _, snap in st.history.get(pid, [])):
        return "unknown", conf["unknown"], []                 # it shows something the truth never said today
    # A changed itinerary is a trip update, not proof of a seat hold. This feed
    # has no separate seat-confirmation field, so it cannot substantiate that cause.
    age = (st.t - since).total_seconds() / 60
    if age <= usual_lag(st, ch, s):
        return "waiting_for_update", conf["waiting_for_update"], []
    return "stuck", conf["stuck"], []


def flight_of(st: CommState, key: tuple):
    """The flight this mismatch is about: the passenger's next flight that hasn't left (truth first, then what is shown)."""
    pid, ch, _ = key
    for flights in (st.truth[pid]["itinerary"], st.shown[(pid, ch)]["itinerary"]):
        for fid in flights:
            f = st.flights.get(fid)
            if f and not f.done:
                return f
    return None


def severity_of(st: CommState, key: tuple, since: datetime, segment: str | None, s: dict) -> tuple[str, list[str]]:
    v = s["severity"]
    pid, ch, f = key
    truth, shown = st.truth[pid], st.shown[(pid, ch)]
    fl = flight_of(st, key)
    mins = (fl.est_dep - st.t).total_seconds() / 60 if fl else None
    age = (st.t - since).total_seconds() / 60
    why = []
    if f in MAIN:
        operating = [fid for fid in shown["itinerary"] if st.flights.get(fid) and st.flights[fid].cancelled_at
                     and st.flights[fid].cancelled_at <= st.t]
        if operating and shown["status"] != "cancelled":
            why.append(f"shows cancelled flight {operating[0]} as operating")
        if truth["status"] == "cancelled" and shown["status"] != "cancelled":
            why.append("shows a cancelled trip as going ahead")
        if segment in v["vulnerable_segments"] and mins is not None and mins <= v["vulnerable_within_min"]:
            why.append("a passenger who needs extra care, wrong about a flight leaving within "
                       f"{v['vulnerable_within_min'] // 60} h")          # the segment itself is never written in text
        if why:
            return "critical", why
    level = "medium"
    if f in ("itinerary", "status") and mins is not None and mins <= v["near_departure_min"]:
        level = "high"
        why.append(f"wrong {f} for a flight leaving within {v['near_departure_min'] // 60} h")
    if age > v["old_after_min"]:
        level = "high"
        why.append(f"wrong for more than {v['old_after_min']} min")
    if level == "medium" and mins is not None and mins > v["far_future_min"]:
        level = "low"
        why.append("about a flight more than 12 h away")
    if mins is not None and 0 <= mins <= v["boost_within_min"] and level in ("low", "medium"):
        level = LEVELS[LEVELS.index(level) + 1]
        why.append(f"about to matter: leaves within {v['boost_within_min']} min")
    return level, why or ["wrong status or time"]


# ---------------------------------------------------------------- one scan

def _stale(ctx, s) -> list[dict]:
    max_age = ctx.contract.max_data_age_sec
    out = []
    for feed in [s["data"]["truth_feed"]] + s["channels"]:
        if ctx.feed_age_sec(feed) > max_age:
            out.append({"feed": feed, "since": ctx.feed_last(feed)})
    return out


def scan(ctx, memory: Memory | None = None, write: bool = True, settings: dict | None = None, full: bool = False) -> dict:
    """Detect as of ctx.now. full=True rebuilds everything from scratch (the incremental scan must give the same answer)."""
    s = settings or load_settings()
    memory = memory or Memory()
    t = ctx.now
    if memory.schedule is None:
        memory.schedule = ctx.read("flights")
        memory.segments = {r["id"]: r["segment"] for r in ctx.read("passengers", ["id", "segment"])}
        memory.screens = ctx.passenger_screens()      # screen types only: app/SMS exist only for passengers who have them
    stale = _stale(ctx, s)
    frozen = {"*"} if any(x["feed"] == s["data"]["truth_feed"] for x in stale) else {x["feed"] for x in stale}
    came_back = memory.stale - {x["feed"] for x in stale}
    # read what was delivered since the last scan; after a silence, also what the feed held back (it happened earlier)
    if memory.last_scan is None or full:
        new = ctx.read_events()
    else:
        new = ctx.read_events(delivered_since=memory.last_scan)
        if came_back:
            new += ctx.read_events(since=DAY0 - timedelta(days=1))
    for e in new:
        memory.events.setdefault(e["id"], e)
    if memory.state is None or full or came_back:
        memory.state = build_state(memory.schedule, list(memory.events.values()), t, s["channels"], frozen,
                                   memory.screens)
        st = memory.state
        st.closed = [c for c in st.closed if (tuple(c[0]), c[1]) not in memory.closed_done]     # already reported
    else:
        st = memory.state
        st.frozen = frozen
        apply_events(st, new, t)
    compare(st, t)
    st.t = t

    findings = []
    for key, m in sorted(st.open.items()):
        pid, ch, f = key
        cause, conf, ev = cause_of(st, key, m["since"], s)
        cause = canonical(cause)
        sev, why = severity_of(st, key, m["since"], memory.segments.get(pid), s)
        fl = flight_of(st, key)
        findings.append({"passenger_id": pid, "channel": ch, "field": f, "shown_value": show(st.shown[(pid, ch)][f]),
                         "true_value": show(st.truth[pid][f]), "since": m["since"].isoformat(),
                         "age_min": int((t - m["since"]).total_seconds() // 60), "suspected_cause": cause, "cause_confidence": conf,
                         "severity": sev, "severity_why": why, "flight_id": fl.id if fl else None,
                         "evidence": m["evidence"] + ev, "decision_ids": DECISION.get(cause, ["O03"]),
                         "sources": _sources(st, pid, ch, ctx)})
    closed, st.closed = st.closed, []
    result = {"t": t, "findings": findings, "closed": closed, "cannot_check": [f"{x['feed']} stale since {hhmm(x['since'])}" for x in stale],
              "by_flight": _by_flight(findings), "escalations": [], "state": st}
    memory.stale = {x["feed"] for x in stale}
    memory.last_scan = t
    if write:
        _write(ctx, memory, result, s)
    return result


def _sources(st, pid, ch, ctx) -> list[dict]:
    upd = [st.last_update.get((pid, ch, k)) for k in ("main", "bag")]
    last = max((u[0] for u in upd if u), default=DAY0 + timedelta(hours=5))
    return [{"fact": "trip record (the truth)", "source": "reservations", "as_of": st.truth[pid]["at"].isoformat()},
            {"fact": f"what {ch} shows", "source": ch, "as_of": max(last, ctx.feed_last(ch)).isoformat()}]


def _by_flight(findings) -> dict:
    out = {}
    for x in findings:
        if x["flight_id"] and x["field"] != "bag_routing":
            fl = out.setdefault(x["flight_id"], {"passengers": set(), "by_channel": {}})
            fl["passengers"].add(x["passenger_id"])
            fl["by_channel"].setdefault(x["channel"], set()).add(x["passenger_id"])
    return out


# ---------------------------------------------------------------- writing (only through AgentContext)

def _row(x, run, t) -> dict:
    return {"passenger_id": x["passenger_id"], "channel": x["channel"], "field": x["field"], "shown_value": x["shown_value"],
            "true_value": x["true_value"], "cause": x["suspected_cause"], "cause_confidence": x["cause_confidence"],
            "severity": x["severity"], "flight_id": x["flight_id"], "status": "open", "since": datetime.fromisoformat(x["since"]),
            "detected_at": t, "evidence": x["evidence"], "sources": x["sources"], "decision_ids": x["decision_ids"]}


def _write(ctx, memory, result, s):
    """Everything this scan found, in a few batched writes (all through AgentContext): new rows, changed rows, cleared rows, events."""
    t, run = result["t"], ctx.run_id
    inserts, insert_keys, updates, update_ids, events, new_critical = [], [], [], [], [], []
    for key, since, ended, ev in result["closed"]:
        if (*key, since) not in memory.rows:      # it opened and closed between two scans (after a feed came back): record both
            x = {"passenger_id": key[0], "channel": key[1], "field": key[2], "shown_value": "", "true_value": "",
                 "since": since.isoformat(), "suspected_cause": "unknown", "cause_confidence": 0.3, "severity": "low", "flight_id": None,
                 "evidence": ev, "decision_ids": ["O03"], "sources": [{"source": "reservations", "as_of": since.isoformat()}]}
            inserts.append(_row(x, run, t))
            insert_keys.append(((*key, since), x, False))
    for x in result["findings"]:
        k = (x["passenger_id"], x["channel"], x["field"], datetime.fromisoformat(x["since"]))
        row = memory.rows.get(k)
        if row is None:
            inserts.append(_row(x, run, t))
            insert_keys.append((k, x, True))
            if x["severity"] == "critical":
                new_critical.append(x)
            continue
        change = {c: x[v] for c, v in (("severity", "severity"), ("cause", "suspected_cause"), ("shown_value", "shown_value"),
                                       ("true_value", "true_value")) if row[c] != x[v]}
        if change:
            vals = dict(change)
            if "cause" in change:
                vals["cause_confidence"] = x["cause_confidence"]
            updates.append(vals)
            update_ids.append(row["id"])
            row.update(change)
            if change.get("severity") == "critical":
                new_critical.append(x)
            memory.history.append({"kind": "changed", "at": t, "key": k[:3], "since": k[3], **change})
    ids = ctx.write_many("mismatches", inserts) if inserts else []
    lookup = {}
    for rid, (k, x, track) in zip(ids, insert_keys):
        row = {"id": rid, "severity": x["severity"], "cause": x["suspected_cause"], "shown_value": x["shown_value"],
               "true_value": x["true_value"]}
        lookup[k] = row
        if track:
            memory.rows[k] = row
            events.append(("mismatch_detected", {"mismatch_id": rid, "passenger_id": x["passenger_id"], "channel": x["channel"],
                                                 "field": x["field"], "severity": x["severity"], "suspected_cause": x["suspected_cause"],
                                                 "since": x["since"], "flight_id": x["flight_id"]}))
            memory.history.append({"kind": "detected", "at": t, "key": k[:3], "since": k[3], "severity": x["severity"],
                                   "cause": x["suspected_cause"], "shown": x["shown_value"], "true": x["true_value"],
                                   "why": x["severity_why"]})
    for key, since, ended, ev in result["closed"]:
        memory.closed_done.add((tuple(key), since))
        row = memory.rows.pop((*key, since), None) or lookup[(*key, since)]
        updates.append({"status": "cleared", "cleared_at": ended})
        update_ids.append(row["id"])
        events.append(("mismatch_cleared", {"mismatch_id": row["id"], "passenger_id": key[0], "channel": key[1], "field": key[2],
                                            "since": since.isoformat(), "cleared_at": ended.isoformat(),
                                            "minutes": int((ended - since).total_seconds() // 60)}))
        memory.history.append({"kind": "cleared", "at": t, "key": key, "since": since, "ended": ended, "severity": row["severity"],
                               "cause": row["cause"]})
    if updates:
        ctx.write_many("mismatches", updates, mode="update", row_ids=update_ids)
    events += _receipts(memory, result)
    sev = {}
    for x in result["findings"]:
        sev[x["severity"]] = sev.get(x["severity"], 0) + 1
    due = memory.last_summary is None or t - memory.last_summary >= timedelta(minutes=s["summary_every_min"])
    if (sev != memory.counts and (due or new_critical)) or (result["cannot_check"] and due):
        events.append(("communication_summary", {"open": len(result["findings"]), "by_severity": sev,
                                                 "cannot_check": result["cannot_check"],
                                                 "flights": {f: len(v["passengers"]) for f, v in result["by_flight"].items()}}))
        memory.last_summary, memory.counts = t, sev
    if events:
        ctx.record_events(events)
    _escalate(ctx, memory, result, s)
    memory.history.append({"kind": "scan", "at": t, "open": dict(sev), "new_critical": [(x["passenger_id"], x["channel"], x["field"])
                                                                                        for x in new_critical],
                           "cannot_check": result["cannot_check"]})


def _receipts(memory, result) -> list:
    out = []
    for when, ch, pid, eid in result["state"].all_receipts:
        if eid not in memory.receipts_seen:
            memory.receipts_seen.add(eid)
            out.append(("delivery_failure_detected", {"passenger_id": pid, "channel": ch, "at": when.isoformat(), "event_id": eid}))
    return out


def _escalate(ctx, memory, result, s):
    """The contract's escalate_when: a recommendation to the customer service supervisor, once per problem while it lasts."""
    e, t, st = s["escalate"], result["t"], result["state"]
    wanted = {}
    for fid, v in result["by_flight"].items():
        if len(v["passengers"]) > e["flight_passengers_wrong"]:
            wanted[("flight", fid)] = (f"{len(v['passengers'])} passengers are shown wrong information about {fid}",
                                       [x for x in result["findings"] if x["flight_id"] == fid and x["field"] != "bag_routing"], fid)
    vuln = s["severity"]["vulnerable_segments"]
    for x in result["findings"]:
        if x["severity"] == "critical" and memory.segments.get(x["passenger_id"]) in vuln and x["age_min"] > e["vulnerable_wrong_min"]:
            wanted[("care", x["passenger_id"])] = (f"A passenger who needs extra care has been shown wrong flight information for "
                                                   f"{x['age_min']} min", [x], x["flight_id"])
    window = timedelta(minutes=e["failures_window_min"])
    for ch in s["channels"]:
        n = sum(1 for when, c, _, _ in st.all_receipts if c == ch and t - window < when <= t)
        if n > e["failures_per_window"]:
            wanted[("failures", ch)] = (f"{n} deliveries failed on {ch} in the last {e['failures_window_min']} min",
                                        [x for x in result["findings"] if x["channel"] == ch and x["suspected_cause"] == "failed_delivery"],
                                        None)
    for k in list(memory.escalated):
        if k not in wanted:
            memory.escalated.pop(k)                  # the problem is over: if it comes back, it is a new escalation
    for k, (title, items, fid) in wanted.items():
        if k in memory.escalated:
            continue
        clean = [{c: x[c] for c in ("passenger_id", "channel", "field", "shown_value", "true_value", "since", "age_min",
                                    "suspected_cause", "cause_confidence", "severity", "flight_id", "evidence", "sources", "decision_ids")}
                 for x in items[:20]]
        ids = sorted({d for x in items for d in x["decision_ids"]}) or ["O03"]
        out = {"title": title, "why": "; ".join(sorted({w for x in items for w in x.get("severity_why", [])}))[:500] or title,
               "data_as_of": t.isoformat(), "options": [], "findings": clean, "flight_id": fid, "decision_ids": ids,
               "sources": [{"fact": "trip records and channel updates", "source": "reservations and channels", "as_of": t.isoformat()}]}
        rid = ctx.propose("escalate_channel_problem", out)
        memory.escalated[k] = rid
        result["escalations"].append({"key": k, "id": rid, "title": title})
        memory.history.append({"kind": "escalation", "at": t, "title": title})
