"""Run one simulated day.

    python -m simulator.run --seed 7 --scenario random --mode today --until 23:59

Resets the database (scripts.reset_db), plays the day minute by minute, saves everything in batches and
prints a short summary. The simulator never decides anything: it only creates the world and its problems.
"""
import argparse
import contextlib
import io
import random
import time
from dataclasses import dataclass

from backend.seed.build import build
from simulator.channels import Channels
from simulator.clock import SimClock, fmt, hhmm, minute_of
from simulator.config import load_config
from simulator.metrics import Recorder
from simulator.scenario import DEFAULT_SCENARIO, SCENARIOS, build_scenario
from simulator.world import World


class NotBuiltYet(NotImplementedError):
    """Raised for modes that come in a later phase."""


@dataclass
class Result:
    run_id: str
    seed: int
    scenario: object
    world: World
    channels: Channels
    metrics: dict
    until: int


def run_id_for(seed: int, scenario: str, mode: str) -> str:
    return f"{scenario}-s{seed}-{mode}"


def check_mode(mode: str):
    if mode == "copilot":
        raise NotBuiltYet("--mode copilot is not built yet (Phase 4+). Only --mode today exists in Phase 3.")
    if mode != "today":
        raise ValueError(f"Unknown mode '{mode}'. Use 'today' (or 'copilot' once Phase 4+ is built).")


def simulate(seed: int, scenario: str = DEFAULT_SCENARIO, mode: str = "today", until: int | None = None,
             data: dict | None = None, writer=None, on_flush=None) -> Result:
    """Play the day. With a writer, results are saved in batches; without one it all stays in memory."""
    check_mode(mode)
    cfg = load_config()
    data = data if data is not None else build()
    start = hhmm(cfg["day"]["start"])
    until = hhmm(cfg["day"]["end"]) if until is None else until
    flights = [{"id": f["id"], "direction": f["direction"], "sched_dep": minute_of(f["sched_dep"]),
                "sched_arr": minute_of(f["sched_arr"]), "aircraft_id": f["aircraft_id"], "crew_id": f["crew_id"]}
               for f in data["flights"]]
    sc = build_scenario(seed, scenario, flights)
    world = World(data, sc, cfg, start)
    world.check_story()
    channels = Channels(cfg, random.Random(f"channels:{scenario}:{seed}"), world.truth, start)
    recorder = Recorder(cfg, sc, start)
    every = cfg["day"]["write_every_min"]

    for t in SimClock(start, until).ticks():
        world.step(t)
        channels.observe(t, world.collect_truth_changes())
        channels.apply_due(t)
        for e in channels.drain_events():
            world.events.append({"t": e["t"], "type": e["type"], "actor": "simulator", "actor_name": e["actor_name"],
                                 "station": None, "payload": e["payload"], "incident": False})
        channels.refresh()
        recorder.tick(t, world, channels)
        if writer and ((t - start + 1) % every == 0 or t == until):
            writer.flush(world, channels)
            if on_flush:
                on_flush(writer.conn)

    metrics = recorder.finish(world, channels, until)
    rid = run_id_for(seed, scenario, mode)
    if writer:
        writer.finish(sc, metrics, mode, until)
    return Result(rid, seed, sc, world, channels, metrics, until)


def run(seed: int, scenario: str = DEFAULT_SCENARIO, mode: str = "today", until: str | int = "23:59",
        write: bool = True, on_flush=None) -> Result:
    """Reset the database, play the day and save it."""
    check_mode(mode)                                   # fail before touching the database
    until_min = hhmm(until) if isinstance(until, str) else until
    if not write:
        return simulate(seed, scenario, mode, until_min)
    from backend.database import connect
    from scripts.reset_db import main as reset_db
    from simulator.store import Writer
    with contextlib.redirect_stdout(io.StringIO()):
        reset_db()
    with connect() as conn:
        return simulate(seed, scenario, mode, until_min, writer=Writer(conn, run_id_for(seed, scenario, mode), seed),
                        on_flush=on_flush)


def summary(res: Result, seconds: float | None = None) -> str:
    m, sc = res.metrics, res.scenario
    agree = m["minutes_until_all_channels_agree_after_stop"]
    lines = [
        f"Run {res.run_id}" + (f"  ({seconds:.1f} s)" if seconds is not None else ""),
        f"  Disruption : {sc.incident['title']} - {sc.program.replace('_', ' ')} at MCI {fmt(sc.start)} to {fmt(sc.end)}"
        f" (first announced to end {fmt(sc.announced_end)})",
        f"  Flights    : {m['flights_total']} total | {m['flights_delayed']} delayed (avg {m['avg_delay_min']} min, "
        f"max {m['max_delay_min']}) | {m['flights_cancelled']} cancelled | {m['flights_diverted']} diverted",
        f"  Passengers : {m['passengers_disrupted']} disrupted | {m['passengers_rebooked']} rebooked | "
        f"{m['tracked_passengers_stranded']} of {m['tracked_passengers']} tracked with no seat found",
        f"  Tarmac     : longest wait {m['max_tarmac_min']} min | peak {m['peak_people_in_terminal']} people in the terminal",
        f"  Special    : {m['wheelchair_passengers_disrupted']} wheelchair, {m['child_alone_passengers_disrupted']} children alone disrupted",
        f"  Screens    : up to {m['peak_passengers_disagreeing']} of {m['tracked_passengers']} tracked passengers saw wrong info "
        f"(avg {m['avg_passengers_disagreeing']}); {m['passenger_minutes_of_disagreement']} passenger-minutes wrong",
        "  Recovery   : " + (f"all screens (except lost messages) first agreed {agree} min after the stop ended"
                             if agree is not None else "screens still wrong at the end of the run")
        + f"; {m['lost_messages_still_wrong_at_end']} lost message(s) still wrong",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Play one simulated day at MCI and save it to the database.")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scenario", choices=SCENARIOS, default=DEFAULT_SCENARIO)
    ap.add_argument("--mode", default="today", help="today (no co-pilot). copilot comes in Phase 4+.")
    ap.add_argument("--until", default=load_config()["day"]["end"], help="last minute to simulate, HH:MM")
    args = ap.parse_args(argv)
    try:
        check_mode(args.mode)
    except (NotBuiltYet, ValueError) as e:
        raise SystemExit(str(e))
    started = time.perf_counter()
    res = run(args.seed, args.scenario, args.mode, args.until)
    print(summary(res, time.perf_counter() - started))
    print(f"\nSaved to the database. Make the debug page with:  python -m simulator.report --run-id {res.run_id}")


if __name__ == "__main__":
    main()
