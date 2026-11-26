"""The Situation agent: is something abnormal happening, what is it and why, how long will it last, who and what is affected.

It reads only through AgentContext (which cuts events at its current time, hides the simulator's answer key and anything a real
source wouldn't publish) and writes only through it. Rules decide the cause, the times and the numbers; no AI.

Status: normal -> abnormal (something is happening) -> recovery (operations are back, but cancelled-flight passengers, stranded
passengers or aircraft out of position remain) -> normal. "Operations back" and "aftermath cleared" are one definition shared
with the test judge (backend/rules/recovery.py). Deciding that the incident is over (A10) stays with the orchestrator.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

import yaml

from backend.agents.situation.state_at import DAY0, State, blocks, build_state, hhmm
from backend.rules import recovery as R
from backend.rules.loader import load_rules
from backend.settings import ROOT, TZ

SETTINGS_FILE = ROOT / "config" / "agent_settings" / "situation.yaml"
WORDS = {"weather": "thunderstorms / lightning at MCI", "faa_program": "an FAA traffic program at MCI",
         "it_outage": "a Southwest systems outage", "airport": "an airport problem (runway or power)",
         "security": "a security event", "crew": "crews running out of legal duty", "unknown": "an unknown cause"}


@lru_cache
def load_settings() -> dict:
    return yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8"))


@lru_cache
def quiet_min() -> float:
    """The shared "operations back" quiet time (config/rules/operations.yaml: operations_back_quiet_min)."""
    return load_rules()["recovery"]["operations_back_quiet_min"]


@dataclass
class Memory:
    """The agent's working memory during one replay (temporary: dropped when the replay or incident ends)."""
    version: int = 0
    last: dict | None = None              # the last version written (comparable form)
    last_id: int | None = None
    schedule: list | None = None
    incident_no: int = 0
    open: dict = field(default_factory=dict)        # decision -> {"id", "key"}: the one open proposal per decision this incident
    last_p02: datetime | None = None
    asked: set = field(default_factory=set)         # ask_human keys already sent
    normal_since: datetime | None = None            # when it last wrote "normal" (a flap back within minutes is the same incident)
    conflicts: dict = field(default_factory=dict)   # conflict key -> its life: open / unresolved / settled / resolved_by_human
    history: list = field(default_factory=list)     # what it wrote and proposed (for the replay printout)


# ---------------------------------------------------------------- the pieces of an assessment

def _detect(st: State, health: dict, s: dict, since=None) -> list[str]:
    """A01: the signals that make the day abnormal right now (empty list = not abnormal). The build-up signals (delays and
    cancellations in a rolling window) only count events after `since`: when operations were last back after an earlier incident."""
    d = s["detect"]
    why = []
    p = st.program
    if p and p.ended is None:
        why.append(f"FAA {p.kind.replace('_', ' ')} active")
    if not st.ramp_open:
        why.append("ramp closed")
    for n in st.active_notices():
        why.append(f"{n.source.replace('_', ' ')} notice: {n.kind.replace('_', ' ')}")
    for system, h in health.items():
        if h["status"] != "up":
            why.append(f"{system} {h['status']}")
    holding = [f for f in st.flights.values() if f.direction == "arrival" and f.status == "holding"]
    # a fresh diversion is a signal, unless the FAA program behind it has already ended (then it is aftermath: recovery)
    over = p is not None and p.ended is not None
    fresh = [] if over else [f for f in st.flights.values() if f.direction == "arrival" and f.status in ("diverted", "resumed")
                             and f.diverted_at and st.t - f.diverted_at <= timedelta(minutes=d["fresh_diversion_min"])]
    if len(holding) + len(fresh) >= d["holding_or_diverted_inbound"]:
        why.append(f"{len(holding)} inbound flights holding, {len(fresh)} just diverted")
    return why + _build_up(st, st.t, since, s)


def _build_up(st: State, t, since, s: dict) -> list[str]:
    """Patterns with no announcement, as of t, counting only events after `since`: a burst of cancellations, crews timing out
    one after another, or more flights newly delayed than is normal for that hour."""
    d = s["detect"]

    def recent(times, minutes):
        return [x for x in times if x and (since is None or x > since) and t - timedelta(minutes=minutes) < x <= t]
    out = []
    cx = recent([f.cancelled_at for f in st.flights.values()], d["cancellations_window_min"])
    if len(cx) >= d["cancellations_threshold"]:
        out.append(f"{len(cx)} flights cancelled in the last {d['cancellations_window_min']} min")
    crew = recent([f.cancelled_at for f in st.flights.values() if f.cancel_reason == "crew out of legal duty"],
                  d["crew_cancellations_window_min"])
    if len(crew) >= d["crew_cancellations_threshold"]:
        out.append(f"{len(crew)} cancellations for crew out of legal duty in the last {d['crew_cancellations_window_min']} min")
    delays = recent([f.first_delayed_at for f in st.flights.values()], d["new_delays_window_min"])
    normal = d["normal_new_delays_by_hour"].get(t.astimezone(TZ).hour, 0)
    if len(delays) >= max(normal + d["delays_above_normal"], d["min_new_delays"]):
        out.append(f"{len(delays)} flights newly delayed in the last {d['new_delays_window_min']} min (normal for this hour: "
                   f"up to {normal})")
    return out


