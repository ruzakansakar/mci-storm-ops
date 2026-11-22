"""Calibrate the Situation agent's expected-end ranges and end confidences on chaos TRAINING days, and measure them by group.

  python -m chaos.calibrate                         # the calibration table by group, training days 1-100 (current settings)
  python -m chaos.calibrate --seeds 5000-5059       # the same table on other days (measured only, never tuned on)
  python -m chaos.calibrate --search --write        # choose margins and confidences on training days ONLY, write them
  python -m chaos.calibrate --baseline              # the normal number of newly delayed flights per window, by hour

Training seeds 1-100 are the only ones ever used to choose numbers. Other seeds are only measured.
It plays the days in memory (chaos/memory_replay.py: the same events, late deliveries and feed silences the agent sees in the
database) and calls the agent's own expected-end rule at every 5-minute tick of each FAA program, while the agent could assess
(no feed silent). A tick counts as "inside" when the real end falls between the earliest and the latest end.
"""
import argparse
import copy
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta

from backend.agents.situation import assess as A
from backend.agents.situation.state_at import build_state
from backend.settings import TZ
from chaos.memory_replay import MemoryContext, parse_seeds, play_day
from chaos.run import feeds_for

TRAIN = range(1, 101)
GROUP_ORDER = ["all", "changes", "cause", "time", "messy"]


def _days(seeds, data):
    return [play_day(s, data) for s in seeds]


def samples(days, settings):
    """Every 5-minute tick inside each FAA program that really ended: (state, the program's cause as the agent sees it, real end,
    its groups)."""
    out = []
    for day in days:
        feeds = feeds_for(day["truth"])
        for inc in day["truth"]["incidents"]:
            if not inc.get("has_end"):
                continue
            t, end = datetime.fromisoformat(inc["start"]), datetime.fromisoformat(inc["end"])
            while t < end:
                ctx = MemoryContext(day, t, feeds, None)
                if all(ctx.feed_age_sec(f) <= ctx.contract.max_data_age_sec for f in settings["data"]["depends_on"]):
                    st = build_state(day["schedule"], ctx.read_events(), t)
                    if st.program and st.program.ended is None:
                        kind = A.program_cause(st.program, A._classify(st, A._health([], st), settings)["type"], settings)
                        out.append({"st": st, "kind": kind, "real": end, "seed": day["seed"], "groups": groups_of(st, kind)})
                t += timedelta(minutes=5)
    return out


def groups_of(st, kind) -> dict:
    """The groups a tick belongs to, from what the agent can see at that moment (not from the truth)."""
    n = st.program.extensions + st.program.shortened
    messy = any(st.t - timedelta(minutes=30) <= when <= st.t for when, _, _ in st.messy)
    return {"all": "all", "changes": "0" if n == 0 else "1" if n == 1 else "2+", "cause": kind,
            "time": "evening (18:00+)" if st.t.astimezone(TZ).hour >= 18 else "before 18:00",
            "messy": "messy data in last 30 min" if messy else "no messy data in last 30 min"}


def evaluate(points, settings):
    """Run the agent's rule on every tick: inside the range or not, its confidence, the range width."""
    high = settings["expected_end"]["confidence_words"]["high"]
    rows = []
    for p in points:
        e = A._expected_end(p["st"], p["kind"], settings)
        if not e["latest"]:
            continue
        rows.append({**p, "inside": e["earliest"] <= p["real"] <= e["latest"], "conf": e["confidence"],
                     "high": e["confidence"] >= high, "width": (e["latest"] - e["earliest"]).total_seconds() / 60})
    return rows


def table(rows) -> dict:
    out = {}
    for g in GROUP_ORDER:
        buckets = defaultdict(list)
        for r in rows:
            buckets[r["groups"][g]].append(r)
        for name, rs in sorted(buckets.items()):
            hi = [r for r in rs if r["high"]]
            out[(g, name)] = {"n": len(rs), "inside": sum(r["inside"] for r in rs) / len(rs),
                              "width": sum(r["width"] for r in rs) / len(rs), "high_n": len(hi),
                              "high_right": sum(r["inside"] for r in hi) / len(hi) if hi else None,
                              "high_claims": sum(r["conf"] for r in hi) / len(hi) if hi else None,
                              "days": len({r["seed"] for r in rs})}
    return out


