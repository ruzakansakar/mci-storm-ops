"""The Situation agent: is something abnormal happening, what is it and why, how long will it last, who and what is affected.

It reads only through AgentContext (which cuts events at its current time, hides the simulator's answer key and anything a real
source wouldn't publish) and writes only through it. Rules decide the cause, the times and the numbers; no AI.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

import yaml

from backend.agents.situation.state_at import DAY0, State, build_state, hhmm
from backend.settings import ROOT

SETTINGS_FILE = ROOT / "config" / "agent_settings" / "situation.yaml"


@lru_cache
def load_settings() -> dict:
    return yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8"))


@dataclass
class Memory:
    """The agent's working memory during one replay (temporary: dropped when the replay or incident ends)."""
    version: int = 0
    last: dict | None = None              # the last version written (comparable form)
    last_id: int | None = None
    events: list = field(default_factory=list)
    last_read: datetime | None = None
    schedule: list | None = None
    proposed: set = field(default_factory=set)
    last_p02: datetime | None = None
    history: list = field(default_factory=list)   # what it wrote and proposed (for the replay printout)


# ---------------------------------------------------------------- the pieces of an assessment

def _detect(st: State, s: dict) -> tuple[bool, list[str]]:
    d = s["detect"]
    why = []
    p = st.program
    if p and p.ended is None:
        why.append(f"FAA {p.kind.replace('_', ' ')} active")
    if not st.ramp_open:
        why.append("ramp closed")
    recent = [f for when, f in st.newly_delayed if st.t - when <= timedelta(minutes=d["delayed_departures_window_min"])]
    if len(recent) >= d["delayed_departures_threshold"]:
        why.append(f"{len(recent)} departures delayed in the last {d['delayed_departures_window_min']} min")
    out = [f for f in st.flights.values() if f.direction == "arrival" and f.status in ("holding", "diverted", "resumed")]
    if len(out) >= d["holding_or_diverted_inbound"]:
        why.append(f"{len(out)} inbound flights holding or diverted")
    return bool(why), why


def _classify(st: State, health: list[dict], s: dict) -> dict:
    """A02: map the signals to a type, with a confidence from how many independent sources agree, and the evidence."""
    c = s["classify"]
    votes, evidence = {}, []

    def vote(kind, source, eids, note):
        votes.setdefault(kind, set()).add(source)
        for eid, when, typ in eids:
            evidence.append({"event_id": eid, "at": when.isoformat(), "type": typ, "source": source, "points_to": kind, "note": note})

    progs = ([st.program] if st.program else []) + st.past_programs
    for p in progs:
        reason = (p.reason or "").lower()
        hit = [k for k, words in c["faa_reason_words"].items() if any(w in reason for w in words)]
        if hit:
            vote(hit[0], "faa_feed", p.notices[:1], f"FAA notice reason: '{p.reason}'")
        elif p.notices:
            vote("faa_program", "faa_feed", p.notices[:1], "FAA program with no stated weather reason")
    for start, end, eids, reason in st.ramp_closures:
        if reason and "lightning" in reason:
            vote("weather", "weather_feed", [(eids[0], start, "ramp_closed")], "lightning detected near the ramp")
    codes = st.delay_reasons
    if codes:
        top = max(codes, key=codes.get)
        delayed = [f for f in st.flights.values() if f.delay_reason == top]
        mapped = "weather" if top == "weather" else top if top in c["controllable_by_type"] else None
        if mapped:
            vote(mapped, "flight_ops", [delayed[0].events[0]] if delayed and delayed[0].events else [],
                 f"{codes[top]} delay reports coded '{top}'")
    down = [h for h in health if h["status"] != "up"]
    if down:
        votes.setdefault("it_outage", set()).add("system_health")
        evidence.append({"event_id": None, "at": None, "type": "system_health", "source": "system_health", "points_to": "it_outage",
                         "note": f"{', '.join(h['system'] for h in down)} not up"})
    crew = [f for f in st.flights.values() if f.cancel_reason == "crew out of legal duty"]
    external = {k for k in votes if k != "crew"}
    if len(crew) >= c["crew_cancellations_for_crew_cause"] and not external:
        vote("crew", "flight_ops", [crew[0].events[-1]], f"{len(crew)} cancellations for crew out of legal duty")

    if not votes:
        return {"type": "unknown", "likely_cause": "abnormal signals with no stated cause yet", "confidence": 0.3, "evidence": evidence}
    best = max(sorted(votes), key=lambda k: len(votes[k]))
    agree = len(votes[best])
    ladder = c["confidence_by_agreeing_sources"]
    conf = ladder[min(agree, len(ladder)) - 1]
    others = [k for k in votes if k != best]
    if others:
        conf = min(conf, c["confidence_if_signals_disagree"])
    words = {"weather": "thunderstorms / lightning at MCI", "faa_program": "an FAA traffic program at MCI",
             "it_outage": "a Southwest systems outage", "airport": "an airport problem (runway or power)",
             "security": "a security event", "crew": "crews running out of legal duty"}
    sources = sorted(votes[best])
    return {"type": best, "likely_cause": f"suspected: {words.get(best, best)} (signals from {', '.join(sources)})",
            "confidence": round(conf, 2), "evidence": evidence, "disagreeing": others}


