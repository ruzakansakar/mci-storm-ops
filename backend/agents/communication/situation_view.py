"""What Communication knows from the Situation agent (Phase 4c): its working context, read only through AgentContext.

It reads Situation's latest shared version (time <= now, only its contract's fields) and Situation's heartbeat. It uses it for:
  a. conflict holds: no fixes on a flight where Situation reports conflicting data (A09), until a later version clears it;
  b. Situation unavailable (no heartbeat for unavailable_after_min): logged, fixes carry on (they only re-show confirmed truth),
     escalated to the customer service supervisor after escalate_after_min. The last real picture is kept, marked as old;
     nothing is ever made up in its place;
  c. Situation cannot assess: recorded here, no change to fixes;
  d. the latest picture, kept for 4b-3 messages. It is not used in any content yet.
Suspected causes and expected ends stay suspicions (with their confidence): they are never treated as facts.
"""
from dataclasses import dataclass, field
from datetime import datetime

from backend.agents.communication.state_at import hhmm
from backend.contracts.shared_state import disagreement, heartbeat_age_min

TABLE = "situation_assessments"


@dataclass
class SituationView:
    picture: dict | None = None             # the latest version read (facts + suspicions), for 4b-3
    version: int | None = None
    available: bool = True
    heartbeat: datetime | None = None
    unavailable_since: datetime | None = None
    escalated: bool = False
    cannot_assess: str | None = None        # Situation's own words, when it says it cannot assess
    conflicts: dict = field(default_factory=dict)   # flight id -> {"since": when first seen, "version": version, "key": ...}
    cleared: list = field(default_factory=list)     # [{flight_id, since, cleared_at, version}]
    disagree_since: datetime | None = None
    disagree_logged: bool = False
    timeline: list = field(default_factory=list)    # what it knew, each time it changed (for the debug page)


def read_situation(ctx, view: SituationView, settings: dict) -> SituationView:
    s, now = settings["situation"], ctx.now
    pic = ctx.read_shared(TABLE)                     # audited with the version read
    view.heartbeat = ctx.producer_heartbeat("situation")
    age = heartbeat_age_min(view.heartbeat, now)
    available = age is not None and age <= s["unavailable_after_min"]
    changed = False

    # b. is Situation running?
    if not available and view.available:
        view.unavailable_since = view.heartbeat or now
        ctx.record_event("situation_unavailable", {
            "last_heartbeat": view.heartbeat.isoformat() if view.heartbeat else None,
            "last_version": view.version, "as_of": now.isoformat(),
            "effect": "fixes carry on with confirmed trip records; the last Situation picture is kept and marked old"})
        changed = True
    elif available and not view.available:
        ctx.record_event("situation_available_again", {"since": view.unavailable_since.isoformat() if view.unavailable_since else None,
                                                       "as_of": now.isoformat()})
        view.unavailable_since, view.escalated, changed = None, False, True
    view.available = available
    if not available and not view.escalated and (now - view.unavailable_since).total_seconds() / 60 > s["escalate_after_min"]:
        mins = int((now - view.unavailable_since).total_seconds() // 60)
        out = {"title": f"The Situation agent has not run for {mins} min",
               "why": "Communication keeps fixing screens with confirmed trip records, but has no current picture of the disruption",
               "data_as_of": now.isoformat(), "options": [], "findings": [], "decision_ids": ["O03"],
               "sources": [{"fact": "Situation heartbeat", "source": "situation agent",
                            "as_of": (view.heartbeat or now).isoformat()}]}
        ctx.propose("escalate_channel_problem", out)
        view.escalated = True

    # a, c, d. the picture itself (only a real version; an absent one stays absent)
    if pic is not None and pic["version"] != view.version:
        view.picture, view.version, changed = pic, pic["version"], True
        status = pic["facts"].get("status") or ""
        if status.startswith("cannot_assess"):
            if view.cannot_assess != status:
                ctx.record_event("situation_cannot_assess", {"version": pic["version"], "status": status,
                                                              "as_of": now.isoformat()})
            view.cannot_assess = status
        else:
            view.cannot_assess = None
        flights = {}
        for c in pic["facts"].get("conflicts") or []:
            for fid in c.get("flight_ids") or []:
                flights.setdefault(fid, c.get("key"))
        for fid in list(view.conflicts):
            if fid not in flights:
                gone = view.conflicts.pop(fid)
                item = {"flight_id": fid, "since": gone["since"].isoformat(), "cleared_at": now.isoformat(), "version": pic["version"]}
                view.cleared.append(item)
                ctx.record_event("conflict_hold_cleared", item)
        for fid, key in flights.items():
            view.conflicts.setdefault(fid, {"since": now, "version": pic["version"], "key": key})

    _disagreement(ctx, view, s)
    if changed:
        view.timeline.append({"t": now.isoformat(), "version": view.version, "available": view.available,
                              "status": (view.picture or {}).get("facts", {}).get("status"),
                              "conflict_flights": sorted(view.conflicts)})
    return view


def hold_reason(view: SituationView, flight_ids) -> tuple[str, list] | None:
    """a. If Situation reports conflicting data on one of these flights: the hold reason and the flights."""
    held = sorted(f for f in flight_ids if f in view.conflicts)
    if not held:
        return None
    first = held[0]
    return f"held: Situation reports conflicting data on {first} since {hhmm(view.conflicts[first]['since'])}", held


def _disagreement(ctx, view, s):
    """Shared-state rule 7: Situation and Communication both judge whether flight operations data is current. If they disagree
    for longer than disagree_after_min, both views are logged and a human is asked (nobody picks a side)."""
    if view.picture is None or not view.available:
        view.disagree_since, view.disagree_logged = None, False
        return
    theirs = any(x.get("source") == "flight_ops" for x in view.picture["facts"].get("stale_sources") or [])
    mine = ctx.feed_age_sec("flight_ops") > ctx.contract.max_data_age_sec
    if theirs == mine:
        view.disagree_since, view.disagree_logged = None, False
        return
    view.disagree_since = view.disagree_since or ctx.now
    if view.disagree_logged or (ctx.now - view.disagree_since).total_seconds() / 60 < s["disagree_after_min"]:
        return
    disagreement(ctx, "is flight operations data current?", {"flight_ops_stale": mine, "as_of": ctx.now.isoformat()},
                 {"flight_ops_stale": theirs, "version": view.version}, "situation")
    out = {"title": "Situation and Communication disagree about whether flight operations data is current",
           "why": "both views are logged; a human decides (the orchestrator will, later)", "data_as_of": ctx.now.isoformat(),
           "options": [], "findings": [], "decision_ids": ["O03"],
           "sources": [{"fact": "flight_ops heartbeat", "source": "flight_ops", "as_of": ctx.feed_last("flight_ops").isoformat()},
                       {"fact": f"Situation version {view.version}", "source": "situation agent",
                        "as_of": view.picture["time"].isoformat()}]}
    ctx.propose("escalate_channel_problem", out)
    view.disagree_logged = True