def show(tab, title) -> str:
    lines = [title, f"  {'group':38s} {'ticks':>5s} {'days':>4s} {'end in range':>12s} {'avg width':>9s} {'said high':>9s} "
                    f"{'high right':>10s} {'high claims':>11s}  honest?"]
    for (g, name), m in tab.items():
        hr = f"{m['high_right']:.0%}" if m["high_right"] is not None else "-"
        hc = f"{m['high_claims']:.0%}" if m["high_claims"] is not None else "-"
        ok = "-" if m["high_right"] is None else ("yes" if m["high_right"] >= m["high_claims"] - 1e-9 else "NO")
        label = name if g == "all" else f"{g}: {name}"
        lines.append(f"  {label:38s} {m['n']:5d} {m['days']:4d} {m['inside']:12.0%} {m['width']:8.0f}m {m['high_n']:9d} {hr:>10s} "
                     f"{hc:>11s}  {ok}")
    return "\n".join(lines)


# ---------------------------------------------------------------- choosing the numbers (training days only)

def search(points, base) -> dict:
    """Choose the margins and the confidence table on the TRAINING ticks only.
    Early margin, per cause: the smallest that covers the stops that ended EARLY (before the end last announced) on at least
    `target` of their ticks. (Chosen this way because an early end misses a whole day, not a few moments; on training days the
    narrowest-overall rule left two whole storms outside the range.)
    Late margin, per cause and number of changes so far: the smallest that puts the real end inside the range on at least
    `target` of those ticks. It never shrinks with more changes. Extra margins (evening, growing storm, messy FAA data): the
    smallest that brings the ticks they apply to up to `target` too. Confidence: each group's real coverage, rounded down, and
    never higher after more changes than after fewer."""
    s = copy.deepcopy(base)
    x = s["expected_end"]
    target = x["calibration"]["target_coverage"]
    grid = list(range(0, x["max_margin_min"] + 1, 5))

    def cov(sub):
        rs = evaluate(sub, s)
        return sum(r["inside"] for r in rs) / len(rs) if rs else 1.0

    causes = sorted({p["kind"] for p in points})
    x["early_margin_min"], x["late_margin_min"] = {"unknown": 0}, {"unknown": [x["max_margin_min"]] * 3}
    x["extra_late_min"] = {k: 0 for k in x["extra_late_min"]}
    for kind in causes:
        mine = [p for p in points if p["kind"] == kind]
        # early side: cover the stops that ended early (before the end last announced) as reliably as the rest
        early_ends = [p for p in mine if p["real"] < p["st"].program.announced_end]
        x["early_margin_min"][kind] = next(m for m in range(0, x["calibration"]["max_early_margin_min"] + 5, 5) if not early_ends
                                           or m >= x["calibration"]["max_early_margin_min"] or sum(
            1 for p in early_ends if p["real"] >= p["st"].program.announced_end - timedelta(minutes=m)) >= target * len(early_ends))
        late = [x["max_margin_min"]] * 3
        x["late_margin_min"][kind] = late
        for b, label in enumerate(("0", "1", "2+")):
            sub = [p for p in mine if p["groups"]["changes"] == label]
            for m in grid:
                late[b] = m
                if not sub or cov(sub) >= target:
                    break
        for b in (1, 2):                                   # never a narrower range after more changes
            late[b] = max(late[b], late[b - 1])
    x["early_margin_min"]["unknown"] = max(v for k, v in x["early_margin_min"].items() if k != "unknown")
    x["late_margin_min"]["unknown"] = [max(v[b] for k, v in x["late_margin_min"].items() if k != "unknown") for b in range(3)]
    tests = {"evening": lambda p: A.end_features(p["st"], p["kind"], s)["time"] == "evening",
             "growing": lambda p: A.end_features(p["st"], p["kind"], s)["growing"],
             "messy_faa": lambda p: A.end_features(p["st"], p["kind"], s)["messy_faa"]}
    for extra, test in tests.items():
        sub = [p for p in points if test(p)]
        for m in grid:
            x["extra_late_min"][extra] = m
            if not sub or cov(sub) >= target:
                break
    x["confidence_by_group"] = confidence_table(evaluate(points, s), s)
    return s