def _controllable(st: State, kind: str, s: dict) -> tuple[bool | None, list[dict]]:
    """A03: incident level (the cause itself) and one proposed value per cancelled or long-delayed flight, with evidence."""
    c = s["classify"]
    incident = c["controllable_by_type"].get(kind)
    rows = []
    for f in sorted(st.flights.values(), key=lambda f: f.id):
        long_delay = f.status in ("delayed",) and f.delay_min >= c["long_delay_min"]
        if f.status != "cancelled" and not long_delay:
            continue
        reason = f.cancel_reason if f.status == "cancelled" else f"delay coded '{f.delay_reason}'"
        review = f.status == "cancelled" and incident is False and any(r in (reason or "") for r in c["needs_review_reasons"])
        if review:
            proposed, why = "needs_human_review", (f"cancelled for '{reason}' during an uncontrollable event: whether this was within "
                                                    "Southwest's control decides what passengers are owed")
        elif incident is None:
            proposed, why = "needs_human_review", "the cause of the event is not known yet"
        else:
            proposed = "controllable" if incident else "uncontrollable"
            why = f"{reason}; follows the event's cause ({kind})"
        rows.append({"flight_id": f.id, "kind": f.status if f.status == "cancelled" else "long_delay", "reason": reason,
                     "proposed": proposed, "why": why, "decided_by": "customer_service_supervisor",
                     "evidence": [{"event_id": e[0], "at": e[1].isoformat(), "type": e[2]} for e in f.events[-2:]]})
    return incident, rows


def _expected_end(st: State, kind: str, s: dict) -> dict:
    """A05: earliest / likely / latest. Never a single time while it is still running."""
    x = s["expected_end"]
    p = st.program
    if p is None and st.ramp_open:
        return {"earliest": None, "likely": None, "latest": None, "confidence": None, "what": None, "extensions": 0, "ended": None}
    if p is not None and p.ended is not None:
        if st.ramp_open:
            return {"earliest": p.ended, "likely": p.ended, "latest": p.ended, "confidence": 1.0, "what": "ended",
                    "extensions": p.extensions, "ended": p.ended}
    if p is None or p.ended is not None:      # only the ramp is closed
        likely = st.ramp_expected_open or st.t
        return {"earliest": likely, "likely": likely, "latest": likely + timedelta(minutes=x["base_margin_min"]["weather"]),
                "confidence": x["base_confidence"], "what": "ramp", "extensions": 0, "ended": p.ended if p else None}
    running_h = max(0.0, (st.t - p.started).total_seconds() / 3600)
    margin = (x["base_margin_min"].get(kind, x["base_margin_min"]["unknown"]) + x["margin_per_extension_min"] * p.extensions
              + x["margin_per_hour_running_min"] * running_h)
    margin = min(margin, x["max_margin_min"])
    conf = max(x["min_confidence"], x["base_confidence"] - x["confidence_drop_per_extension"] * p.extensions)
    likely = p.announced_end
    return {"earliest": likely - timedelta(minutes=x["early_margin_min"]), "likely": likely,
            "latest": likely + timedelta(minutes=round(margin)), "confidence": round(conf, 2), "what": p.kind,
            "extensions": p.extensions, "ended": None}


