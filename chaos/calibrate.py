"""Calibrate the Situation agent's expected-end ranges and end confidences on chaos TRAINING days, then report on TEST days.

  python -m chaos.calibrate                      # measure the current settings on training (1-100) and test (101-150)
  python -m chaos.calibrate --search --write     # search on training days ONLY, write the frozen values to the settings file

Training seeds 1-100 are the only ones ever used to choose numbers. Test seeds 101-150 are only measured, never tuned on.
This is a tool for tuning the agent's settings, so it plays the days in memory (fast, no database) and calls the agent's own
expected-end rule at every 5-minute tick of each incident. It sees what the agent would see: the same event types and payload
fields its contract allows.
"""
import argparse
import itertools
import re
from datetime import timedelta

from backend.agents.situation import assess as A
from backend.agents.situation.state_at import build_state
from backend.contracts import load_contracts
from chaos.generator import overlay_events, plan_day, scenario_for, truth_for
from simulator.clock import at

TRAIN, TEST = range(1, 101), range(101, 151)


def visible_events(world_events, overlay, contract):
    """The events as AgentContext would hand them over (types and payload fields from the contract; delivered by their time)."""
    out = []
    for i, e in enumerate(list(world_events) + [{**o, "actor_name": o["feed"]} for o in overlay]):
        feed, fields = contract.event_types.get(e["type"], (None, None))
        if feed is None:
            continue
        out.append({"id": i + 1, "occurred_at": at(e["t"]), "type": e["type"], "station": e.get("station"),
                    "payload": {k: v for k, v in (e["payload"] or {}).items() if k in fields}, "feed": feed})
    return out


def play(seed, data):
    from simulator.run import simulate
    plan = plan_day(seed)
    res = simulate(seed, "chaos", data=data, prebuilt=scenario_for(plan), run_id=f"chaos-{seed}")
    over = overlay_events(plan, res.world.events, res.world.flights)
    truth = truth_for(plan, res.world, over)
    schedule = [{"id": f["id"], "direction": f["direction"], "origin": f["origin"], "destination": f["destination"],
                 "sched_dep": at(f["sched_dep"]), "sched_arr": at(f["sched_arr"]), "crew_id": f.get("crew_id")}
                for f in res.world.flights.values() if not f.get("_next_day")]
    return truth, visible_events(res.world.events, over, load_contracts()["situation"]), schedule


def samples(days):
    """Every 5-minute tick inside each incident that has a real end: the state the agent would build at that tick."""
    out = []
    for truth, events, schedule in days:
        for inc in truth["incidents"]:
            if not inc.get("has_end"):
                continue
            from datetime import datetime
            t, end = datetime.fromisoformat(inc["start"]), datetime.fromisoformat(inc["end"])
            while t < end:
                seen = [e for e in events if e["occurred_at"] <= t]
                st = build_state(schedule, seen, t)
                if st.program and st.program.ended is None:
                    out.append((st, inc["type"], end))
                t += timedelta(minutes=5)
    return out


def measure(points, settings):
    n = inside = width = 0
    by_conf = {}
    for st, kind, real in points:
        e = A._expected_end(st, kind, settings)
        if not e["latest"]:
            continue
        ok = e["earliest"] <= real <= e["latest"]
        n += 1
        inside += ok
        width += (e["latest"] - e["earliest"]).total_seconds() / 60
        b = by_conf.setdefault(e["confidence"], [0, 0])
        b[0] += 1
        b[1] += ok
    high = [(c, k, h) for c, (k, h) in by_conf.items() if c >= settings["expected_end"]["confidence_words"]["high"]]
    hn, hi = sum(k for _, k, _ in high), sum(h for _, _, h in high)
    hc = sum(c * k for c, k, _ in high) / hn if hn else None
    return {"n": n, "coverage": inside / n if n else 0, "avg_width_min": width / n if n else 0,
            "high_n": hn, "high_right": hi / hn if hn else None, "high_claims": hc,
            "by_confidence": {c: f"{h}/{k}" for c, (k, h) in sorted(by_conf.items())}}


