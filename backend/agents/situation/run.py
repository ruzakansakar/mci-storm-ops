"""Run the Situation agent against a simulator run that is already in the database.

  python -m backend.agents.situation.run --run-id a31_storm-s7-today --at 12:20     # the assessment at that moment (nothing saved)
  python -m backend.agents.situation.run --run-id a31_storm-s7-today --replay       # step through the day, save every new version
  python -m backend.agents.situation.run --run-id a31_storm-s7-today --replay --stop faa_feed=12:00   # simulate a feed outage

It reads only from the database (through AgentContext) and config/agent_settings/situation.yaml. Never from the simulator.
The replay clock below wakes the agent every few minutes and when a trigger event happens; it is plumbing (like a message
bus), not the agent, and it only ever hands the agent a time. The agent itself sees nothing after that time.
"""
import copy
import argparse
import time
import uuid
from datetime import datetime, timedelta

from backend.agents.situation.assess import Memory, assess, load_settings
from backend.agents.situation.state_at import DAY0, hhmm, parse_clock
from backend.contracts import AgentContext
from backend.contracts.feeds import Feeds
from backend.database import connect


def wake_times(delivered: list[datetime], feeds: Feeds, s: dict, until: datetime | None = None) -> list[datetime]:
    """When the agent is woken: every few minutes, the moment a trigger event is DELIVERED (a late event wakes it when it
    arrives, not at the time it says it happened, which has already passed), and when a source goes quiet or comes back."""
    r = s["run"]
    start, end = parse_clock(r["day_start"]), parse_clock(r["day_end"])
    if until:
        end = min(end, until)
    times = set()
    t = start
    while t <= end:
        times.add(t)
        t += timedelta(minutes=r["every_min"])
    times |= set(delivered)
    # A source going quiet is a status change too: wake when it goes quiet, when its heartbeat is first too old (a watchdog
    # timer, like a real heartbeat monitor), and when it is back (its held-back events are delivered then).
    max_age = timedelta(seconds=int(r["watchdog_after_sec"]))
    for at in feeds.change_times():
        times |= {at, at + max_age}
    times = {t for t in times if start <= t <= end}
    return sorted(times)


def _times(cur, run_id: str, feeds: Feeds, s: dict, until: datetime | None = None) -> list[datetime]:
    r = s["run"]
    cur.execute("SELECT DISTINCT coalesce(published_at, occurred_at) FROM events WHERE run_id = %s AND type = ANY(%s)",
                (run_id, r["trigger_events"]))
    return wake_times([row[0] for row in cur.fetchall()], feeds, s, until)


def _save(memory: Memory) -> dict:
    return {"version": memory.version, "last": memory.last, "last_id": memory.last_id, "incident_no": memory.incident_no,
            "open": dict(memory.open), "asked": set(memory.asked), "last_p02": memory.last_p02, "n_hist": len(memory.history),
            "normal_since": memory.normal_since, "conflicts": copy.deepcopy(memory.conflicts)}


def _restore(memory: Memory, saved: dict) -> None:
    memory.version, memory.last, memory.last_id = saved["version"], saved["last"], saved["last_id"]
    memory.incident_no, memory.open, memory.asked = saved["incident_no"], saved["open"], saved["asked"]
    memory.last_p02, memory.normal_since = saved["last_p02"], saved["normal_since"]
    memory.conflicts = saved["conflicts"]
    del memory.history[saved["n_hist"]:]


def replay(conn, run_id: str, feeds: Feeds | None = None, until: datetime | None = None, agent_run_id: str | None = None,
           fail_at: datetime | None = None) -> dict:
    """Step through the day. Every replay gets its own agent_run_id, so two replays never mix."""
    s = load_settings()
    feeds = feeds or Feeds()
    agent_run_id = agent_run_id or f"situation-{uuid.uuid4().hex[:12]}"
    memory = Memory()
    timings, errors = [], []
    with conn.cursor() as cur:
        times = _times(cur, run_id, feeds, s, until)
        for t in times:
            ctx = AgentContext("situation", cur, now=t, run_id=agent_run_id, source_run_id=run_id, feeds=feeds)
            saved = _save(memory)
            t0 = time.perf_counter()
            try:
                with conn.transaction():
                    if fail_at is not None and t == fail_at:
                        assess(ctx, memory)
                        raise RuntimeError("test failure in the middle of an assessment")
                    assess(ctx, memory)
            except Exception as err:          # roll back: no partial assessment; one agent_error event with the reason
                _restore(memory, saved)
                ctx2 = AgentContext("situation", cur, now=t, run_id=agent_run_id, source_run_id=run_id, feeds=feeds)
                ctx2.record_event("agent_error", {"at": t.isoformat(), "error": f"{type(err).__name__}: {err}"})
                errors.append((t, str(err)))
            timings.append((t, time.perf_counter() - t0))
    return {"agent_run_id": agent_run_id, "memory": memory, "timings": timings, "errors": errors, "assessments": len(times)}


