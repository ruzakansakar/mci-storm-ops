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
    copilot: object = None             # co-pilot mode: the agents' loop (their shared store), for tests and pages


def run_id_for(seed: int, scenario: str, mode: str) -> str:
    return f"{scenario}-s{seed}-{mode}"


def check_mode(mode: str):
    if mode not in ("today", "copilot", "orchestrated"):
        raise ValueError(f"Unknown mode '{mode}'. Use 'today', 'copilot' or 'orchestrated'.")


def simulate(seed: int, scenario: str = DEFAULT_SCENARIO, mode: str = "today", until: int | None = None,
             data: dict | None = None, writer=None, on_flush=None, prebuilt=None, run_id: str | None = None,
             on_tick=None, feeds=None, source_overlay=(), late_delivery=None, copilot_options=None) -> Result:
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
    from backend.screens import screens_for
    screens = {p["id"]: screens_for(p["contact_channels"]) for p in data["passengers"]}
    channels = Channels(cfg, random.Random(f"channels:{scenario}:{seed}"), world.truth, start, screens)
    recorder = Recorder(cfg, sc, start)
    from backend.agents.communication.detect import load_settings
    from simulator.comparison import ComparisonRecorder, focus_passenger
    from simulator.clock import at
    comparison = ComparisonRecorder(load_settings(), focus_passenger(data))
    copilot = None
    event_cursor = 0
    if mode in ("copilot", "orchestrated"):
        from backend.orchestrator.loop import Orchestrator as Copilot     # Phase 5a: the orchestrator owns the minute loop
        schedule = [{k: f[k] for k in ("id", "direction", "origin", "destination", "sched_dep", "sched_arr")}
                    for f in data["flights"]]
        passengers = [{"id": p["id"], "segment": p["segment"], "full_name": p["full_name"],
                       "channel_types": [c for c in p["contact_channels"] if c in ("app", "sms", "email")]}
                      for p in data["passengers"]]      # full_name: AgentContext gives it only for passengers being worked on
        situation_schedule = [{k: f.get(k) for k in ("id", "direction", "origin", "destination", "sched_dep", "sched_arr", "crew_id")}
                              for f in data["flights"]]
        from backend.seed.build import AS_OF, SOR
        system_health = [{"system": s_, "status": "up", "last_heartbeat": AS_OF, "note": "simulated stand-in for the official system"}
                         for s_ in sorted(set(SOR.values()))]                      # as backend/seed/load.py seeds it
        from backend.execution.simulated_human import SimulatedHumans
        humans = SimulatedHumans(random.Random(f"humans:{scenario}:{seed}"), lambda *a: _window(world, *a))
        network_rows = {                                   # the plan published before the day (start-of-day values only)
            "flights": [{k: f.get(k) for k in ("id", "direction", "origin", "destination", "sched_dep", "sched_arr", "aircraft_id",
                                               "crew_id", "priority", "pax_total")} for f in data["flights"]],
            "aircraft": [{k: a.get(k) for k in ("id", "type", "seats", "maint_due_at", "needs_inspection")} for a in data["aircraft"]],
            # as backend/seed/load.py loads the roster: every crew starts on duty, reserves as reserve; nobody reported fatigue
            "crews": [{**c, "availability": c.get("availability") or ("reserve" if c.get("is_reserve") else "on_duty"),
                       "fatigue_reported": bool(c.get("fatigue_reported"))} for c in data["crews"]],
            "stations": [dict(x) for x in data["stations"]],
            "later_capacity": [dict(x) for x in data["later_capacity"]], "route_tiers": [dict(x) for x in data["route_tiers"]],
            "passenger_groups": [{k: g[k] for k in ("flight_id", "segment", "count")} for g in data["passenger_groups"]]}
        care_rows = {                                      # published before the day; names only for passengers it works on
            "flights": [{k: f.get(k) for k in ("id", "direction", "origin", "destination", "sched_dep", "sched_arr", "priority")}
                        for f in data["flights"]],
            "passengers": [{k: p.get(k) for k in ("id", "full_name", "booking_ref", "segment", "final_destination", "lives_locally",
                                                  "loyalty_tier", "contact_channels")} for p in data["passengers"]],
            "hotels": [dict(h) for h in data["hotels"]],
            "transport_options": [{"name": t["name"], "capacity": t["capacity"], "hourly_rate_usd": t["hourly"],
                                   "per_trip_rate_usd": t["per_trip"]} for t in data["transport"]],
            "hotel_rate_tiers": [{"tier": t["tier"], "radius_miles": t["radius"], "hotel_count": t["count"], "rate_usd": t["rate"]}
                                 for t in data["hotel_tiers"]],
            "later_capacity": [dict(x) for x in data["later_capacity"]]}
        station_rows = {                                   # published before the day: schedule, roster, station facts, counts
            "flights": [{k: f.get(k) for k in ("id", "direction", "origin", "destination", "sched_dep", "sched_arr", "aircraft_id",
                                               "pax_total")} for f in data["flights"]],
            "staff": [{k: x.get(k) for k in ("id", "role", "station", "shift_start", "shift_end", "last_break")} for x in data["staff"]],
            "stations": [{k: x.get(k) for k in ("code", "name", "region", "gates", "wheelchair_staff", "has_customs", "terminal_capacity")}
                         for x in data["stations"]],
            "passenger_groups": [{k: g[k] for k in ("flight_id", "segment", "count")} for g in data["passenger_groups"]]}
        copilot = Copilot(at(start), schedule, passengers, feeds, situation_schedule, system_health,
                          stations=[{"code": x["code"], "name": x["name"]} for x in data["stations"]], humans=humans,
                          network_rows=network_rows, care_rows=care_rows, station_rows=station_rows, **(copilot_options or {}),
                          **({"apply_port": world.apply_decision, "pending_port": world.set_pending} if mode == "orchestrated" else {}))
        world.orchestrated = mode == "orchestrated"     # approved co-pilot decisions replace today's process decisions
        world.publish_seats = True                      # fix-seats: the reservations system's seat inventory (agents read it)
        from backend.contracts import load_contracts
        agent_types = {t for a in ("situation", "network_recovery", "passenger_care", "station_staff", "communication")
                       for t in load_contracts()[a].event_types}
        channels.fix_rng = random.Random(f"corrections:{scenario}:{seed}")
        channels.msg_rng = random.Random(f"messages:{scenario}:{seed}")
    every = cfg["day"]["write_every_min"]

    # The reservation system publishes each tracked passenger's trip record: once at the start of the day, then every time it
    # changes. This is the truth feed the channels should follow (a real passenger service system publishes the same).
    for pid in sorted(world.truth):
        world.events.append(_trip_record(start, pid, world.truth[pid]))
    if world.publish_seats:
        world.t = start
        world.publish_all_seats()
    for t in SimClock(start, until).ticks():
        world.step(t)
        for e in source_overlay:
            if e["t"] == t:
                world.events.append({"t": t, "type": e["type"], "actor": "simulator", "actor_name": e["feed"],
                                     "station": e.get("station"), "payload": e["payload"], "incident": False,
                                     "published_t": e.get("late_t"), "overlay": True})
        changes = world.collect_truth_changes()
        for pid, _, new, _ in changes:
            world.events.append(_trip_record(t, pid, new))
        channels.observe(t, changes)
        channels.apply_due(t)
        if copilot:
            channels.apply_messages(t)
        for e in channels.drain_events():
            world.events.append({"t": e["t"], "type": e["type"], "actor": "simulator", "actor_name": e["actor_name"],
                                 "station": None, "payload": e["payload"], "incident": False})
        channels.refresh()
        if copilot:
            published = [{"occurred_at": at(e["t"]), "type": e["type"], "station": e.get("station"),
                          "published_at": at(delivered) if (delivered := (late_delivery(e) if late_delivery else e.get("published_t"))) is not None else None,
                          "payload": e["payload"]} for e in world.events[event_cursor:]
                         if e["type"] in agent_types and e.get("actor") == "simulator"]
            # each agent's context cuts these to its own contract. Agents' outputs, people's answers and the care records
            # reach the shared store directly when they happen (so nothing is counted twice).
            event_cursor = len(world.events)
            channels.recovery_guard = copilot.recovery_delivery_problems
            outputs, audits = copilot.tick(at(t), published, lambda p: channels.deliver_messages(t, p) if p.get("messages")
                                           else channels.deliver_fix(t, p))
            # Recovery deliveries wait for this minute's published facts and approval versions before their final check.
            channels.apply_messages(t, recovery_only=True)
            from backend.execution.copilot import serial
            for agent, kind, payload in outputs:
                world.events.append({"t": t, "type": kind,
                                     "actor": "simulated_human" if agent.startswith("simulated_human") else
                                     "system" if agent.startswith("system:") else "agent", "actor_name": agent,
                                     "station": None, "payload": serial(payload), "incident": False})
            for a in audits:
                world.audit_rows.append({"t": t, "actor": "agent", "actor_name": a["agent"], "what": a["what"],
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
    if copilot:
        from simulator.network_metrics import network_metrics
        metrics["network"] = network_metrics(world, copilot, data)
        from simulator.care_metrics import care_metrics
        from simulator.comparison import focus_passenger
        metrics["care"] = care_metrics(world, copilot, data, focus_passenger(data))
        metrics["recovery_messages"] = copilot.recovery.history
        from simulator.station_metrics import station_metrics
        from backend.execution.copilot import serial as _serial
        metrics["station"] = _serial(station_metrics(world, copilot, data))
        from simulator.orchestrator_metrics import orchestrator_metrics
        metrics["orchestrator"] = _serial(orchestrator_metrics(copilot))
    from simulator.comparison import focus_passenger as _focus
    from simulator.three_ways import mode_summary
    from backend.execution.copilot import serial as _ser
    metrics["three_ways"] = _ser(mode_summary(world, metrics, copilot, _focus(data)))    # 5b: today / copilot / orchestrated
    metrics["mode"] = mode
    rid = run_id or run_id_for(seed, scenario, mode)
    result = Result(rid, seed, sc, world, channels, metrics, until, copilot)
    from simulator.comparison import truth_fingerprint
    metrics["truth_sha256"] = truth_fingerprint(result)
    metrics["contract_violations"] = (len(copilot.context.violations) + len(copilot.situation.context.violations)) if copilot else 0
    if writer:
        if copilot:
            imap = writer.save_incidents(copilot) if hasattr(copilot, "incidents") else {}
            writer.save_proposals(copilot.context, imap)
            writer.save_proposals(copilot.situation.context, imap)
            if copilot.network:
                writer.save_proposals(copilot.network.context, imap)
            if copilot.care:
                writer.save_proposals(copilot.care.context, imap)
                writer.save_hotel_ledger(copilot.care.ledger)
            if copilot.station:
                writer.save_proposals(copilot.station.context, imap)
            if hasattr(copilot, "incidents"):
                writer.save_proposals(copilot.octx, imap)
                writer.save_orchestrator(copilot, imap)
                writer.save_station_ledgers(copilot.station.gate_ledger, copilot.station.staff_ledger)
            writer.save_shared(copilot.store)
        writer.finish(sc, metrics, mode, until)
    return result


def _window(world, key: str, sides: list, flight_ids: list, now) -> str:
    """What a simulated person sees when they look out of the window: the side of a conflict that matches the real truth.
    Only the simulated people (backend/execution/simulated_human.py) get this; no agent does."""
    from simulator.clock import at
    kind = key.split(":")[0]
    if kind == "departed-during-stop" and flight_ids:
        f = world.flights.get(flight_ids[0]) or {}
        reported = next((e["t"] for e in world.events if e["type"] == "flight_departed" and e.get("overlay")
                         and (e["payload"] or {}).get("flight_id") == flight_ids[0]), None)
        real = f.get("actual_dep") is not None and (reported is None or f["actual_dep"] <= reported + 2)
        return sides[0] if real else sides[1]
    if kind == "faa-notices":
        real = [e for e in world.events if e["type"].startswith("ground_") and not e.get("overlay") and at(e["t"]) <= now
                and (e["payload"] or {}).get("expected_end")]
        end = real[-1]["payload"]["expected_end"] if real else None
        return next((x for x in sides if end and x.endswith(end)), sides[0])
    if kind == "holding-after-end":
        return sides[1]                                  # the flights really were still in the air
    if kind == "health-up-feed-stopped":
        it_down = any(e["type"] == "system_status_changed" and (e["payload"] or {}).get("status") != "up" and not e.get("overlay")
                      and at(e["t"]) <= now for e in world.events)
        return sides[1] if it_down else sides[0]
    return sides[0]


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
        if mode in ("copilot", "orchestrated"):
            if not conn.execute("SELECT 1 FROM metric_runs WHERE run_id=%s", (run_id_for(seed, scenario, "today"),)).fetchone():
                raise ValueError("Run the matching today mode first so the modes can be compared.")
            if conn.execute("SELECT 1 FROM events WHERE run_id=%s LIMIT 1", (run_id_for(seed, scenario, mode),)).fetchone():
                raise ValueError(f"This {mode} run already exists; rerun today first to rebuild the modes cleanly.")
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
    ap.add_argument("--mode", default="today", choices=("today", "copilot", "orchestrated"),
                    help="today; copilot (advice, nothing changes the world); orchestrated (approved decisions change the world)")
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
