"""Score the Network Recovery agent (Phase 4d, advisory) against what today's process actually did, in memory.

Seed 7 plus a few fresh random chaos days (recorded in reports/chaos/network-seeds.json). Per day:
  - crew warnings: minutes ahead of each real crew time-out (a crew replaced by a reserve, or a flight cancelled for its crew)
  - illegal proposals: proposals the rule check blocked (must be 0)
  - hold/divert warnings: minutes ahead of each real diversion
  - an ESTIMATE of passengers stranded overnight and cancellations, the plan vs today's, by the same method (the plan is not
    applied, so this cannot be measured; see backend/agents/network_recovery/plan.py estimate_stranded)

  python -m chaos.judge_network            # seed 7 + 5 fresh random days
  python -m chaos.judge_network --seeds 123,456
"""
import argparse
import json
import random
from statistics import median

import yaml

from backend.seed.build import build
from backend.settings import ROOT

OUT = ROOT / "reports" / "chaos"


def score_day(seed, data, chaos):
    from chaos.compare import compare
    row = compare(seed, data, chaos)
    n = row["copilot"]["network"]
    leads = [c["lead_min"] for c in n["crew_timeouts"]]
    fuel = [f["lead_min"] for f in n["fuel_diversions"]]
    warn_min = yaml.safe_load((ROOT / "config" / "agent_settings" / "network_recovery.yaml").read_text())["crew"]["warn_before_min"]
    return {"seed": seed, "chaos": chaos, "truth_identical": row["truth_identical"],
            "crew_timeouts": len(leads), "crew_warned": sum(1 for x in leads if x is not None),
            "crew_warned_60": sum(1 for x in leads if x is not None and x >= warn_min),
            "crew_lead_median": median([x for x in leads if x is not None]) if any(x is not None for x in leads) else None,
            "crew_lead_min": min([x for x in leads if x is not None], default=None),
            "diversions": len(fuel), "fuel_warned": sum(1 for x in fuel if x is not None),
            "fuel_lead_median": median([x for x in fuel if x is not None]) if any(x is not None for x in fuel) else None,
            "proposals": n["proposals"], "illegal_proposals": n["illegal_proposals"], "errors": n["errors"],
            "today_cancelled": len(n["today"]["cancelled"]), "today_stranded_est": n["today"]["estimate"]["passengers_stranded_overnight"],
            "today_stranded_actual": n["today"]["actual_passengers_stranded_overnight"],
            "plan_cancelled": len(n["plan"]["cancel"]), "plan_stranded_est": n["plan"]["estimate"]["passengers_stranded_overnight"],
            "crew_saves": n["plan"]["crew_saves"], "detail": n}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", help="comma-separated chaos seeds; omitted = 5 fresh random ones")
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args(argv)
    seeds = [int(x) for x in args.seeds.split(",")] if args.seeds else \
        random.SystemRandom().sample(range(10_000_000, 99_999_999), args.days)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "network-seeds.json").write_text(json.dumps(seeds), encoding="utf-8")
    data = build()
    rows = []
    for seed, chaos in [(7, False)] + [(s, True) for s in seeds]:
        r = score_day(seed, data, chaos)
        rows.append(r)
        print(f"{'seed 7' if not chaos else seed}: crew time-outs {r['crew_timeouts']} warned {r['crew_warned']} (>=60 min "
              f"{r['crew_warned_60']}), lead median {r['crew_lead_median']} | diversions {r['diversions']} warned {r['fuel_warned']}, "
              f"lead median {r['fuel_lead_median']} | illegal {r['illegal_proposals']}/{r['proposals']} | cancels today "
              f"{r['today_cancelled']} plan {r['plan_cancelled']} | stranded est today {r['today_stranded_est']} plan "
              f"{r['plan_stranded_est']} | truth identical {r['truth_identical']}", flush=True)
    (OUT / "network.json").write_text(json.dumps(rows, default=str), encoding="utf-8")
    body = "".join(
        f"<tr><td>{'seed 7' if not r['chaos'] else r['seed']}</td><td>{r['crew_warned']}/{r['crew_timeouts']} ({r['crew_warned_60']} by 60+ min)"
        f"</td><td>{r['crew_lead_median']}</td><td>{r['fuel_warned']}/{r['diversions']}</td><td>{r['fuel_lead_median']}</td>"
        f"<td>{r['illegal_proposals']} of {r['proposals']}</td><td>{r['today_cancelled']} / {r['plan_cancelled']}</td>"
        f"<td>{r['today_stranded_est']} / {r['plan_stranded_est']}</td></tr>" for r in rows)
    page = ('<!doctype html><meta charset="utf-8"><title>Network Recovery scores</title><style>body{font-family:Segoe UI,Arial,'
            'sans-serif;margin:24px}table{border-collapse:collapse}td,th{border-bottom:1px solid #ddd;padding:6px 10px;text-align:left}'
            '</style><h1>Network Recovery (advisory) vs today\'s process</h1><p>The plan is not applied, so cancellations and '
            'stranded passengers for the plan are ESTIMATES (same method for both columns).</p><table><tr><th>Day</th>'
            '<th>Crew time-outs warned</th><th>Median minutes ahead</th><th>Diversions warned</th><th>Median minutes ahead</th>'
            '<th>Illegal proposals</th><th>Cancelled: today / plan</th><th>Stranded (est.): today / plan</th></tr>' + body + '</table>')
    (OUT / "network.html").write_text(page, encoding="utf-8")
    print("page:", OUT / "network.html")


if __name__ == "__main__":
    main()
