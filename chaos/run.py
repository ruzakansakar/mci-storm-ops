"""Run chaos days: random worst-case days the Situation agent has never seen, scored by the judge.

  python -m chaos.run --surprise            one brand-new random day (chaos seed from the clock)
  python -m chaos.run --seed 17             replay exactly chaos day 17 (to reproduce a failure)
  python -m chaos.run --days 20             20 new random days, then the summary page
  python -m chaos.run --days 20 --fresh     reset the database first

Chaos runs use run ids "chaos-<seed>" and never touch the a31_storm run. Pages: reports/chaos/index.html and one per day.
This is test harness code: the agent is run exactly as in production (backend/agents/situation/run.py) and knows nothing of this.
"""
import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from psycopg.types.json import Jsonb

from backend.agents.situation.run import replay
from backend.contracts.feeds import Feeds
from backend.database import connect
from chaos.generator import TRUTH_DIR, load_chaos, overlay_events, plan_day, run_id_for, scenario_for, truth_for, write_truth
from simulator.clock import at
from simulator.store import Writer

LATE_TYPES = {"flight_delayed", "flight_delay_updated", "flight_cancelled", "flight_holding", "flight_diverted", "station_status"}
SECONDS_PER_DAY = 70          # rough time per day on Neon, for the estimate (assumption, measured)


class ChaosWriter(Writer):
    """Delivers a few of the world's events late (messy data), without changing what happened or when."""

    def __init__(self, conn, run_id, seed, plan):
        super().__init__(conn, run_id, seed)
        self.left = plan.messy.get("late", 0)
        self.after = plan.program["start"] if plan.program else 8 * 60
        self.rng = random.Random(f"chaos-late:{plan.seed}")
        self.late = []

    def flush(self, world, channels):
        """Only the events are saved. Chaos days never write the shared truth tables (flights, bookings, ...), so they can't
        touch the a31_storm run or each other; the agent doesn't read those tables anyway. Screen-copy events are left out."""
        ev, _ = world.drain()
        world.new_flights = []
        world.dirty.clear()
        channels.touched = set()
        ev = [e for e in ev if e["type"] not in ("channel_view_updated", "channel_update_failed")]
        with self.conn.cursor() as cur:
            ids = []
            if ev:
                cur.execute("SELECT nextval(pg_get_serial_sequence('events', 'id')) FROM generate_series(1, %s)", (len(ev),))
                ids = [r[0] for r in cur.fetchall()]
            rows = []
            for e, eid in zip(ev, ids):
                if e["incident"]:
                    self.incident_event_ids.append(eid)
                rows.append((eid, at(e["t"]), e["type"], e["actor"], e["actor_name"], e["station"], Jsonb(e["payload"]), self.seed,
                             self.run_id, self.published_at(e)))
            if rows:
                cur.executemany("INSERT INTO events (id, occurred_at, type, actor, actor_name, station, payload, seed, run_id, "
                                "published_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        self.conn.commit()

    def published_at(self, e):
        if self.left and e["type"] in LATE_TYPES and e["t"] >= self.after and self.rng.random() < 0.25:
            self.left -= 1
            delay = self.rng.randint(*load_chaos()["messy_data"]["late_by_min"])
            self.late.append({"type": e["type"], "at": at(e["t"]).isoformat(), "delivered": at(e["t"] + delay).isoformat()})
            return at(e["t"] + delay)
        return None


def _exists(conn, run_id) -> bool:
    return conn.execute("SELECT 1 FROM metric_runs WHERE run_id=%s LIMIT 1", (run_id,)).fetchone() is not None


def make_day(conn, seed: int, data: dict) -> dict:
    """Play the chaos day into the database (once) and write its sealed truth file."""
    from simulator.run import simulate
    run_id = run_id_for(seed)
    plan = plan_day(seed)
    path = TRUTH_DIR / f"{run_id}.json"
    if _exists(conn, run_id) and path.exists():          # the same seed is the same day: reuse it (events are append-only)
        old = json.loads(path.read_text(encoding="utf-8"))
        res = simulate(seed, "chaos", data=data, prebuilt=scenario_for(plan), run_id=run_id)     # same day, in memory only
        truth = truth_for(plan, res.world, overlay_events(plan, res.world.events, res.world.flights))
        truth["messy"]["late_events"] = old["messy"].get("late_events", [])
        write_truth(truth)                                 # the truth is recomputed by the same rules (in case they improved)
        return truth
    writer = ChaosWriter(conn, run_id, seed, plan)
    res = simulate(seed, "chaos", data=data, writer=writer, prebuilt=scenario_for(plan), run_id=run_id)
    flights = res.world.flights
    over = overlay_events(plan, res.world.events, flights)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO events (occurred_at, type, actor, actor_name, station, payload, run_id, published_at) "
            "VALUES (%s, %s, 'source', %s, %s, %s, %s, %s)",
            [(at(e["t"]), e["type"], e["feed"], e["station"], Jsonb(e["payload"]), run_id,
              at(e["late_t"]) if e.get("late_t") is not None else None) for e in over])
    conn.commit()
    truth = truth_for(plan, res.world, over)
    truth["messy"]["late_events"] = writer.late
    write_truth(truth)
    return truth


