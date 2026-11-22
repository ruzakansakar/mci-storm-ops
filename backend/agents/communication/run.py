"""Run the Communication agent (4b-1: detect only) against a simulator run that is already in the database.

  python -m backend.agents.communication.run --run-id a31_storm-s7-today --at 14:58         # what is wrong at that moment (nothing saved)
  python -m backend.agents.communication.run --run-id a31_storm-s7-today --replay           # the whole day, saving its mismatches
  python -m backend.agents.communication.run --run-id a31_storm-s7-today --passenger PAX0001 --replay   # Maya only, next to the truth

It reads only through AgentContext and config/agent_settings/communication.yaml. The replay clock wakes it every simulated minute;
it is plumbing and hands the agent only a time. If a scan fails it is rolled back (nothing partial) and one agent_error is written.
"""
import argparse
import copy
import time
import uuid
from collections import Counter
from datetime import timedelta

from backend.agents.communication.detect import Memory, load_settings, scan
from backend.agents.communication.state_at import clock, hhmm
from backend.contracts import AgentContext
from backend.contracts.feeds import Feeds
from backend.database import connect


def replay(conn, run_id: str, feeds: Feeds | None = None, until=None, agent_run_id: str | None = None, fail_at=None) -> dict:
    s = load_settings()
    feeds = feeds or Feeds()
    agent_run_id = agent_run_id or f"communication-{uuid.uuid4().hex[:12]}"
    memory = Memory()
    t, end = clock(s["run"]["day_start"]), clock(s["run"]["day_end"])
    if until:
        end = min(end, until)
    timings, errors = [], []
    with conn.cursor() as cur:
        while t <= end:
            ctx = AgentContext("communication", cur, now=t, run_id=agent_run_id, source_run_id=run_id, feeds=feeds)
            saved = copy.deepcopy(memory.__dict__)
            t0 = time.perf_counter()
            try:
                with conn.transaction():
                    scan(ctx, memory, settings=s)
                    if fail_at is not None and t == fail_at:
                        raise RuntimeError("test failure in the middle of a scan")
            except Exception as err:            # roll back: nothing partial is written, and the memory goes back as well
                memory.__dict__.clear()
                memory.__dict__.update(saved)
                AgentContext("communication", cur, now=t, run_id=agent_run_id, source_run_id=run_id, feeds=feeds).record_event(
                    "agent_error", {"at": t.isoformat(), "error": f"{type(err).__name__}: {err}"})
                errors.append((t, str(err)))
            timings.append((t, time.perf_counter() - t0))
            t += timedelta(minutes=s["run"]["every_min"])
    return {"agent_run_id": agent_run_id, "memory": memory, "timings": timings, "errors": errors}


def at(conn, run_id: str, when, feeds: Feeds | None = None) -> dict:
    """What is wrong at that moment (read only: nothing is saved)."""
    with conn.cursor() as cur:
        ctx = AgentContext("communication", cur, now=when, run_id="communication-at", source_run_id=run_id, feeds=feeds or Feeds())
        return scan(ctx, Memory(), write=False, full=True)


# ---------------------------------------------------------------- printing

def describe(res: dict) -> str:
    f = res["findings"]
    lines = [f"Channels at MCI as of {hhmm(res['t'])}: {len(f)} open mismatches on {len({x['passenger_id'] for x in f})} passengers"]
    if res["cannot_check"]:
        lines.append("  cannot_check: " + "; ".join(res["cannot_check"]))
    by = lambda k: ", ".join(f"{a} {b}" for a, b in sorted(Counter(x[k] for x in f).items()))   # noqa: E731
    lines.append(f"  By channel  : {by('channel')}")
    lines.append(f"  By severity : {by('severity')}")
    lines.append(f"  By cause    : {by('suspected_cause')}")
    flights = sorted(res["by_flight"].items(), key=lambda kv: -len(kv[1]["passengers"]))[:8]
    lines.append("  By flight   : " + "; ".join(f"{fid} {len(v['passengers'])} passengers (" + ", ".join(
        f"{ch} {len(p)}" for ch, p in sorted(v["by_channel"].items())) + ")" for fid, v in flights))
    crit = [x for x in f if x["severity"] == "critical"]
    lines.append(f"  Critical ({len(crit)}):")
    for x in crit[:25]:
        lines.append(f"    {x['passenger_id']} {x['channel']:11s} {x['field']:10s} shows {x['shown_value']!r} but true is {x['true_value']!r} "
                     f"since {x['since'][11:16]} ({x['age_min']} min) | {x['suspected_cause']} {x['cause_confidence']:.1f} | "
                     f"{'; '.join(x['severity_why'])}")
    return "\n".join(lines)