def by_changes(points, settings):
    """Coverage by how many times the announced end has changed so far (to set honest confidences)."""
    out = {}
    for st, kind, real in points:
        e = A._expected_end(st, kind, settings)
        n = min(st.program.extensions + st.program.shortened, 4)
        b = out.setdefault(n, [0, 0])
        b[0] += 1
        b[1] += e["earliest"] <= real <= e["latest"]
    return {k: (h / n, n) for k, (n, h) in sorted(out.items())}


def search(points, base):
    best = None
    for early, m0, per_ext, per_hour in itertools.product((0, 10, 20, 30), (15, 30, 45, 60, 90, 120), (0, 15, 30, 45, 60), (0, 10, 20, 30)):
        s = _with(base, early, m0, per_ext, per_hour, base["expected_end"]["confidence_by_changes"])
        m = measure(points, s)
        if m["coverage"] >= 0.8 and (best is None or m["avg_width_min"] < best[0]["avg_width_min"]):
            best = (m, (early, m0, per_ext, per_hour))
    return best


def _with(base, early, m0, per_ext, per_hour, conf):
    import copy
    s = copy.deepcopy(base)
    x = s["expected_end"]
    x["early_margin_min"] = early
    x["base_margin_min"] = {k: m0 for k in x["base_margin_min"]}
    x["margin_per_extension_min"], x["margin_per_hour_running_min"] = per_ext, per_hour
    x["confidence_by_changes"] = list(conf)
    return s


def write_settings(early, m0, per_ext, per_hour, conf):
    path = A.SETTINGS_FILE
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(  early_margin_min: )\d+", rf"\g<1>{early}", text)
    block = re.search(r"  base_margin_min:.*?\n((?:    \w+: \d+\n)+)", text, flags=re.S)
    new_block = "".join(f"    {k}: {m0}\n" for k in re.findall(r"    (\w+): \d+\n", block.group(1)))
    text = text.replace(block.group(1), new_block)
    text = re.sub(r"(  margin_per_extension_min: )\d+", rf"\g<1>{per_ext}", text)
    text = re.sub(r"(  margin_per_hour_running_min: )\d+", rf"\g<1>{per_hour}", text)
    text = re.sub(r"(  confidence_by_changes: )\[[^\]]*\]", rf"\g<1>[{', '.join(str(c) for c in conf)}]", text)
    path.write_text(text, encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", action="store_true", help="search margins on the training days only")
    ap.add_argument("--write", action="store_true", help="write the chosen values to config/agent_settings/situation.yaml")
    args = ap.parse_args(argv)
    from backend.seed.build import build
    data = build()
    A.load_settings.cache_clear()
    base = A.load_settings()
    print("playing training days 1-100 and test days 101-150 in memory ...", flush=True)
    train = samples([play(s, data) for s in TRAIN])
    test = samples([play(s, data) for s in TEST])
    print(f"BEFORE  training: {measure(train, base)}")
    print(f"BEFORE  test    : {measure(test, base)}")
    if not args.search:
        return
    m, (early, m0, per_ext, per_hour) = search(train, base)
    s = _with(base, early, m0, per_ext, per_hour, base["expected_end"]["confidence_by_changes"])
    changes = by_changes(train, s)
    # an honest confidence for each number of changes so far: what the training days showed, rounded down
    conf = [round(int(changes.get(k, (0.2, 0))[0] * 20) / 20, 2) for k in range(5)]
    conf = [max(0.2, min(0.95, c)) for c in conf]
    # never more sure after more changes than after fewer (small samples can say otherwise; we don't believe them)
    for k in range(1, len(conf)):
        conf[k] = min(conf[k], conf[k - 1])
    s = _with(base, early, m0, per_ext, per_hour, conf)
    print(f"CHOSEN on training only: early margin {early}, base margin {m0}, per change {per_ext}, per hour running {per_hour}, "
          f"confidence by changes {conf}")
    print(f"AFTER   training: {measure(train, s)}")
    print(f"AFTER   test    : {measure(test, s)}")
    if args.write:
        write_settings(early, m0, per_ext, per_hour, conf)
        print(f"written to {A.SETTINGS_FILE}")


if __name__ == "__main__":
    main()