def confidence_table(rows, s) -> dict:
    """Group key -> [coverage rounded down to 0.05, ticks]. The agent looks up the most specific group with enough ticks."""
    out = {}
    for r in rows:
        for key in A.group_keys(A.end_features(r["st"], r["kind"], s)):
            n, k = out.get(key, (0, 0))
            out[key] = (n + 1, k + r["inside"])
    conf = {key: [math.floor(k / n * 20) / 20, n] for key, (n, k) in sorted(out.items())}
    for key, v in conf.items():                          # never more sure after more changes than after fewer
        parts = key.split("|")
        if len(parts) > 1 and parts[1] in ("1", "2+"):
            for fewer in (("0",) if parts[1] == "1" else ("0", "1")):
                k2 = "|".join([parts[0], fewer] + parts[2:])
                if k2 in conf and conf[k2][1] >= s["expected_end"]["min_group_ticks"]:
                    v[0] = min(v[0], conf[k2][0])
    return conf


def write_settings(s):
    """Write the chosen expected_end block back into the settings file, keeping its comments above the block."""
    import yaml
    path = A.SETTINGS_FILE
    text = path.read_text(encoding="utf-8")
    x = s["expected_end"]
    for name in ("early_margin_min", "late_margin_min", "extra_late_min"):
        dumped = yaml.safe_dump(x[name], default_flow_style=True, width=200).strip()
        text = re.sub(rf"(\n  {name}: )\{{[^\n]*\}}", lambda m: m.group(1) + dumped, text)
    rows = "".join(f'    "{k}": {v}\n' for k, v in x["confidence_by_group"].items())
    text = re.sub(r"(\n  confidence_by_group:[^\n]*\n)((?:    [^\n]*\n)*)", lambda m: m.group(1) + rows, text)
    path.write_text(text, encoding="utf-8")


def baseline(days, s) -> dict:
    """The most newly delayed flights seen in one rolling window outside any incident, by hour (training days only)."""
    w = timedelta(minutes=s["detect"]["new_delays_window_min"])
    out = {h: 0 for h in range(5, 24)}
    for day in days:
        busy = [(datetime.fromisoformat(i["start"]) - timedelta(hours=1), datetime.fromisoformat(i["end"] or i["start"]) + timedelta(hours=3))
                for i in day["truth"]["incidents"]]
        first = {}
        for e in sorted(day["events"], key=lambda e: e["occurred_at"]):
            if e["type"] in ("flight_delayed", "flight_delay_updated") and isinstance(e["payload"].get("flight_id"), str):
                first.setdefault(e["payload"]["flight_id"], e["occurred_at"])
        ts = sorted(t for t in first.values() if not any(a <= t <= b for a, b in busy))
        for t in ts:
            n = sum(1 for y in ts if t - w < y <= t)
            h = t.astimezone(TZ).hour
            out[h] = max(out.get(h, 0), n)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", help="measure on these seeds instead of the training days (e.g. 5000-5059)")
    ap.add_argument("--search", action="store_true", help="choose margins and confidences on the training days only")
    ap.add_argument("--write", action="store_true", help="write the chosen values to config/agent_settings/situation.yaml")
    ap.add_argument("--baseline", action="store_true", help="the normal level of newly delayed flights by hour (training days)")
    args = ap.parse_args(argv)
    from backend.seed.build import build
    data = build()
    A.load_settings.cache_clear()
    base = A.load_settings()
    if args.baseline:
        print("most newly delayed flights in one window outside incidents, by hour (training days 1-100):")
        print(baseline(_days(TRAIN, data), base))
        return
    if args.search:
        if args.seeds:
            ap.error("--search only ever uses the training days")
        points = samples(_days(TRAIN, data), base)
        print(show(table(evaluate(points, base)), "BEFORE (training days 1-100)"))
        s = search(points, base)
        print(show(table(evaluate(points, s)), "AFTER (training days 1-100)"))
        x = s["expected_end"]
        print("chosen: early", x["early_margin_min"], "| late", x["late_margin_min"], "| extra", x["extra_late_min"])
        if args.write:
            write_settings(s)
            print(f"written to {A.SETTINGS_FILE}")
        return
    seeds = parse_seeds(args.seeds) if args.seeds else TRAIN
    points = samples(_days(seeds, data), base)
    label = f"seeds {args.seeds}" if args.seeds else "training days 1-100"
    print(show(table(evaluate(points, base)), f"Calibration by group, {label} (one row per 5-minute tick during an FAA program)"))


if __name__ == "__main__":
    main()