def _spells(st: State, s: dict) -> tuple[list[dict], object]:
    """The day's busy spells (backend/rules/recovery.py), each marked as an incident or not, and when operations were last back
    after an incident. A spell is an incident if it had an FAA program, a closed ramp, an open notice or a system down, two
    flights holding at once, or a build-up signal (counting only events since operations were last back). A spell with only
    delays and cancellations that starts soon after an incident's operations came back is that incident's aftermath (crews who
    worked through a long stop time out in the hours after it), not a new incident."""
    spells = R.busy_spells(st.t, blocks(st), st.activity, quiet_min())
    last_back = incident_back = None                  # events up to last_back are used up; incident_back: the last incident's
    after = timedelta(minutes=s["recovery"]["aftermath_attribution_min"])
    for sp in spells:
        hard = [b for b in sp["blocks"] if not b[2].endswith(" holding")]
        holds = [b for b in sp["blocks"] if b[2].endswith(" holding")]
        two_holding = any(sum(1 for c, d_, _ in holds if c <= a < (d_ or st.t + timedelta(days=1))) >= 2 for a, _, _ in holds)
        sp["aftermath_of_last"] = bool(not hard and not two_holding and incident_back and sp["start"] - incident_back < after)
        sp["incident"] = bool(hard or two_holding or (not sp["aftermath_of_last"] and
                                                      any(_build_up(st, x, last_back, s) for x, _ in sp["activity"])))
        if sp["back_at"] is not None and (sp["incident"] or sp["aftermath_of_last"]):
            last_back = sp["back_at"]
            if sp["incident"]:
                incident_back = sp["back_at"]
    return spells, last_back


def _aftermath(st: State, s: dict) -> list[str]:
    """What still needs looking after once operations are back (backend/rules/recovery.py). The agent can't see rebookings:
    a cancelled flight's passengers count as "still to rebook" until the reservation system has had time to re-accommodate them
    (those it couldn't place today are reported as stranded overnight)."""
    settle = timedelta(minutes=s["recovery"]["rebooking_settle_min"])
    rebooking = any(f.cancelled_at and st.t - f.cancelled_at < settle for f in st.flights.values())
    away = sorted(f.id for f in st.flights.values() if f.status in ("diverted", "resumed"))
    return R.aftermath(None if rebooking else 0, away, st.stranded_overnight)


def _classify(st: State, health: dict, s: dict) -> dict:
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
            vote("unknown", "faa_feed", p.notices[:1], "FAA program with no stated reason")
    for start, end, eids, reason in st.ramp_closures:
        if reason and "lightning" in reason:
            vote("weather", "weather_feed", [(eids[0], start, "ramp_closed")], "lightning detected near the ramp")
    for n in st.notices:
        kind = c["notice_kinds"].get(n.kind)
        if kind:
            vote(kind, n.source, [n.event], f"{n.source.replace('_', ' ')} notice: {n.kind.replace('_', ' ')}"
                                            + (f" ({n.area})" if n.area else ""))
    for system, h in health.items():
        if h["status"] != "up":
            vote("it_outage", "system_monitor", [h["event"]] if h.get("event") else [], f"{system} reported {h['status']}")
    codes = st.delay_reasons
    if codes:
        top = max(sorted(codes), key=codes.get)
        delayed = [f for f in st.flights.values() if f.delay_reason == top]
        mapped = top if top in c["controllable_by_type"] else None
        if mapped:
            vote(mapped, "flight_ops", [delayed[0].events[0]] if delayed and delayed[0].events else [],
                 f"{codes[top]} delay reports coded '{top}'")
    crew = [f for f in st.flights.values() if f.cancel_reason == "crew out of legal duty"]
    external = {k for k in votes if k not in ("crew", "unknown")}
    if len(crew) >= c["crew_cancellations_for_crew_cause"] and not external:
        vote("crew", "flight_ops", [crew[0].events[-1]], f"{len(crew)} cancellations for crew out of legal duty")

    if len(crew) >= c["crew_cancellations_for_crew_cause"] and external:
        # crews timing out alongside an outside cause: reported as a possible second problem, never as the main cause
        votes.setdefault("crew", set()).add("crew_cancellations")
        evidence.append({"event_id": crew[0].events[-1][0], "at": crew[0].events[-1][1].isoformat(), "type": "flight_cancelled",
                         "source": "flight_ops", "points_to": "crew", "note": f"{len(crew)} cancellations for crew out of legal duty"})
    known = {k: v for k, v in votes.items() if k != "unknown"}
    if not known:
        return {"type": "unknown", "likely_cause": "suspected: abnormal signals with no clear cause yet", "confidence": c["unknown_confidence"],
                "evidence": evidence, "others": []}
    best = max(sorted(k for k in known if not (k == "crew" and external)) or sorted(known), key=lambda k: len(known[k]))
    agree = len(known[best])
    ladder = c["confidence_by_agreeing_sources"]
    conf = ladder[min(agree, len(ladder)) - 1]
    if best == "crew" and known[best] == {"flight_ops"}:
        # crews timing out with nothing else behind it: "crew suspected", low at first, higher as more crews time out
        cl = c["crew_confidence_by_cancellations"]
        conf = cl[min(len(crew) - c["crew_cancellations_for_crew_cause"], len(cl) - 1)]
    others = sorted(k for k in known if k != best)
    if others:
        conf = min(conf, c["confidence_if_signals_disagree"])
    # a second incident: another cause that a notice-type source reports on its own (not just a delay code)
    second = sorted(k for k in others if known[k] & set(c["second_cause_sources"]) or k == "crew")
    text = f"suspected: {WORDS.get(best, best)} (signals from {', '.join(sorted(known[best]))})"
    if second:
        text += "; also suspected: " + "; ".join(f"{WORDS.get(k, k)} (signals from {', '.join(sorted(known[k]))})" for k in second)
    return {"type": best, "likely_cause": text, "confidence": round(conf, 2), "evidence": evidence, "others": second}


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