def _impact(st: State, end: dict, s: dict) -> dict:
    """A07 (and Q05): counts as of T. Flight ids and counts only; no names."""
    i = s["impact"]
    p = st.program
    if p:
        lo = p.started
        hi = (end["latest"] or p.announced_end) + timedelta(minutes=i["window_after_latest_end_min"])
    else:
        lo, hi = st.t - timedelta(hours=1), st.t + timedelta(minutes=i["window_after_latest_end_min"])
    deps = [f for f in st.flights.values() if f.direction == "departure" and lo <= f.sched_dep <= hi]
    arrs = [f for f in st.flights.values() if f.direction == "arrival" and lo <= f.sched_arr <= hi]

    def counts(fl):
        out = {k: 0 for k in ("delayed", "holding", "diverted", "cancelled", "scheduled", "departed", "arrived", "resumed")}
        for f in fl:
            out[f.status] = out.get(f.status, 0) + 1
        out["ever_delayed_30_plus"] = sum(1 for f in fl if f.delay_min >= 30)
        out["ever_diverted"] = sum(1 for f in fl if any(e[2] == "flight_diverted" for e in f.events))
        return out

    affected = [f for f in deps + arrs if f.status in ("delayed", "holding", "diverted", "resumed", "cancelled") or f.delay_min >= 30
                or any(e[2] == "flight_diverted" for e in f.events)]
    closed_min = 0
    for start, stop, _, _ in st.ramp_closures:
        closed_min += int(((stop or st.t) - start).total_seconds() // 60)

    def score(f):
        if f.status == "cancelled":
            return 10000
        if any(e[2] == "flight_diverted" for e in f.events) and f.status != "arrived":
            return 9000
        if f.status == "holding":
            return 8000
        return f.delay_min

    worst = sorted(affected, key=lambda f: (-score(f), f.id))[: i["worst_affected"]]

    def why(f):
        if f.status == "cancelled":
            return f"cancelled: {f.cancel_reason}"
        if any(e[2] == "flight_diverted" for e in f.events) and f.status != "arrived":
            return f"diverted to {f.diverted_to}" + (" (on its way back)" if f.status == "resumed" else "")
        if f.status == "holding":
            return "holding over MCI"
        return f"delayed {f.delay_min} min"

    crowd = st.crowd or {}
    return {
        "window": {"from": lo.isoformat(), "to": hi.isoformat()},
        "departures": counts(deps), "arrivals": counts(arrs),
        "flights_affected": len(affected),
        "passengers_on_affected_flights": sum(f.pax or 0 for f in affected),
        "flights_with_unknown_passenger_count": sum(1 for f in affected if f.pax is None),
        "missed_connections": st.missed_connections,
        "crews_on_affected_flights": len({f.crew_id for f in affected if f.crew_id}),
        "aircraft_out_of_position": sorted(f.id for f in st.flights.values() if f.status in ("diverted", "resumed")),
        "ramp": {"open": st.ramp_open, "closed_minutes_so_far": closed_min,
                 "closures": [{"from": a.isoformat(), "to": b.isoformat() if b else None} for a, b, _, _ in st.ramp_closures]},
        "terminal": {"people": crowd.get("people_in_terminal"), "capacity": crowd.get("terminal_capacity"),
                     "as_of": st.crowd_at.isoformat() if st.crowd_at else None},
        "stranded_overnight": st.stranded_overnight, "stranded_overnight_local": st.stranded_local,
        "next_day_flights": st.next_day_flights,
        "worst_affected": [{"flight_id": f.id, "why": why(f)} for f in worst],
    }


def _conflicts(st: State, health: list[dict], stale: list[dict], s: dict) -> list[dict]:
    """A09: facts that disagree. Both sides are recorded; the agent never picks one."""
    d = s["data"]
    out = []
    for fid, eid, when in st.departed_during_stop:
        out.append({"key": f"departed-during-stop:{fid}", "what": f"{fid} reported departed while the stop is on and the ramp closed",
                    "values": [{"source": "flight_ops", "says": f"{fid} departed at {hhmm(when)}", "event_id": eid},
                               {"source": "faa_feed / weather_feed", "says": "ground stop active and ramp closed"}]})
    p = st.program
    if p and p.ended and st.t - p.ended >= timedelta(minutes=d["holding_after_stop_end_min"]):
        holding = sorted(f.id for f in st.flights.values() if f.status == "holding" and f.hold_since and f.hold_since <= p.ended)
        if holding:
            out.append({"key": f"holding-after-end:{p.ended.isoformat()}",
                        "what": f"the FAA says the stop ended at {hhmm(p.ended)} but {len(holding)} flight(s) holding since before then "
                                f"are still holding {int((st.t - p.ended).total_seconds() // 60)} min later",
                        "values": [{"source": "faa_feed", "says": f"stop ended {hhmm(p.ended)}"},
                                   {"source": "flight_ops", "says": f"still holding: {', '.join(holding)}"}]})
    by_system = {h["system"]: h for h in health}
    for st_ in stale:
        system = d["system_for_feed"].get(st_["source"])
        if system and by_system.get(system, {}).get("status") == "up":
            out.append({"key": f"health-up-feed-stopped:{st_['source']}:{st_['since']}",
                        "what": f"system health says {system} is up, but its feed has sent nothing since {st_['since']}",
                        "values": [{"source": "system_health", "says": f"{system}: up"},
                                   {"source": st_["source"], "says": f"last heartbeat {st_['since']}"}]})
    return out


def _summary(a: dict, st: State) -> str:
    """A fixed template: 3-6 plain sentences for operations staff (the AI hook is off by default)."""
    if a["status"].startswith("cannot_assess"):
        return (f"Cannot assess the situation at MCI: {a['status'].split(': ', 1)[1]}. Nothing new is concluded until the feed is back. "
                f"The last known picture stays as it was.")
    if a["status"] == "normal":
        return "Normal operations at MCI. No FAA program, the ramp is open, and no unusual delays or diversions."
    imp = a["impact"]
    s = []
    p = st.program
    if p and p.ended is None:
        ext = {0: "", 1: " It was extended once.", 2: " It was extended twice."}.get(p.extensions, f" It was extended {p.extensions} times.")
        s.append(f"{p.kind.replace('_', ' ').capitalize()} at MCI since {hhmm(p.started)} for {p.reason or 'an unstated reason'} (FAA). "
                 f"Expected to end {hhmm(a['end_likely'])} (range {hhmm(a['end_earliest'])}-{hhmm(a['end_latest'])}, "
                 f"{a['end_confidence_word']} confidence).{ext}")
    elif p and p.ended:
        s.append(f"The {p.kind.replace('_', ' ')} at MCI ({hhmm(p.started)}-{hhmm(p.ended)}, {p.reason}) has ended; recovery is under way.")
    s.append(f"Suspected cause: {a['likely_cause'].replace('suspected: ', '')} (confidence {a['confidence']:.2f}).")
    d, r = imp["departures"], imp["arrivals"]
    s.append(f"{d['ever_diverted'] + r['ever_diverted']} inbound flights diverted, {d['cancelled'] + r['cancelled']} cancelled, "
             f"{d['ever_delayed_30_plus'] + r['ever_delayed_30_plus']} delayed 30+ min, about {imp['passengers_on_affected_flights']:,} "
             f"passengers on affected flights.")
    ramp = imp["ramp"]
    if ramp["closures"]:
        spans = ", ".join(f"{hhmm(datetime.fromisoformat(c['from']))}-{hhmm(datetime.fromisoformat(c['to'])) if c['to'] else 'now'}"
                          for c in ramp["closures"])
        s.append(f"Ramp {'closed' if not ramp['open'] else 'was closed'} for lightning {spans}.")
    review = [f for f in a["flight_controllability"] if f["proposed"] == "needs_human_review"]
    if review:
        s.append(f"{len(review)} cancellation(s) ({', '.join(f'{f['flight_id']}, {f['reason']}' for f in review)}) need a human to "
                 f"decide if they were within Southwest's control.")
    if imp["stranded_overnight"]:
        s.append(f"{imp['stranded_overnight']} passengers stranded overnight so far ({imp['stranded_overnight_local']} live locally).")
    return " ".join(s[:6])


def _word(conf, s):
    if conf is None:
        return "no"
    w = s["expected_end"]["confidence_words"]
    return "high" if conf >= w["high"] else "medium" if conf >= w["medium"] else "low"


# ---------------------------------------------------------------- one assessment

def assess(ctx, memory: Memory | None = None, write: bool = True, settings: dict | None = None) -> dict:
    """Assess the situation as of ctx.now. Writes a new version only if something meaningful changed (and write=True)."""
    s = settings or load_settings()
    memory = memory or Memory()
    t = ctx.now
    new = ctx.read_events(since=memory.last_read)
    memory.events.extend(new)
    memory.last_read = t
    if memory.schedule is None:
        memory.schedule = ctx.read("flights")
    health = ctx.read("system_health")
    st = build_state(memory.schedule, memory.events, t)

    max_age = ctx.contract.max_data_age_sec
    stale = []
    for feed in s["data"]["depends_on"]:
        age = ctx.feed_age_sec(feed)
        if age > max_age:
            stale.append({"source": feed, "since": hhmm(ctx.feed_last(feed)), "minutes": int(age // 60)})
    sources = [{"fact": f"{feed} heartbeat", "source": feed, "as_of": ctx.feed_last(feed).isoformat()} for feed in s["data"]["depends_on"]]
    sources.append({"fact": "published schedule", "source": "flights schedule (published before the day)", "as_of": DAY0.isoformat()})
    data_as_of = min(ctx.feed_last(f) for f in s["data"]["depends_on"])

    a = {"assessed_at": t, "stale_sources": stale, "conflicts": [], "flight_controllability": [], "evidence": [], "impact": {},
         "type": None, "likely_cause": None, "confidence": None, "incident_controllable": None,
         "end_earliest": None, "end_likely": None, "end_latest": None, "end_confidence": None, "end_confidence_word": "no",
         "data_as_of": data_as_of, "sources": sources, "abnormal_because": []}
    if stale:
        first = stale[0]
        a["status"] = f"cannot_assess: {first['source']} stale since {first['since']}"
        a["decision_ids"] = ["A08"]
        a["conflicts"] = _conflicts(st, health, stale, s)
        if a["conflicts"]:
            a["decision_ids"].append("A09")
    else:
        abnormal, because = _detect(st, s)
        a["abnormal_because"] = because
        if not abnormal:
            a["status"] = "normal"
            a["decision_ids"] = ["A01"]
        else:
            a["status"] = "abnormal"
            cls = _classify(st, health, s)
            a.update(type=cls["type"], likely_cause=cls["likely_cause"], confidence=cls["confidence"], evidence=cls["evidence"])
            a["incident_controllable"], a["flight_controllability"] = _controllable(st, cls["type"], s)
            end = _expected_end(st, cls["type"], s)
            a.update(end_earliest=end["earliest"], end_likely=end["likely"], end_latest=end["latest"], end_confidence=end["confidence"],
                     end_confidence_word=_word(end["confidence"], s))
            a["impact"] = _impact(st, end, s)
            a["conflicts"] = _conflicts(st, health, stale, s)
            a["decision_ids"] = ["A01", "A02", "A03", "A05", "A07"] + (["A09"] if a["conflicts"] else []) + \
                                (["Q05"] if st.stranded_overnight else [])
            a["_extensions"] = end["extensions"]
    a["summary"] = _summary(a, st)
    a["_state"] = st

    snap = _snapshot(a)
    changed = _changed(memory.last, snap, s)
    a["changed"] = changed
    result = {"assessment": a, "wrote": False, "proposals": [], "events": []}
    if not write:
        return result
    if changed:
        _write(ctx, memory, a, snap, result)
    if memory.last_id is not None:
        _propose(ctx, memory, a, st, result, s)
    return result


def _snapshot(a):
    imp = a["impact"] or {}
    d, r = imp.get("departures", {}), imp.get("arrivals", {})
    return {"status": a["status"], "type": a["type"], "confidence": a["confidence"],
            "ends": [a["end_earliest"], a["end_likely"], a["end_latest"]],
            "cancelled": d.get("cancelled", 0) + r.get("cancelled", 0),
            "diverted": d.get("ever_diverted", 0) + r.get("ever_diverted", 0),
            "conflicts": sorted(c["key"] for c in a["conflicts"]), "stale": sorted(x["source"] for x in a["stale_sources"])}


def _changed(last, snap, s) -> bool:
    w = s["write_when"]
    if last is None:
        return snap["status"] != "normal"            # normal operations and nothing before: write nothing
    if snap["status"] != last["status"] or snap["type"] != last["type"]:
        return True
    if (snap["confidence"] is None) != (last["confidence"] is None):
        return True
    if snap["confidence"] is not None and abs(snap["confidence"] - last["confidence"]) >= w["confidence_change"] - 1e-9:
        return True
    for x, y in zip(snap["ends"], last["ends"]):
        if (x is None) != (y is None) or (x and y and abs((x - y).total_seconds()) >= w["expected_end_change_min"] * 60):
            return True
    if snap["cancelled"] > last["cancelled"] or snap["diverted"] > last["diverted"]:
        return True
    return bool(set(snap["conflicts"]) - set(last["conflicts"]) or set(snap["stale"]) - set(last["stale"]))


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def _write(ctx, memory, a, snap, result):
    memory.version += 1
    rid = ctx.write_assessment({
        "version": memory.version, "assessed_at": a["assessed_at"], "status": a["status"], "type": a["type"],
        "likely_cause": a["likely_cause"], "confidence": a["confidence"], "incident_controllable": a["incident_controllable"],
        "flight_controllability": a["flight_controllability"], "expected_end_earliest": a["end_earliest"],
        "expected_end_likely": a["end_likely"], "expected_end_latest": a["end_latest"], "end_confidence": a["end_confidence"],
        "impact": _jsonable(a["impact"]), "conflicts": a["conflicts"], "stale_sources": a["stale_sources"],
        "evidence": _jsonable(a["evidence"]), "summary": a["summary"], "data_as_of": a["data_as_of"],
        "decision_ids": a["decision_ids"]})
    prev = memory.last
    memory.last, memory.last_id = snap, rid
    a["version"], a["id"] = memory.version, rid
    ctx.record_event("situation_assessed", {"version": memory.version, "assessment_id": rid, "status": a["status"],
                                            "type": a["type"], "confidence": a["confidence"]}, station="MCI")
    result["events"].append("situation_assessed")
    if a["type"] and (prev is None or prev["type"] != a["type"]):
        ctx.record_event("cause_suspected", {"version": memory.version, "type": a["type"], "likely_cause": a["likely_cause"],
                                             "confidence": a["confidence"], "evidence": _jsonable(a["evidence"][:10])}, station="MCI")
        result["events"].append("cause_suspected")
    if a["end_likely"] and (prev is None or prev["ends"] != snap["ends"]):
        ctx.record_event("expected_end_updated", {"version": memory.version, "earliest": a["end_earliest"].isoformat(),
                                                  "likely": a["end_likely"].isoformat(), "latest": a["end_latest"].isoformat(),
                                                  "confidence": a["end_confidence"]}, station="MCI")
        result["events"].append("expected_end_updated")
    result["wrote"] = True
    memory.history.append({"kind": "version", "at": a["assessed_at"], "version": memory.version, "status": a["status"],
                           "type": a["type"], "confidence": a["confidence"], "ends": snap["ends"], "end_conf": a["end_confidence"],
                           "impact": a["impact"], "summary": a["summary"]})


def _output(a, memory, title, why, decision, options=None):
    def t(v):
        return v.isoformat() if v else "not known"
    return {"title": title, "why": why, "data_as_of": a["data_as_of"].isoformat(), "options": options or [],
            "sources": a["sources"], "decision_ids": [decision], "likely_cause": a["type"] or "unknown",
            "confidence": float(a["confidence"] or 0), "controllable": a["incident_controllable"],
            "expected_end_earliest": t(a["end_earliest"]), "expected_end_latest": t(a["end_latest"]),
            "summary": a["summary"], "assessment_version": memory.version}


def _propose(ctx, memory, a, st, result, s):
    """Proposals for a human (pending; the orchestrator's rule check reviews them later). Each one only once."""
    def once(key, action, decision, title, why, options=None, rung=None):
        if key in memory.proposed:
            return
        memory.proposed.add(key)
        rec = ctx.propose(action, _output(a, memory, title, why, decision, options), safety_rung=rung)
        result["proposals"].append({"decision": decision, "id": rec, "title": title})
        memory.history.append({"kind": "proposal", "at": a["assessed_at"], "decision": decision, "title": title, "action": action})

    if a["status"].startswith("cannot_assess"):
        for x in a["stale_sources"]:
            if x["minutes"] > s["data"]["propose_s01_after_down_min"]:
                once(("S01", x["source"], x["since"]), "switch_to_manual_fallback", "S01",
                     f"{x['source']} has been down since {x['since']}: switch to the manual fallback?",
                     f"No heartbeat from {x['source']} for {x['minutes']} min; the assessment depends on it.")
        for c in a["conflicts"]:
            once(("A09", c["key"]), "resolve_source_conflict", "A09", "Two sources disagree: which one is right?", c["what"],
                 options=c["values"])
        return
    if a["status"] != "abnormal":
        return
    once(("A02", a["type"]), "classify_cause", "A02", f"Confirm the type of event: {a['type']}",
         f"{a['likely_cause']}; confidence {a['confidence']:.2f}. Evidence: " +
         "; ".join(e["note"] for e in a["evidence"][:4]))
    fc = a["flight_controllability"]
    if fc:
        key = ("A03", a["incident_controllable"], tuple(sorted((f["flight_id"], f["proposed"]) for f in fc)))
        review = [f["flight_id"] for f in fc if f["proposed"] == "needs_human_review"]
        once(key, "classify_cause", "A03",
             f"Within Southwest's control? Event: {'no' if a['incident_controllable'] is False else 'yes' if a['incident_controllable'] else 'unknown'}; "
             f"{len(fc)} flight(s), {len(review)} need review",
             "Proposed per flight with evidence; a customer service supervisor decides (this decides what passengers are owed).",
             options=[{k: f[k] for k in ("flight_id", "kind", "reason", "proposed", "why")} for f in fc])
    p = st.program
    if p and (p.extensions or p.shortened):
        once(("A06", p.started.isoformat(), p.extensions, p.shortened), "replan_after_program_change", "A06",
             f"The FAA {'extended' if p.extensions else 'shortened'} the stop to {hhmm(p.announced_end)}: re-plan now?",
             f"New announced end {hhmm(p.announced_end)} (range {hhmm(a['end_earliest'])}-{hhmm(a['end_latest'])}, "
             f"{a['end_confidence_word']} confidence).")
    for c in a["conflicts"]:
        once(("A09", c["key"]), "resolve_source_conflict", "A09", "Two sources disagree: which one is right?", c["what"],
             options=c["values"])
    if p and p.ended is None and (memory.last_p02 is None or a["assessed_at"] - memory.last_p02 >=
                                  timedelta(minutes=s["proposals"]["faa_status_check_every_min"])):
        memory.last_p02 = a["assessed_at"]
        once(("P02", a["assessed_at"].isoformat()), "contact_partner", "P02", "Check the stop's status with the FAA",
             f"Stop active since {hhmm(p.started)}, announced end {hhmm(p.announced_end)}; a fresh FAA status keeps the end time honest.")
    reason = (p.reason if p else "") or ""
    if "power" in reason.lower():
        once(("T03", p.started.isoformat()), "switch_to_manual_fallback", "T03", "Power outage at the airport: switch to manual procedures?",
             f"FAA notice reason: '{reason}'.")
    if "runway" in reason.lower():
        once(("T04", p.started.isoformat()), "replan_after_program_change", "T04", "Runway closure: re-plan arrivals and departures?",
             f"FAA notice reason: '{reason}'.")
    if a["type"] == "security" and ("T01",) not in memory.proposed:
        memory.proposed.add(("T01",))
        ctx.ask_human("T01", {"faa_reason": reason, "since": p.started.isoformat() if p else None,
                              "evidence": _jsonable(a["evidence"][:5])}, "a security report: only a human decides what to do")
        result["proposals"].append({"decision": "T01", "id": None, "title": "security report handed to a human"})
        memory.history.append({"kind": "proposal", "at": a["assessed_at"], "decision": "T01", "title": "security report: ask_human",
                               "action": "ask_human"})
