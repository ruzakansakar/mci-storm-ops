"""Replay the Situation agent on a chaos day IN MEMORY (no database), then score it with the real judge.

Test harness only: it is used to tune settings on the TRAINING seeds (1-100) quickly, and to measure them. The agent code is the
production code (backend/agents/situation/assess.py); only the context it reads through is replaced by MemoryContext, which makes
the same cuts as AgentContext: events delivered by now, only the agent's feeds and the payload fields its contract allows, nothing
from a feed while it is silent, heartbeats from Feeds. The day is the same one `python -m chaos.run --seed N` plays (same events,
same late deliveries, same overlay), so a day scored here and in the database should agree. tests/test_chaos.py checks one.

  python -m chaos.memory_replay --seeds 1-100          # summary of verdicts and per-check results on training seeds
"""
import argparse
import random
import time
from collections import Counter
from datetime import datetime

from backend.agents.situation.assess import Memory, assess, load_settings
from backend.agents.situation.run import wake_times
from backend.contracts import load_contracts
from chaos.generator import load_chaos, overlay_events, plan_day, scenario_for, truth_for
from chaos.run import LATE_TYPES, feeds_for
from simulator.clock import at

SYSTEMS = ["Airport operations (simulated)", "Amadeus Altea (simulated)", "Baggage system (simulated)",
           "Crew Optimization (simulated)", "Hotel vendor (simulated)", "Maintenance tracking (simulated)",
           "Revenue management (simulated)", "Station staffing (simulated)"]


def late_deliveries(plan, world_events):
    """The same late deliveries ChaosWriter makes (same random stream, same event order)."""
    left = plan.messy.get("late", 0)
    after = plan.program["start"] if plan.program else 8 * 60
    rng = random.Random(f"chaos-late:{plan.seed}")
    out, late = {}, []
    for i, e in enumerate(world_events):
        if e["type"] in ("channel_view_updated", "channel_update_failed"):
            continue
        if left and e["type"] in LATE_TYPES and e["t"] >= after and rng.random() < 0.25:
            left -= 1
            delay = rng.randint(*load_chaos()["messy_data"]["late_by_min"])
            out[i] = e["t"] + delay
            late.append({"type": e["type"], "at": at(e["t"]).isoformat(), "delivered": at(e["t"] + delay).isoformat(),
                         "flight_id": (e.get("payload") or {}).get("flight_id")})
    return out, late


def play_day(seed: int, data: dict) -> dict:
    """The chaos day as the database would hold it: events (with delivery times), the published schedule, and the truth."""
    from simulator.run import simulate
    plan = plan_day(seed)
    res = simulate(seed, "chaos", data=data, prebuilt=scenario_for(plan), run_id=f"chaos-{seed}")
    world = res.world.events
    over = overlay_events(plan, world, res.world.flights)
    late, late_list = late_deliveries(plan, world)
    truth = truth_for(plan, res.world, over)
    truth["messy"]["late_events"] = late_list
    from chaos.generator import add_visibility
    add_visibility(truth)
    contract = load_contracts()["situation"]
    events = []
    rows = [(e, late.get(i), e.get("actor_name")) for i, e in enumerate(world)] + [(o, o.get("late_t"), o["feed"]) for o in over]
    for n, (e, late_t, _) in enumerate(rows):
        feed, fields = contract.event_types.get(e["type"], (None, None))
        if feed is None:
            continue
        events.append({"id": n + 1, "occurred_at": at(e["t"]), "published_at": at(late_t) if late_t is not None else None,
                       "type": e["type"], "station": e.get("station"), "payload": dict(e["payload"] or {}), "feed": feed,
                       "_fields": fields})
    schedule = [{"id": f["id"], "direction": f["direction"], "origin": f["origin"], "destination": f["destination"],
                 "sched_dep": at(f["sched_dep"]), "sched_arr": at(f["sched_arr"]), "crew_id": f.get("crew_id")}
                for f in res.world.flights.values() if not f.get("_next_day")]
    return {"seed": seed, "truth": truth, "events": events, "schedule": schedule}


class _Contract:
    def __init__(self, c):
        self.max_data_age_sec = c.max_data_age_sec