def program_cause(p, kind: str, s: dict) -> str:
    """Why the FAA issued THIS program (from its stated reason), which is what its end depends on; the day's main suspected
    cause can be something else (a storm during an IT outage). Falls back to the main cause when the notice gives no reason."""
    reason = (p.reason or "").lower()
    hit = [k for k, words in s["classify"]["faa_reason_words"].items() if any(w in reason for w in words)]
    return hit[0] if hit else kind


def end_features(st: State, kind: str, s: dict) -> dict:
    """What makes the end of the running FAA program uncertain, from what the agent can see now."""
    x = s["expected_end"]
    p = st.program
    kind = program_cause(p, kind, s)
    n = p.extensions + p.shortened
    recent = timedelta(minutes=x["recent_min"])
    lightning = any(b is None and "lightning" in (r or "") for a, b, _, r in st.ramp_closures)
    just_extended = any(st.t - when <= recent and b > a for (_, a), (when, b) in zip(p.ends, p.ends[1:]))
    evening = st.t.astimezone(TZ).hour >= int(x["evening_from"].split(":")[0])
    return {"cause": kind, "changes": "0" if n == 0 else "1" if n == 1 else "2+", "evening": evening,
            "time": "evening" if evening else "day",
            "messy": any(st.t - recent <= when <= st.t for when, _, _ in st.messy),
            "messy_faa": any(st.t - recent <= when <= st.t and feed == "faa_feed" for when, feed, _ in st.messy)
                         or any(st.t - when <= recent for when, _, _ in p.conflicts),
            "growing": lightning or just_extended}


def group_keys(f: dict) -> list[str]:
    """The groups a moment belongs to, most specific first (for the calibrated confidence table)."""
    m = "messy" if f["messy"] else "clean"
    return [f"{f['cause']}|{f['changes']}|{f['time']}|{m}", f"{f['cause']}|{f['changes']}|{f['time']}", f"{f['cause']}|{f['changes']}",
            f"{f['cause']}"]


def end_confidence(f: dict, s: dict) -> float:
    """How often, on the training days, the real end fell inside the range in this moment's group (rounded down). It is never
    "high" in a group where that was not proven on enough training moments."""
    x = s["expected_end"]
    table = x["confidence_by_group"] or {}
    for key in group_keys(f):
        if key in table and table[key][1] >= x["min_group_ticks"]:
            return min(table[key][0], x["max_confidence"])      # never certain about the future, whatever the training days say
    return min(x["unproven_confidence"], x["confidence_words"]["high"] - 0.05)


def _expected_end(st: State, kind: str, s: dict, ops_back: bool = True) -> dict:
    """A05: earliest / likely / latest, with a confidence. Never a single time while it is still running.
    latest = announced end + a margin for the cause and the number of changes so far, plus extra margins for an evening storm,
    a storm still growing (lightning on the ramp now, or extended in the last half hour) and recent messy or conflicting FAA
    data. earliest = announced end - an early margin for the cause. The margins and the confidence table are calibrated on
    training days 1-100 only (see the settings file), then frozen."""
    x = s["expected_end"]
    none = {"earliest": None, "likely": None, "latest": None, "confidence": None, "what": None, "extensions": 0, "ended": None}
    p = st.program
    if p is not None and p.ended is None:
        f = end_features(st, kind, s)
        fam = f["cause"] if f["cause"] in x["late_margin_min"] else "unknown"
        late = x["late_margin_min"][fam][{"0": 0, "1": 1, "2+": 2}[f["changes"]]]
        late += sum(x["extra_late_min"][k] for k in ("evening", "growing", "messy_faa") if f[k])
        late = min(max(late, x["min_late_margin_min"]), x["max_margin_min"])
        early = max(x["early_margin_min"].get(fam, x["early_margin_min"]["unknown"]), x["min_early_margin_min"])
        likely = max(p.announced_end, st.t)
        conf = end_confidence(f, s)
        if p.announced_end < st.t:                   # past its announced end and no word from the FAA: say so, don't guess
            conf = min(conf, x["overdue_confidence"])
        return {"earliest": max(st.t, p.announced_end - timedelta(minutes=early)), "likely": likely,
                "latest": likely + timedelta(minutes=round(late)), "confidence": conf, "what": p.kind,
                "extensions": p.extensions, "ended": None, "features": f}
    timed = [n for n in st.active_notices() if n.expected_end]
    if timed:                                        # an airport notice with an expected end (power, runway)
        likely = max(n.expected_end for n in timed)
        return {"earliest": max(st.t, likely - timedelta(minutes=x["min_early_margin_min"])), "likely": likely,
                "latest": likely + timedelta(minutes=x["notice_margin_min"]), "confidence": x["notice_confidence"], "what": "notice",
                "extensions": 0, "ended": None}
    if not st.ramp_open:
        likely = st.ramp_expected_open or st.t
        return {"earliest": max(st.t, likely), "likely": likely, "latest": likely + timedelta(minutes=x["notice_margin_min"]),
                "confidence": x["notice_confidence"], "what": "ramp", "extensions": 0, "ended": None}
    if p is not None and p.ended is not None and not ops_back:
        # the FAA says it ended, but operations are not back (flights still holding, the ramp still closed): the notice may be
        # premature, so the range stays open up to the end last announced, at medium confidence at most
        latest = max(p.announced_end, p.ended) + timedelta(minutes=x["min_late_margin_min"])
        return {"earliest": p.ended, "likely": p.ended, "latest": max(latest, st.t), "confidence": x["ended_not_back_confidence"],
                "what": "ended, operations not back", "extensions": p.extensions, "ended": p.ended}
    if p is not None and p.ended is not None:
        return {"earliest": p.ended, "likely": p.ended, "latest": p.ended, "confidence": 1.0, "what": "ended",
                "extensions": p.extensions, "ended": p.ended}
    return none


