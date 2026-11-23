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
    if mode not in ("today", "copilot"):
        raise ValueError(f"Unknown mode '{mode}'. Use 'today' or 'copilot'.")


def simulate(seed: int, scenario: str = DEFAULT_SCENARIO, mode: str = "today", until: int | None = None,
             data: dict | None = None, writer=None, on_flush=None, prebuilt=None, run_id: str | None = None,
             on_tick=None, feeds=None, source_overlay=(), late_delivery=None) -> Result:
    """Play the day. With a writer, results are saved in batches; without one it all stays in memory.
    on_tick(t, world, channels): called at the end of every simulated minute (test harnesses read the answer key there)."""
    check_mode(mode)
    cfg = load_config()
    data = data if data is not None else build()
    start = hhmm(cfg["day"]["start"])
    until = hhmm(cfg["day"]["end"]) if until is None else until
    flights = [{"id": f["id"], "direction": f["direction"], "sched_dep": minute_of(f["sched_dep"]),
                "sched_arr": minute_of(f["sched_arr"]), "aircraft_id": f["aircraft_id"], "crew_id": f["crew_id"]}
               for f in data["flights"]]
    sc = prebuilt(flights) if prebuilt else build_scenario(seed, scenario, flights)   # prebuilt: a ready-made day (chaos tests)
    world = World(data, sc, cfg, start)
    world.check_story()
    channels = Channels(cfg, random.Random(f"channels:{scenario}:{seed}"), world.truth, start)
    recorder = Recorder(cfg, sc, start)
    from backend.agents.communication.detect import load_settings
    from simulator.comparison import ComparisonRecorder, focus_passenger
    from simulator.clock import at
    comparison = ComparisonRecorder(load_settings(), focus_passenger(data))
    copilot = None
    event_cursor = 0
    if mode == "copilot":
        from backend.execution.copilot import Copilot
        schedule = [{k: f[k] for k in ("id", "direction", "origin", "destination", "sched_dep", "sched_arr")}
                    for f in data["flights"]]
        passengers = [{"id": p["id"], "segment": p["segment"],
                       "channel_types": [c for c in p["contact_channels"] if c in ("app", "sms", "email")]}
                      for p in data["passengers"]]
        copilot = Copilot(at(start), schedule, passengers, feeds)
        channels.fix_rng = random.Random(f"corrections:{scenario}:{seed}")
    every = cfg["day"]["write_every_min"]

    # The reservation system publishes each tracked passenger's trip record: once at the start of the day, then every time it
    # changes. This is the truth feed the channels should follow (a real passenger service system publishes the same).
    for pid in sorted(world.truth):
        world.events.append(_trip_record(start, pid, world.truth[pid]))
    for t in SimClock(start, until).ticks():
        world.step(t)
        for e in source_overlay:
            if e["t"] == t:
                world.events.append({"t": t, "type": e["type"], "actor": "simulator", "actor_name": e["feed"],
                                     "station": e.get("station"), "payload": e["payload"], "incident": False,
                                     "published_t": e.get("late_t")})
        changes = world.collect_truth_changes()
        for pid, _, new, _ in changes:
            world.events.append(_trip_record(t, pid, new))
        channels.observe(t, changes)
        channels.apply_due(t)
        for e in channels.drain_events():
            world.events.append({"t": e["t"], "type": e["type"], "actor": "simulator", "actor_name": e["actor_name"],
                                 "station": None, "payload": e["payload"], "incident": False})
        channels.refresh()
        if copilot:
            published = [{"occurred_at": at(e["t"]), "type": e["type"], "station": e.get("station"),
                          "published_at": at(delivered) if (delivered := (late_delivery(e) if late_delivery else e.get("published_t"))) is not None else None,
                          "payload": e["payload"]} for e in world.events[event_cursor:]
                         if e["type"] in copilot.context.contract.event_types]
            event_cursor = len(world.events)
            outputs, audits = copilot.tick(at(t), published, lambda p: channels.deliver_fix(t, p))
            for kind, payload in outputs:
                world.events.append({"t": t, "type": kind, "actor": "agent", "actor_name": "communication",
                                     "station": None, "payload": payload, "incident": False})
            from backend.execution.copilot import serial
            for a in audits:
                world.audit_rows.append({"t": t, "actor": "agent", "actor_name": "communication", "what": a["what"],
                                         "entity_type": a["entity_type"], "entity_id": a["entity_id"],
                                         "before": serial(a["before"]), "after": serial(a["after"])})
        recorder.tick(t, world, channels)
        comparison.tick(t, world, channels)
        if on_tick:
            on_tick(t, world, channels)
        if writer and ((t - start + 1) % every == 0 or t == until):
            writer.flush(world, channels)
            if on_flush:
                on_flush(writer.conn)

    metrics = recorder.finish(world, channels, until)
    metrics.update(comparison.finish(world, channels, sc.end, copilot))
    rid = run_id or run_id_for(seed, scenario, mode)
    result = Result(rid, seed, sc, world, channels, metrics, until)
    from simulator.comparison import truth_fingerprint
    metrics["truth_sha256"] = truth_fingerprint(result)
    metrics["contract_violations"] = len(copilot.context.violations) if copilot else 0
    if writer:
        if copilot:
            writer.save_proposals(copilot.context)
        writer.finish(sc, metrics, mode, until)
    return result


def _trip_record(t: int, pid: str, truth: dict) -> dict:
    return {"t": t, "type": "trip_record_updated", "actor": "simulator", "actor_name": "reservations", "station": None,
            "payload": {"passenger_id": pid, "itinerary": list(truth["itinerary"]), "status": truth["status"],
                        "destination": truth["destination"], "bag_routing": list(truth["bag_routing"])}, "incident": False}


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
    if mode == "today":
        with contextlib.redirect_stdout(io.StringIO()):
            reset_db()
    with connect() as conn:
        if mode == "copilot":
            if not conn.execute("SELECT 1 FROM metric_runs WHERE run_id=%s", (run_id_for(seed, scenario, "today"),)).fetchone():
                raise ValueError("Run the matching today mode first so both modes can be compared.")
            if conn.execute("SELECT 1 FROM events WHERE run_id=%s LIMIT 1", (run_id_for(seed, scenario, mode),)).fetchone():
                raise ValueError("This copilot run already exists; rerun today first to rebuild both modes cleanly.")
        return simulate(seed, scenario, mode, until_min, writer=Writer(conn, run_id_for(seed, scenario, mode), seed,
                                                                     persist_truth=mode == "today"),
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
        f"{m['passengers_stranded_overnight']} stranded overnight on {m['next_day_flights_created']} next-day flights "
        f"({m['tracked_passengers_overnight']} of {m['tracked_passengers']} tracked, {m['tracked_overnight_who_live_locally']} of them local) | "
        f"{m['tracked_passengers_stranded']} tracked with no seat at all",
        f"  Tarmac     : longest wait {m['max_tarmac_min']} min | peak {m['peak_people_in_terminal']} people in the terminal",
        f"  Special    : {m['wheelchair_passengers_disrupted']} wheelchair, {m['child_alone_passengers_disrupted']} children alone disrupted",
        f"  Crews      : {m['crews_illegal_rest']} flying with rest below the legal minimum (flagged at 05:00) | "
        f"{m['crews_short_for_plan']} short only for our plan (not illegal) | {m['pilot_duty_extensions']} pilot duty extension(s) "
        f"(decided by the captain and crew scheduling)",
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
    ap.add_argument("--mode", default="today", choices=("today", "copilot"), help="today or automatic confirmed screen corrections")
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
