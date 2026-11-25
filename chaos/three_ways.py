"""Phase 5b: the same chaos day three ways (today, copilot, orchestrated), in memory. Uses the same published overlays, late
deliveries and feed outages for all three, so only the process differs. Usage: python -m chaos.three_ways 6001 6002 ..."""
import json
import sys

from backend.seed.build import build
from simulator.clock import at


def kwargs_for(seed, data):
    from backend.contracts.feeds import Feeds
    from chaos.generator import overlay_events, plan_day, scenario_for
    from chaos.memory_replay import late_deliveries
    from simulator.run import simulate
    plan = plan_day(seed)
    outages = {}
    for o in plan.feed_outages:
        outages.setdefault(o["feed"], []).append((at(o["start"]), at(o["end"])))
    for p in (plan.program, plan.second):
        if p and p["kind"] == "it_outage":
            outages.setdefault("flight_ops", []).append((at(p["start"]), at(p["end"])))
    base = simulate(seed, "chaos", data=data, prebuilt=scenario_for(plan))
    overlay = overlay_events(plan, base.world.events, base.world.flights)
    late, _ = late_deliveries(plan, base.world.events)

    def key(e):
        return (e["t"], e["type"], json.dumps(e["payload"], sort_keys=True))
    delayed = {key(base.world.events[i]): m for i, m in late.items()}
    return {"prebuilt": scenario_for(plan), "feeds": Feeds(outages=outages), "source_overlay": overlay,
            "late_delivery": lambda e: delayed.get(key(e), e.get("published_t"))}


def day(seed, data):
    from simulator.run import simulate
    kw = kwargs_for(seed, data)
    out = {}
    for mode in ("today", "copilot", "orchestrated"):
        r = simulate(seed, "chaos", mode=mode, data=data, **{**kw, "prebuilt": _fresh(seed)})
        out[mode] = {k: v for k, v in r.metrics["three_ways"].items() if k != "maya"}
        out[mode]["truth"] = r.metrics["truth_sha256"]
    return out


def _fresh(seed):
    from chaos.generator import plan_day, scenario_for
    return scenario_for(plan_day(seed))


if __name__ == "__main__":
    data = build()
    rows = {}
    for s in map(int, sys.argv[1:]):
        rows[s] = day(s, data)
        print(json.dumps({s: rows[s]}, default=str), flush=True)