def _impact(st: State, end: dict, others: list, s: dict) -> dict:
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

    def ever_diverted(f):
        return any(e[2] == "flight_diverted" for e in f.events)

    def counts(fl):
        out = {k: 0 for k in ("delayed", "holding", "diverted", "cancelled", "scheduled", "departed", "arrived", "resumed")}
        for f in fl:
            out[f.status] = out.get(f.status, 0) + 1
        out["ever_delayed_30_plus"] = sum(1 for f in fl if f.delay_min >= 30)
        out["ever_diverted"] = sum(1 for f in fl if ever_diverted(f))
        return out

    affected = [f for f in deps + arrs if f.status in ("delayed", "holding", "diverted", "resumed", "cancelled") or f.delay_min >= 30
                or ever_diverted(f)]
    closed_min = sum(int(((stop or st.t) - start).total_seconds() // 60) for start, stop, _, _ in st.ramp_closures)

    def score(f):
        if f.status == "cancelled":
            return 10000
        if ever_diverted(f) and f.status != "arrived":
            return 9000
        if f.status == "holding":
            return 8000
        return f.delay_min

    def why(f):
        if f.status == "cancelled":
            return f"cancelled: {f.cancel_reason}"
        if ever_diverted(f) and f.status != "arrived":
            return f"diverted to {f.diverted_to}" + (" (on its way back)" if f.status == "resumed" else "")
        if f.status == "holding":
            return "holding over MCI"
        return f"delayed {f.delay_min} min"

    worst = sorted(affected, key=lambda f: (-score(f), f.id))[: i["worst_affected"]]
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
        "active_notices": [{"source": n.source, "kind": n.kind, "area": n.area, "since": n.since.isoformat()} for n in st.active_notices()],
        "other_suspected_causes": others,
        "stranded_overnight": st.stranded_overnight, "stranded_overnight_local": st.stranded_local,
        "next_day_flights": st.next_day_flights,
        "inputs_set_aside": {"unreadable": len(st.unreadable), "duplicates": st.duplicates},
        "worst_affected": [{"flight_id": f.id, "why": why(f)} for f in worst],
    }


def _conflicts(st: State, health: dict, stale: list[dict], s: dict) -> list[dict]:
    """A09: facts that disagree. Both sides are recorded; the agent never picks one."""
    d = s["data"]
    out = []
    for prog in st.past_programs + ([st.program] if st.program else []):
        for when, ends, eids in prog.conflicts:
            out.append({"key": f"faa-notices:{when.isoformat()}", "flight_ids": [], "fields": ["expected_end"], "reported_at": when,
                        "sides": [f"FAA notice: ends {hhmm(e)}" for e in ends],
                        "what": f"two FAA notices issued at {hhmm(when)} give different ends ({' and '.join(hhmm(e) for e in ends)})",
                        "values": [{"source": "faa_feed", "says": f"ends {hhmm(e)}", "event_id": i} for e, i in zip(ends, eids)]})
    for fid, eid, when in st.departed_during_stop:
        out.append({"key": f"departed-during-stop:{fid}", "flight_ids": [fid], "fields": ["status"], "reported_at": when,
                    "report_event": eid, "sides": [f"departed at {hhmm(when)} (flight ops)",
                                                   "still at the gate (ground stop on, ramp closed)"], "what": f"{fid} reported departed while the stop is on and the ramp closed",
                    "values": [{"source": "flight_ops", "says": f"{fid} departed at {hhmm(when)}", "event_id": eid},
                               {"source": "faa_feed / weather_feed", "says": "ground stop active and ramp closed"}]})
    p = st.program
    if p and p.ended and st.t - p.ended >= timedelta(minutes=d["holding_after_stop_end_min"]):
        holding = sorted(f.id for f in st.flights.values() if f.status == "holding" and f.hold_since and f.hold_since <= p.ended)
        if holding:
            out.append({"key": f"holding-after-end:{p.ended.isoformat()}", "flight_ids": holding, "fields": ["status"],
                        "reported_at": p.ended, "sides": [f"the stop ended at {hhmm(p.ended)} (FAA)", "flights still holding (flight ops)"],
                        "what": f"the FAA says the stop ended at {hhmm(p.ended)} but {len(holding)} flight(s) holding since before then "
                                f"are still holding {int((st.t - p.ended).total_seconds() // 60)} min later",
                        "values": [{"source": "faa_feed", "says": f"stop ended {hhmm(p.ended)}"},
                                   {"source": "flight_ops", "says": f"still holding: {', '.join(holding)}"}]})
    for st_ in stale:
        system = d["system_for_feed"].get(st_["source"])
        if system and health.get(system, {}).get("status") == "up":
            out.append({"key": f"health-up-feed-stopped:{st_['source']}:{st_['since']}", "flight_ids": [],
                        "fields": ["system_status"], "reported_at": st.t, "feed": st_["source"], "system": system,
                        "sides": [f"{system} is up (system health)", f"{st_['source']} has gone silent"],
                        "what": f"system health says {system} is up, but its feed has sent nothing since {st_['since']}",
                        "values": [{"source": "system_health", "says": f"{system}: up"},
                                   {"source": st_["source"], "says": f"last heartbeat {st_['since']}"}]})
    return out


OPEN = ("open", "unresolved")


def _settle(st: State, e: dict, present: bool, health: dict, s: dict):
    """The later evidence that settles a conflict, if there is any yet: (side, how, evidence). A rule, never a guess."""
    kind = e["key"].split(":")[0]
    since = datetime.fromisoformat(e["opened_at"])
    if kind == "departed-during-stop":
        rule = s["conflicts"]["settle"]["departed-during-stop"]
        f = st.flights.get(e["flight_ids"][0])
        for eid, when, typ in (f.events if f else []):
            if when < since or eid == e.get("report_event"):
                continue
            if typ in rule["departure_real"]:
                return (e["sides"][0], f"{f.id} was later seen {typ.replace('flight_', '')} at {hhmm(when)}: the departure was real",
                        [{"event_id": eid, "at": when.isoformat(), "type": typ}])
            if typ in rule["departure_false"]:
                what = {"flight_departed": "departed again", "flight_cancelled": "cancelled",
                        "tarmac_started": "seen leaving the gate"}.get(typ, "given a new delay at the gate")
                return (e["sides"][1], f"{f.id} was {what} at {hhmm(when)}: the earlier departure report was false",
                        [{"event_id": eid, "at": when.isoformat(), "type": typ}])
        return None
    if kind == "faa-notices":
        for prog in st.past_programs + ([st.program] if st.program else []):
            if prog.last_notice_at and prog.last_notice_at > since:
                side = f"FAA notice: ends {hhmm(prog.announced_end)}" if prog.announced_end else "a later FAA notice"
                return side, f"a later FAA notice at {hhmm(prog.last_notice_at)} replaced both", []
        return None
    if kind == "holding-after-end" and not present:
        return e["sides"][1], "the flights that were still holding have landed, diverted or resumed (both were true)", []
    if kind == "health-up-feed-stopped":
        if health.get(e.get("system"), {}).get("status") not in (None, "up"):
            return e["sides"][1], f"system health now reports {e['system']} down: the silence was real", []
        if not present:
            return e["sides"][0], f"{e.get('feed')} is sending again: system health was right", []
    return None


def _lifecycle(st: State, found: list, memory, health: dict, s: dict) -> list:
    """A09 conflicts over time. Opened once; closed when later evidence settles it (with which side was right and the
    evidence) or when a named human answers; marked "unresolved: needs a human" if nothing settles it in time."""
    t, log, present = st.t, memory.conflicts, {c["key"] for c in found}
    for c in found:
        e = log.get(c["key"])
        if e is None:
            log[c["key"]] = {**{k: v for k, v in c.items() if k != "reported_at"}, "status": "open",
                             "opened_at": (c.get("reported_at") or t).isoformat()}
        elif e["status"] in OPEN:
            e.update({k: v for k, v in c.items() if k in ("what", "values", "flight_ids")})
    limit = timedelta(minutes=s["conflicts"]["unresolved_after_min"])
    for key, e in log.items():
        if e["status"] not in OPEN:
            continue
        human = st.human_answers.get(key)
        if human:
            e.update(status="resolved_by_human", side=human["answer"], closed_at=human["at"].isoformat(),
                     settled_by=f"resolved by a human ({human['role']}) at {hhmm(human['at'])}")
            continue
        done = _settle(st, e, key in present, health, s)
        if done:
            e.update(status="settled", side=done[0], closed_at=t.isoformat(), settled_by=done[1], settle_evidence=done[2])
        elif t - datetime.fromisoformat(e["opened_at"]) >= limit:
            e.update(status="unresolved", needs="a human: no later evidence has settled it")
    return [dict(e) for e in log.values()]


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
    if a["status"] == "recovery":
        s.append(f"Operations at MCI are back, but the aftermath remains: {'; '.join(a['aftermath'])}.")
    if p and p.ended is None:
        n = p.extensions + p.shortened
        ext = {0: "", 1: " The end time was changed once."}.get(n, f" The end time was changed {n} times.")
        s.append(f"{p.kind.replace('_', ' ').capitalize()} at MCI since {hhmm(p.started)} for {p.reason or 'an unstated reason'} (FAA). "
                 f"Expected to end {hhmm(a['end_likely'])} (range {hhmm(a['end_earliest'])}-{hhmm(a['end_latest'])}, "
                 f"{a['end_confidence_word']} confidence).{ext}")
    elif p and p.ended:
        s.append(f"The {p.kind.replace('_', ' ')} at MCI ({hhmm(p.started)}-{hhmm(p.ended)}, {p.reason or 'no stated reason'}) has ended.")
    for n in st.active_notices():
        s.append(f"{n.source.replace('_', ' ').capitalize()} reports {n.kind.replace('_', ' ')}"
                 f"{' in ' + n.area if n.area else ''} since {hhmm(n.since)}.")
    s.append(f"Suspected cause: {a['likely_cause'].replace('suspected: ', '')} (confidence {a['confidence']:.2f}).")
    d, r = imp["departures"], imp["arrivals"]
    s.append(f"{d['ever_diverted'] + r['ever_diverted']} inbound flights diverted, {d['cancelled'] + r['cancelled']} cancelled, "
             f"{d['ever_delayed_30_plus'] + r['ever_delayed_30_plus']} delayed 30+ min, about {imp['passengers_on_affected_flights']:,} "
             f"passengers on affected flights.")
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


def _health(rows: list[dict], st: State) -> dict:
    """System status as of T: the system_health table, overridden by the status changes the monitor published up to T."""
    out = {r["system"]: {"status": r["status"], "event": None} for r in rows}
    for system, h in st.systems.items():
        out[system] = {"status": h["status"], "event": h["event"], "since": h["since"]}
    return out


# ---------------------------------------------------------------- one assessment

def assess(ctx, memory: Memory | None = None, write: bool = True, settings: dict | None = None) -> dict:
    """Assess the situation as of ctx.now. Writes a new version only if something meaningful changed (and write=True)."""
    s = settings or load_settings()
    memory = memory or Memory()
    t = ctx.now
    events = ctx.read_events()                       # everything delivered by now (late events included, in their true order)
    if memory.schedule is None:
        memory.schedule = ctx.read("flights")
    st = build_state(memory.schedule, events, t)
    health = _health(ctx.read("system_health"), st)

    max_age = ctx.contract.max_data_age_sec
    stale = []
    for feed in s["data"]["depends_on"]:
        age = ctx.feed_age_sec(feed)
        if age > max_age:
            stale.append({"source": feed, "since": hhmm(ctx.feed_last(feed)), "minutes": int(age // 60)})
    sources = [{"fact": f"{feed} heartbeat", "source": feed, "as_of": ctx.feed_last(feed).isoformat()} for feed in s["data"]["depends_on"]]
    sources.append({"fact": "published schedule", "source": "flights schedule (published before the day)", "as_of": DAY0.isoformat()})
    data_as_of = min(ctx.feed_last(f) for f in s["data"]["depends_on"])
    down = [{"system": k, "since": hhmm(v.get("since")), "minutes": int((t - v["since"]).total_seconds() // 60) if v.get("since") else 0}
            for k, v in health.items() if v["status"] != "up"]

    a = {"assessed_at": t, "stale_sources": stale, "conflicts": [], "flight_controllability": [], "evidence": [], "impact": {},
         "type": None, "likely_cause": None, "confidence": None, "incident_controllable": None,
         "end_earliest": None, "end_likely": None, "end_latest": None, "end_confidence": None, "end_confidence_word": "no",
         "data_as_of": data_as_of, "sources": sources, "abnormal_because": [], "aftermath": [], "systems_down": down}
    if stale:
        first = stale[0]
        a["status"] = f"cannot_assess: {first['source']} stale since {first['since']}"
        a["decision_ids"] = ["A08"]
        a["conflicts"] = _lifecycle(st, _conflicts(st, health, stale, s), memory, health, s)
        if any(c["status"] in OPEN for c in a["conflicts"]):
            a["decision_ids"].append("A09")
    else:
        spells, since = _spells(st, s)
        current = spells[-1] if spells and spells[-1]["back_at"] is None else None
        # delays and cancellations right after an incident are its aftermath: they are not a new build-up
        because = _detect(st, health, s, t if current and current["aftermath_of_last"] else since)
        if not because and current and current["incident"]:
            # the incident's signals have stopped but operations are not back yet (backend/rules/recovery.py)
            because = ["operations not back yet: " + "; ".join(R.why_not_back(t, blocks(st), st.activity, quiet_min()))]
        a["abnormal_because"] = because
        incident_today = st.incident_seen or any(sp["incident"] for sp in spells)
        aftermath = _aftermath(st, s) if incident_today and not because else []
        if because or aftermath:
            a["status"] = "abnormal" if because else "recovery"
            a["aftermath"] = aftermath
            cls = _classify(st, health, s)
            a.update(type=cls["type"], likely_cause=cls["likely_cause"], confidence=cls["confidence"], evidence=cls["evidence"])
            a["incident_controllable"], a["flight_controllability"] = _controllable(st, cls["type"], s)
            end = _expected_end(st, cls["type"], s, ops_back=a["status"] != "abnormal")
            a.update(end_earliest=end["earliest"], end_likely=end["likely"], end_latest=end["latest"], end_confidence=end["confidence"],
                     end_confidence_word=_word(end["confidence"], s))
            a["impact"] = _impact(st, end, cls["others"], s)
            a["conflicts"] = _lifecycle(st, _conflicts(st, health, stale, s), memory, health, s)
            a["decision_ids"] = ["A01", "A02", "A03", "A05", "A07"] + \
                                (["A09"] if any(c["status"] in OPEN for c in a["conflicts"]) else []) + \
                                (["Q05"] if st.stranded_overnight else [])
            if st.unreadable:
                a["evidence"].append({"event_id": None, "at": None, "type": "unreadable_inputs", "source": "several",
                                      "points_to": "unknown", "note": f"{len(st.unreadable)} event(s) set aside as unreadable"})
        else:
            a["status"] = "normal"
            a["decision_ids"] = ["A01"]
    for r in st.floor_reports:                           # live mode: every report from the floor is evidence (none otherwise)
        a["evidence"].append({"event_id": r["event_id"], "at": r["at"], "type": "floor_report", "source": "floor",
                              "points_to": r["kind"], "note": f"floor report ({r['kind'].replace('_', ' ')}"
                              + (f", {r['flight_id']}" if r.get("flight_id") else "") + (f", gate {r['gate']}" if r.get("gate") else "")
                              + f"): {r.get('text') or ''}".rstrip(": ")})
    a["summary"] = _summary(a, st)
    a["_state"] = st

    snap = _snapshot(a)
    changed = _changed(memory.last, snap, s)
    a["changed"] = changed
    result = {"assessment": a, "wrote": False, "proposals": [], "events": []}
    if not write:
        return result
    if changed:
        back_for = t - memory.normal_since if memory.normal_since else None
        if a["status"] == "abnormal" and (memory.last is None or (memory.last["status"] == "normal" and (
                back_for is None or back_for >= timedelta(minutes=quiet_min())))):
            memory.incident_no += 1               # a new incident: its proposals start afresh
            memory.open = {}
        memory.normal_since = (memory.normal_since or t) if a["status"] == "normal" else None
        _write(ctx, memory, a, snap, result)
    if memory.last_id is not None:
        _propose(ctx, memory, a, st, result, s, health)
    # shared-state rule 6: "I ran at T", every run, even when nothing changed (readers measure freshness from this)
    ctx.heartbeat({"latest_version": memory.version or None, "status": memory.last["status"] if memory.last else None})
    return result


def _snapshot(a):
    imp = a["impact"] or {}
    d, r = imp.get("departures", {}), imp.get("arrivals", {})
    return {"status": a["status"], "type": a["type"], "confidence": a["confidence"],
            "ends": [a["end_earliest"], a["end_likely"], a["end_latest"]],
            "cancelled": d.get("cancelled", 0) + r.get("cancelled", 0),
            "diverted": d.get("ever_diverted", 0) + r.get("ever_diverted", 0),
            "others": imp.get("other_suspected_causes", []),
            "conflicts": sorted(f"{c['key']}|{c['status']}" for c in a["conflicts"]),
            "stale": sorted(x["source"] for x in a["stale_sources"])}


def _changed(last, snap, s) -> bool:
    w = s["write_when"]
    if last is None:
        return snap["status"] != "normal"            # normal operations and nothing before: write nothing
    if snap["status"] != last["status"] or snap["type"] != last["type"] or snap["others"] != last["others"]:
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


def _propose(ctx, memory, a, st, result, s, health):
    """Proposals for a human (pending; the orchestrator's rule check reviews them later).
    One open proposal per decision per incident: a new value supersedes the old one instead of adding another."""
    def propose(decision, key, action, title, why, options=None):
        cur = memory.open.get(decision)
        if cur and cur["key"] == key:
            return                                   # the same question is already open
        rec = ctx.propose(action, _output(a, memory, title, why, decision, options), supersedes=cur["id"] if cur else None)
        memory.open[decision] = {"id": rec, "key": key}
        result["proposals"].append({"decision": decision, "id": rec, "title": title, "supersedes": cur["id"] if cur else None})
        memory.history.append({"kind": "proposal", "at": a["assessed_at"], "decision": decision, "title": title, "action": action,
                               "supersedes": bool(cur)})

    t = a["assessed_at"]
    # one S01 for everything that is down (a feed and the system behind it are one question, not two that replace each other)
    after = s["data"]["propose_s01_after_down_min"]
    down = [("feed", x["source"], x["since"], x["minutes"]) for x in a["stale_sources"] if x["minutes"] > after] + \
           [("system", d["system"], d["since"], d["minutes"]) for d in a["systems_down"] if d["minutes"] > after]
    if down:
        propose("S01", tuple(sorted(k[:3] for k in down)), "switch_to_manual_fallback",
                f"{' and '.join(f'{k[1]} down since {k[2]}' for k in down)}: switch to the manual fallback?",
                "; ".join((f"no heartbeat from {k[1]} for {k[3]} min" if k[0] == "feed" else
                           f"the system monitor reports {k[1]} down for {k[3]} min") for k in down)
                + "; the assessment depends on it.")
    still = [c for c in a["conflicts"] if c["status"] in OPEN]
    if still:
        keys = tuple(sorted(f"{c['key']}|{c['status']}" for c in still))
        if not (memory.open.get("A09") and set(keys) <= set(memory.open["A09"]["key"])):
            unresolved = [c for c in still if c["status"] == "unresolved"]
            propose("A09", keys, "resolve_source_conflict",
                    "Sources still disagree and nothing has settled it: a human must decide" if unresolved else
                    "Sources disagree: which one is right?",
                    "; ".join(c["what"] for c in still),
                    options=[{**v, "conflict_key": c["key"], "flight_ids": c["flight_ids"], "sides": c["sides"]}
                             for c in still for v in c["values"]])
    _ask_t01(ctx, memory, a, st, result)
    if a["status"] not in ("abnormal", "recovery"):
        return
    propose("A02", (a["type"], tuple(a["impact"].get("other_suspected_causes", []))), "classify_cause", f"Confirm the type of event: {a['type']}",
            f"{a['likely_cause']}; confidence {a['confidence']:.2f}. Evidence: " + "; ".join(e["note"] for e in a["evidence"][:4]))
    fc = a["flight_controllability"]
    if fc:
        review = [f["flight_id"] for f in fc if f["proposed"] == "needs_human_review"]
        word = {False: "no", True: "yes"}.get(a["incident_controllable"], "unknown")
        propose("A03", (a["incident_controllable"], tuple(sorted((f["flight_id"], f["proposed"]) for f in fc))), "classify_cause",
                f"Within Southwest's control? Event: {word}; {len(fc)} flight(s), {len(review)} need review",
                "Proposed per flight with evidence; a customer service supervisor decides (this decides what passengers are owed).",
                options=[{k: f[k] for k in ("flight_id", "kind", "reason", "proposed", "why")} for f in fc])
    p = st.program
    if p and p.ended is None and (p.extensions or p.shortened):
        propose("A06", (p.started.isoformat(), p.extensions, p.shortened), "replan_after_program_change",
                f"The FAA {'extended' if p.extensions else 'shortened'} the stop to {hhmm(p.announced_end)}: re-plan now?",
                f"New announced end {hhmm(p.announced_end)} (range {hhmm(a['end_earliest'])}-{hhmm(a['end_latest'])}, "
                f"{a['end_confidence_word']} confidence).")
    quiet = timedelta(minutes=s["proposals"]["faa_quiet_min"])
    if p and p.ended is None and t - p.last_notice_at >= quiet and (memory.last_p02 is None or t - memory.last_p02 >= quiet):
        memory.last_p02 = t
        propose("P02", ("check", t.isoformat()), "contact_partner", "Check the stop's status with the FAA",
                f"Stop active since {hhmm(p.started)}, announced end {hhmm(p.announced_end)}, and no FAA update since "
                f"{hhmm(p.last_notice_at)}.")
    reasons = ((p.reason if p else "") or "").lower()
    kinds = {n.kind for n in st.active_notices("airport_ops")}
    if "power" in reasons or "power_outage" in kinds:
        propose("T03", ("power", p.started.isoformat() if p else ""), "switch_to_manual_fallback",
                "Power outage at the airport: switch to manual procedures?", "FAA notice or airport ops notice reports a power outage.")
    if "runway" in reasons or "runway_closed" in kinds:
        propose("T04", ("runway", p.started.isoformat() if p else ""), "replan_after_program_change",
                "Runway closure: re-plan arrivals and departures?", "FAA notice or airport ops notice reports a runway closure.")


def _ask_t01(ctx, memory, a, st, result):
    """A security report goes straight to a human (T01); the agent decides nothing about it."""
    reports = [n for n in st.active_notices("police_tsa") if n.kind == "security_threat"]
    p = st.program
    if not reports and p and p.ended is None and "security" in (p.reason or "").lower():
        reports.append(None)
    for n in reports:
        key = ("T01", n.since.isoformat() if n else p.started.isoformat())
        if key in memory.asked:
            continue
        memory.asked.add(key)
        facts = {"report": {"kind": n.kind, "action": None, "area": n.area, "since": n.since.isoformat()} if n else
                 {"faa_reason": p.reason, "since": p.started.isoformat()}}
        ctx.ask_human("T01", facts, "a security report: only a human decides what to do")
        result["proposals"].append({"decision": "T01", "id": None, "title": "security report handed to a human"})
        memory.history.append({"kind": "proposal", "at": a["assessed_at"], "decision": "T01", "title": "security report: ask_human",
                               "action": "ask_human"})