def feeds_for(truth: dict) -> Feeds:
    from datetime import datetime
    out = {}
    for f in truth["feed_outages"]:
        out.setdefault(f["feed"], []).append((datetime.fromisoformat(f["start"]), datetime.fromisoformat(f["end"])))
    return Feeds(outages=out)


def run_day(seed: int, data: dict) -> dict:
    """Make the day, run the agent on it (as of each moment, as in production), and save the timings for the judge."""
    with connect() as conn:
        truth = make_day(conn, seed, data)
        conn.commit()
        conn.autocommit = True
        result = replay(conn, truth["run_id"], feeds=feeds_for(truth))
    timing = {"agent_run_id": result["agent_run_id"], "assessments": result["assessments"],
              "errors": [[t.isoformat(), e] for t, e in result["errors"]],
              "seconds": [[t.isoformat(), round(s, 3)] for t, s in result["timings"]]}
    (TRUTH_DIR / f"{truth['run_id']}.timing.json").write_text(json.dumps(timing), encoding="utf-8")
    return {"truth": truth, "result": result}


def _said(result) -> str:
    from backend.agents.situation.run import timeline
    return timeline(result)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chaos testing for the Situation agent")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--surprise", action="store_true", help="one brand-new random day")
    g.add_argument("--seed", type=int, help="replay exactly this chaos day")
    g.add_argument("--days", type=int, help="this many new random days, then the summary page")
    ap.add_argument("--fresh", action="store_true", help="reset the database first")
    ap.add_argument("--first-seed", type=int, help="with --days: use seeds first-seed, first-seed+1, ... instead of new random ones")
    ap.add_argument("--workers", type=int, default=4, help="days played at the same time (each has its own database connection)")
    args = ap.parse_args(argv)

    from backend.seed.build import build
    from chaos.judge import judge_day
    from chaos.pages import write_day_page, write_index
    if args.fresh:
        from scripts.reset_db import main as reset_db
        reset_db()
    data = build()
    if args.days:
        base = args.first_seed if args.first_seed is not None else 1000 + int(time.time()) % 1_000_000 * 10
        seeds = [base + i for i in range(args.days)]
        est = SECONDS_PER_DAY * args.days / max(1, args.workers)
        print(f"Playing {args.days} chaos days with {args.workers} at a time: about {est / 60:.0f} min on Neon.")
        print("Chaos seeds: " + " ".join(str(s) for s in seeds))
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for seed, out in zip(seeds, pool.map(lambda s: run_day(s, data), seeds)):
                print(f"  chaos-{seed} played: {out['truth']['summary']}")
        scores = []
        with connect() as conn:
            for seed in seeds:
                sc = judge_day(conn, run_id_for(seed))
                write_day_page(conn, sc)
                scores.append(sc)
                print(f"  chaos-{seed}: {sc['verdict']:4s}  {', '.join(c['name'] for c in sc['checks'] if c['result'] == 'FAIL') or '-'}")
            path = write_index(scores)
        print(f"Done in {(time.perf_counter() - t0) / 60:.1f} min. Summary page: {path}")
        return
    seed = args.seed if args.seed is not None else int(time.time() * 1000) % 1_000_000_000
    print(f"Chaos day {seed} (replay it with: python -m chaos.run --seed {seed})\n")
    out = run_day(seed, data)
    print("WHAT THE AGENT SAID")
    print(_said(out["result"]))
    truth = out["truth"]
    print("\nWHAT REALLY HAPPENED")
    print("  " + truth["summary"])
    for i in truth["incidents"]:
        print(f"  - {i['kind']}: {i['type']}, controllable {i['controllable']}, {i['start'][11:16]}-{(i['end'] or '')[11:16]}")
    with connect() as conn:
        sc = judge_day(conn, truth["run_id"])
        page = write_day_page(conn, sc)
    print(f"\nSCORE: {sc['verdict']}")
    for c in sc["checks"]:
        print(f"  {c['result']:4s} {c['name']}: {c['reason']}")
    print(f"\nPage: {page}")


if __name__ == "__main__":
    main()