class MemoryContext:
    """Stands in for AgentContext: the same cuts on what the agent can see, and it keeps what the agent writes in lists."""

    def __init__(self, day, now, feeds, sink):
        self.day, self.now, self.feeds, self.sink = day, now, feeds, sink
        self.contract = _Contract(load_contracts()["situation"])

    def read_events(self, since=None):
        out = []
        for e in self.day["events"]:
            delivered = e["published_at"] or e["occurred_at"]
            if delivered > self.now:
                continue
            silent = self.feeds.window(e["feed"], self.now)
            if silent and e["occurred_at"] >= silent[0]:
                continue
            out.append({"id": e["id"], "occurred_at": e["occurred_at"], "type": e["type"], "station": e["station"], "feed": e["feed"],
                        "payload": {k: v for k, v in e["payload"].items() if k in e["_fields"]}})
        return sorted(out, key=lambda e: e["occurred_at"])

    def read(self, table, **_):
        if table == "flights":
            return [dict(f) for f in self.day["schedule"]]
        if table == "system_health":
            return [{"system": s, "status": "up", "last_heartbeat": None, "note": ""} for s in SYSTEMS]
        raise KeyError(table)

    def feed_last(self, feed):
        return self.feeds.last_heartbeat(feed, self.now)

    def feed_age_sec(self, feed):
        return (self.now - self.feed_last(feed)).total_seconds()

    def write_assessment(self, values):
        self.sink["versions"].append(dict(values))
        return len(self.sink["versions"])

    def record_event(self, type_, payload=None, station=None):
        self.sink["events"].append({"occurred_at": self.now, "type": type_, "payload": payload})
        return len(self.sink["events"])

    def propose(self, action, output, safety_rung=None, supersedes=None):
        rid = len(self.sink["recs"]) + 1
        self.sink["recs"].append({"id": rid, "created_at": self.now, "decision_ids": output["decision_ids"], "supersedes": supersedes,
                                  "autonomy": "needs_approval", "title": output["title"]})
        return rid

    def ask_human(self, decision_id, facts, reason):
        return self.record_event("human_decision_needed", {"decision_id": decision_id, "facts": facts, "reason": reason})


def replay_day(day: dict, settings: dict | None = None) -> dict:
    """Run the agent through the day and return its outputs in the judge's format."""
    s = settings or load_settings()
    feeds = feeds_for(day["truth"])
    trig = set(s["run"]["trigger_events"])
    delivered = sorted({e["published_at"] or e["occurred_at"] for e in day["events"] if e["type"] in trig})
    sink = {"versions": [], "events": [], "recs": []}
    memory, errors, secs = Memory(), [], []
    for t in wake_times(delivered, feeds, s):
        ctx = MemoryContext(day, t, feeds, sink)
        t0 = time.perf_counter()
        try:
            assess(ctx, memory, settings=s)
        except Exception as err:                     # same as the production replay: record it and carry on
            errors.append([t.isoformat(), f"{type(err).__name__}: {err}"])
        secs.append([t.isoformat(), round(time.perf_counter() - t0, 3)])
    versions = []
    for v in sink["versions"]:
        versions.append({"version": v["version"], "assessed_at": v["assessed_at"], "status": v["status"], "type": v["type"],
                         "likely_cause": v["likely_cause"], "confidence": v["confidence"],
                         "incident_controllable": v["incident_controllable"], "flight_controllability": v["flight_controllability"],
                         "expected_end_earliest": v["expected_end_earliest"], "expected_end_likely": v["expected_end_likely"],
                         "expected_end_latest": v["expected_end_latest"], "end_confidence": v["end_confidence"],
                         "impact": v["impact"], "conflicts": v["conflicts"], "stale_sources": v["stale_sources"],
                         "decision_ids": v["decision_ids"]})
    return {"agent_run_id": f"memory-{day['seed']}", "versions": versions, "recs": sink["recs"], "events": sink["events"],
            "timing": {"agent_run_id": f"memory-{day['seed']}", "errors": errors, "seconds": secs}, "memory": memory}


def score(day: dict, settings: dict | None = None) -> dict:
    import copy
    from chaos.judge import judge
    out = replay_day(day, settings)
    sc = judge(copy.deepcopy(day["truth"]), out)
    sc["outputs"] = out
    return sc


def parse_seeds(text: str) -> list[int]:
    out = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def summary(scores: list[dict]) -> str:
    from chaos.judge import CHECKS
    lines = [f"{len(scores)} days: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(s["verdict"] for s in scores).items()))]
    for ch in CHECKS:
        c = Counter(next(x["result"] for x in s["checks"] if x["name"] == ch) for s in scores)
        judged = c["PASS"] + c["WARN"] + c["FAIL"]
        rate = f"{c['PASS'] / judged:.0%}" if judged else "-"
        lines.append(f"  {ch:15s} pass {rate:>4s}  " + " ".join(f"{k} {c[k]}" for k in ("PASS", "WARN", "FAIL", "N/A") if c[k]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true", help="seed 7 plus 20 fresh paired today/copilot days")
    ap.add_argument("--seeds", default="1-100")
    ap.add_argument("--show", action="store_true", help="print each day's WARN and FAIL checks")
    args = ap.parse_args(argv)
    if args.compare:
        from chaos.compare import main as compare_main
        return compare_main([])
    from backend.seed.build import build
    data = build()
    scores = []
    for seed in parse_seeds(args.seeds):
        sc = score(play_day(seed, data))
        scores.append(sc)
        if args.show:
            bad = [f"{c['name']}: {c['result']} {c['reason']}" for c in sc["checks"] if c["result"] in ("WARN", "FAIL")]
            if bad:
                print(f"{seed} {sc['verdict']} | {sc['summary'][:80]}\n     " + "\n     ".join(bad))
    print(summary(scores))


if __name__ == "__main__":
    main()