def timeline(result: dict) -> str:
    m = result["memory"]
    scans = [h for h in m.history if h["kind"] == "scan"]
    out = [f"Communication agent replay {result['agent_run_id']}: {len(result['timings'])} scans, "
           f"{sum(1 for h in m.history if h['kind'] == 'detected')} mismatches detected, "
           f"{sum(1 for h in m.history if h['kind'] == 'cleared')} cleared, "
           f"{sum(1 for h in m.history if h['kind'] == 'escalation')} escalations, {len(result['errors'])} errors",
           "time   open: critical high medium low | new critical | escalations"]
    last = None
    esc = {}
    for h in m.history:
        if h["kind"] == "escalation":
            esc.setdefault(h["at"], []).append(h["title"])
    for h in scans:
        o = h["open"]
        row = (o.get("critical", 0), o.get("high", 0), o.get("medium", 0), o.get("low", 0))
        if row != last or h["new_critical"] or h["at"] in esc:
            nc = f"{len(h['new_critical'])} new critical" if h["new_critical"] else ""
            out.append(f"{hhmm(h['at'])}  {row[0]:4d} {row[1]:4d} {row[2]:6d} {row[3]:4d} | {nc:16s} | " + "; ".join(esc.get(h["at"], [])))
            last = row
    secs = [x[1] for x in result["timings"]]
    out.append(f"Time: {sum(secs):.1f} s in all, {sum(secs) / len(secs) * 1000:.0f} ms a scan on average, slowest {max(secs) * 1000:.0f} ms")
    return "\n".join(out)


def passenger(result: dict, pid: str, conn=None, run_id: str | None = None) -> str:
    """Each mismatch it found on this passenger's screens, next to when it really started and ended (from the channel events)."""
    m = result["memory"]
    out = [f"{pid}: what the agent found on each screen (severity and cause as it saw them; it never sees the simulator's reasons)"]
    det = [h for h in m.history if h["kind"] == "detected" and h["key"][0] == pid]
    for h in sorted(det, key=lambda h: (h["since"], h["key"][1], h["key"][2])):
        clr = next((c for c in m.history if c["kind"] == "cleared" and c["key"] == h["key"] and c["since"] == h["since"]), None)
        changes = [c for c in m.history if c["kind"] == "changed" and c["key"] == h["key"] and c["since"] == h["since"]]
        later = "; ".join(f"{hhmm(c['at'])} {', '.join(f'{k} -> {v}' for k, v in c.items() if k in ('severity', 'cause'))}"
                          for c in changes if "severity" in c or "cause" in c)
        out.append(f"  {hhmm(h['since'])} detected {hhmm(h['at'])}  {h['key'][1]:11s} {h['key'][2]:10s} shows {h['shown']!r}, true {h['true']!r}"
                   f" | {h['cause']} | {h['severity']} ({'; '.join(h['why'])})" + (f" | later: {later}" if later else "")
                   + f" | cleared {hhmm(clr['ended']) if clr else 'still open at end of day'}")
    if conn is not None and run_id:
        out.append(f"  What really happened on {pid}'s screens (channel updates and delivery receipts, from the run's events):")
        rows = conn.execute("SELECT occurred_at, type, payload FROM events WHERE run_id=%s AND payload->>'passenger_id'=%s AND type IN "
                            "('channel_view_updated','channel_update_failed','trip_record_updated') ORDER BY occurred_at, id",
                            (run_id, pid)).fetchall()
        for occ, typ, p in rows:
            if typ == "trip_record_updated":
                what = f"TRUTH: {' > '.join(p['itinerary'])} {p['status']}"
            elif typ == "channel_update_failed":
                what = f"{p['channel']}: update FAILED (delivery receipt)"
            else:
                what = f"{p['channel']} now shows {' > '.join(p['shown_itinerary'])} {p['shown_status']}" if p["kind"] == "main" else \
                    f"{p['channel']} bag routing now {' > '.join(p['shown_bag_routing']) or '(none)'}"
            out.append(f"    {hhmm(occ)}  {what}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Run the Communication agent (detect only) on a simulator run in the database")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--at", help="HH:MM: what is wrong at that moment (nothing saved)")
    ap.add_argument("--replay", action="store_true", help="the whole day: save its mismatches and print a timeline")
    ap.add_argument("--passenger", help="with --replay: print this passenger's detections next to what really happened")
    ap.add_argument("--stop", action="append", default=[], help="feed=HH:MM: simulate a feed outage from that time")
    args = ap.parse_args()
    feeds = Feeds({k: clock(v) for k, v in (x.split("=") for x in args.stop)})
    with connect() as conn:
        conn.autocommit = True
        if args.at:
            print(describe(at(conn, args.run_id, clock(args.at), feeds)))
        if args.replay:
            res = replay(conn, args.run_id, feeds)
            print(passenger(res, args.passenger, conn, args.run_id) if args.passenger else timeline(res))
        if not args.at and not args.replay:
            ap.error("give --at HH:MM or --replay")


if __name__ == "__main__":
    main()