def at(conn, run_id: str, when: datetime, feeds: Feeds | None = None) -> dict:
    """The assessment at one moment (read only: nothing is saved)."""
    with conn.cursor() as cur:
        ctx = AgentContext("situation", cur, now=when, run_id="situation-at", source_run_id=run_id, feeds=feeds or Feeds())
        return assess(ctx, Memory(), write=False)["assessment"]


# ---------------------------------------------------------------- printing

def _range(a_or_h):
    ends = a_or_h.get("ends") or [a_or_h.get("end_earliest"), a_or_h.get("end_likely"), a_or_h.get("end_latest")]
    if not ends[1]:
        return "no end time"
    return f"likely {hhmm(ends[1])} (range {hhmm(ends[0])}-{hhmm(ends[2])})"


def describe(a: dict) -> str:
    imp = a.get("impact") or {}
    lines = [f"Situation at MCI as of {hhmm(a['assessed_at'])}: {a['status'].upper()}"]
    if a["status"] == "abnormal":
        lines.append(f"  Why abnormal : {'; '.join(a['abnormal_because'])}")
        lines.append(f"  Type / cause : {a['type']} | {a['likely_cause']} | confidence {a['confidence']:.2f}")
        lines.append(f"  Expected end : {_range(a)} | end confidence {a['end_confidence']} ({a['end_confidence_word']})")
        lines.append(f"  Controllable : event {a['incident_controllable']} | " + ", ".join(
            f"{f['flight_id']}={f['proposed']}" for f in a["flight_controllability"]) or "  Controllable : -")
        d, r = imp["departures"], imp["arrivals"]
        lines.append(f"  Departures   : {d}")
        lines.append(f"  Arrivals     : {r}")
        lines.append(f"  People       : ~{imp['passengers_on_affected_flights']} passengers on {imp['flights_affected']} affected flights | "
                     f"{imp['missed_connections']} missed connections | {imp['crews_on_affected_flights']} crews | "
                     f"terminal {imp['terminal']['people']}/{imp['terminal']['capacity']} | stranded overnight {imp['stranded_overnight']}")
        lines.append(f"  Out of place : {imp['aircraft_out_of_position'] or 'none'} | ramp open {imp['ramp']['open']} "
                     f"({imp['ramp']['closed_minutes_so_far']} min closed so far)")
        lines.append(f"  Worst        : " + "; ".join(f"{w['flight_id']} {w['why']}" for w in imp["worst_affected"]))
    if a["conflicts"]:
        lines.append("  Conflicts    : " + "; ".join(c["what"] for c in a["conflicts"]))
    lines.append(f"  Decisions    : {', '.join(a['decision_ids'])}")
    lines.append(f"  Summary      : {a['summary']}")
    return "\n".join(lines)


def timeline(result: dict) -> str:
    out = [f"Situation agent replay {result['agent_run_id']}: {result['assessments']} assessments, "
           f"{sum(1 for h in result['memory'].history if h['kind'] == 'version')} versions written, "
           f"{sum(1 for h in result['memory'].history if h['kind'] == 'proposal')} proposals, {len(result['errors'])} errors"]
    for h in result["memory"].history:
        if h["kind"] == "version":
            imp = h["impact"] or {}
            d, r = imp.get("departures", {}), imp.get("arrivals", {})
            counts = (f"{d.get('ever_delayed_30_plus', 0) + r.get('ever_delayed_30_plus', 0)} delayed, "
                      f"{d.get('cancelled', 0) + r.get('cancelled', 0)} cancelled, "
                      f"{d.get('ever_diverted', 0) + r.get('ever_diverted', 0)} diverted, {r.get('holding', 0)} holding") if imp else ""
            conf = f"{h['confidence']:.2f}" if h["confidence"] is not None else "-"
            out.append(f"{hhmm(h['at'])}  v{h['version']:<2} {h['status'][:40]:40s} {str(h['type'] or '-'):8s} conf {conf:4s} "
                       f"{_range(h):34s} {counts}")
        else:
            out.append(f"{hhmm(h['at'])}      -> proposed {h['decision']}: {h['title']}")
    worst = max(result["timings"], key=lambda x: x[1]) if result["timings"] else None
    if worst:
        total = sum(x[1] for x in result["timings"])
        out.append(f"Time: {total:.1f} s in all, {total / len(result['timings']) * 1000:.0f} ms on average, "
                   f"slowest {worst[1] * 1000:.0f} ms at {hhmm(worst[0])}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Run the Situation agent on a simulator run in the database")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--at", help="HH:MM: print the assessment at that moment (nothing saved)")
    ap.add_argument("--replay", action="store_true", help="step through the day and save every new version")
    ap.add_argument("--stop", action="append", default=[], help="feed=HH:MM: simulate a feed outage from that time")
    args = ap.parse_args()
    feeds = Feeds({k: parse_clock(v) for k, v in (x.split("=") for x in args.stop)})
    with connect() as conn:
        conn.autocommit = True
        if args.at:
            print(describe(at(conn, args.run_id, parse_clock(args.at), feeds)))
        if args.replay:
            print(timeline(replay(conn, args.run_id, feeds)))
        if not args.at and not args.replay:
            ap.error("give --at HH:MM or --replay")


if __name__ == "__main__":
    main()
