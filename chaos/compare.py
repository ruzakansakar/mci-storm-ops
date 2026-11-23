"""Paired in-memory evaluation, with frozen settings and recorded fresh seeds."""
import argparse
import json
import random
from statistics import mean
import time
import yaml

from backend.seed.build import build
from backend.settings import ROOT
from simulator.run import simulate
from simulator.comparison import truth_fingerprint
from simulator.comparison_page import section, esc


def compare(seed, data, chaos=False):
    kwargs = {}
    scenario = "a31_storm"
    if chaos:
        from chaos.generator import plan_day, scenario_for, overlay_events
        from chaos.memory_replay import late_deliveries
        from backend.contracts.feeds import Feeds
        from simulator.clock import at
        plan = plan_day(seed)
        scenario = "chaos"
        outages = {}
        for outage in plan.feed_outages:
            outages.setdefault(outage["feed"], []).append((at(outage["start"]), at(outage["end"])))
        for p in (plan.program, plan.second):
            if p and p["kind"] == "it_outage":
                outages.setdefault("flight_ops", []).append((at(p["start"]), at(p["end"])))
        base = simulate(seed, scenario, data=data, prebuilt=scenario_for(plan))
        overlay = overlay_events(plan, base.world.events, base.world.flights)
        late, _ = late_deliveries(plan, base.world.events)
        def event_key(e):
            return (e["t"], e["type"], json.dumps(e["payload"], sort_keys=True))
        delayed = {event_key(base.world.events[i]): minute for i, minute in late.items()}
        kwargs = {"prebuilt": scenario_for(plan), "feeds": Feeds(outages=outages), "source_overlay": overlay,
                  "late_delivery": lambda e: delayed.get(event_key(e), e.get("published_t"))}
    today = simulate(seed, scenario, data=data, **kwargs)
    copilot = simulate(seed, scenario, mode="copilot", data=data, **kwargs)
    identical = truth_fingerprint(today) == truth_fingerprint(copilot)
    if not identical:
        raise AssertionError(f"Operational truth changed on seed {seed}")
    a, b = today.metrics["passenger_minutes_of_disagreement"], copilot.metrics["passenger_minutes_of_disagreement"]
    return {"seed": seed, "chaos": chaos, "truth_identical": identical, "today": today.metrics, "copilot": copilot.metrics,
            "improvement_pct": (a - b) * 100 / a if a else 0, "saved_passenger_minutes": a-b}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", help="comma-separated evaluation seeds; omitted means fresh random seeds")
    ap.add_argument("--no-index", action="store_true", help="do not rebuild the chaos index for these days")
    args = ap.parse_args(argv)
    cfg = yaml.safe_load((ROOT / "config" / "comparison.yaml").read_text())
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else random.SystemRandom().sample(
        range(cfg["seed_min"], cfg["seed_max"]), cfg["random_days"])
    out = ROOT / "reports" / "chaos"
    out.mkdir(parents=True, exist_ok=True)
    (out / "comparison-seeds.json").write_text(json.dumps(seeds), encoding="utf-8")
    data = build()
    results = []
    for seed, chaos in [(7, False)] + [(seed, True) for seed in seeds]:
        started = time.perf_counter()
        row = compare(seed, data, chaos)
        results.append(row)
        (out / f"comparison-{seed}.html").write_text('<!doctype html><meta charset="utf-8"><title>Channel comparison</title>'
            '<style>body{font-family:Segoe UI,sans-serif;max-width:1400px;margin:30px auto}table{border-collapse:collapse;width:100%}'
            'td,th{padding:7px;border-bottom:1px solid #ddd;text-align:left}svg{width:100%}</style>'
            + f'<h1>Day {seed}</h1>' + section(row["today"], row["copilot"]), encoding="utf-8")
        print(f"{seed}: {row['improvement_pct']:.1f}% fewer passenger-minutes wrong; truth identical; {time.perf_counter()-started:.1f}s", flush=True)
        (out / "comparison.json").write_text(json.dumps(results), encoding="utf-8")
    random_rows = results[1:]
    rates = [r["improvement_pct"] for r in random_rows if r["today"]["passenger_minutes_of_disagreement"]]
    summary = {"days": len(random_rows), "days_with_wrong_screens": len(rates),
               "average_pct": mean(rates) if rates else None, "best_pct": max(rates, default=None),
               "worst_pct": min(rates, default=None), "worse_seeds": [r["seed"] for r in random_rows if r["improvement_pct"] < 0],
               "truth_identical_days": sum(r["truth_identical"] for r in results), "seeds": seeds}
    (out / "comparison-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_page(results, summary)
    print(json.dumps(summary), flush=True)
    if seeds and not args.no_index:                   # the chaos index for the same days: Situation verdicts + this comparison
        from chaos.memory_replay import play_day, score as situation_score
        from chaos.pages import write_day_page, write_index
        sit = []
        for seed in seeds:
            sc = situation_score(play_day(seed, data))
            write_day_page(None, sc)
            sit.append(sc)
        print("index:", write_index(sit, title="Chaos days: Situation agent, and today vs with co-pilot (played in memory)"))


COLS = [("passenger_minutes_of_disagreement", "Passenger-minutes wrong"), ("critical_passenger_minutes_wrong", "Critical passenger-minutes"),
        ("peak_passengers_disagreeing", "Peak passengers wrong"), ("lost_updates_wrong_end", "Lost updates still wrong at day end"),
        ("catch_up_median_min", "Median catch-up (min)"), ("catch_up_worst_min", "Worst catch-up (min)"),
        ("pushes_per_passenger_max", "Most extra pushes to one passenger")]


def write_page(results, summary):
    out = ROOT / "reports" / "chaos"
    head = "".join(f"<th>{esc(label)}<br>today &rarr; co-pilot</th>" for _, label in COLS)
    rows = ""
    for r in results:
        cells = "".join(f"<td>{esc(r['today'].get(k))} &rarr; {esc(r['copilot'].get(k))}</td>" for k, _ in COLS)
        name = "seed 7 (a31_storm story)" if not r["chaos"] else f"chaos {r['seed']}"
        better = (f"{r['improvement_pct']:.0f}% fewer" if r["today"]["passenger_minutes_of_disagreement"]
                  else "nobody's screen was wrong")
        rows += (f'<tr><td><a href="comparison-{r["seed"]}.html">{esc(name)}</a></td><td>{better}</td>{cells}'
                 f'<td>{"identical" if r["truth_identical"] else "DIFFERENT"}</td></tr>')
    avg = "-" if summary["average_pct"] is None else f"{summary['average_pct']:.0f}%"
    page = ('<!doctype html><meta charset="utf-8"><title>Today vs with co-pilot</title><style>body{font-family:Segoe UI,Arial,'
            'sans-serif;margin:24px;color:#222}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid '
            '#e4e4e4;padding:6px 8px;text-align:left;vertical-align:top}th{background:#fafafa}</style>'
            '<h1>Today vs with co-pilot</h1><p>Each day is played twice with the same seed: the same storm, flights, cancellations '
            'and rebookings. Only the screens differ: with co-pilot the Communication agent re-sends the confirmed trip to wrong '
            'screens, through the same slow and unreliable channels.</p>'
            f'<p><b>{summary["days"]} fresh random days</b> ({summary["days_with_wrong_screens"]} had a wrong screen): average '
            f'{avg} fewer passenger-minutes on a wrong screen, best '
            f'{"-" if summary["best_pct"] is None else format(summary["best_pct"], ".0f") + "%"}, worst '
            f'{"-" if summary["worst_pct"] is None else format(summary["worst_pct"], ".0f") + "%"}. Days where co-pilot was worse: '
            f'{", ".join(map(str, summary["worse_seeds"])) or "none"}. World truth identical on {summary["truth_identical_days"]}'
            f' of {len(results)} days.</p><table><tr><th>Day</th><th>Improvement</th>{head}<th>World truth</th></tr>{rows}</table>')
    (out / "comparison.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
