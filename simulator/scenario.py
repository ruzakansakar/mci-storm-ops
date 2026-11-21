"""Scenarios: given a seed, the list of things that will happen to MCI today.

Two scenarios:
  random    picks a cause, timings and side events from config/simulator.yaml
  a31_storm the fixed class story (thunderstorm ground stop, lightning ramp closure, A31 low on fuel)

A scenario is only a schedule of events plus a few per-flight facts (spare fuel, diversion airport).
It decides nothing about what the airline does about them.
"""
import random
from dataclasses import dataclass, field

from simulator.clock import hhmm
from simulator.config import load_config

SCENARIOS = ("random", "a31_storm")
DEFAULT_SCENARIO = "random"


@dataclass
class Scenario:
    name: str
    seed: int
    cause: str
    program: str                   # ground_stop | ground_delay
    start: int                     # minute the restriction starts
    end: int                       # minute it really ends
    announced_end: int             # the end first announced (before any extension)
    incident: dict                 # kind, title, likely_cause, controllable
    events: list[dict] = field(default_factory=list)          # sorted by time
    flight_params: dict = field(default_factory=dict)         # flight id -> {fuel_minutes_extra, alternate, alternate_extra_min}

    def events_at(self, minute: int) -> list[dict]:
        return self._by_time.get(minute, [])

    def __post_init__(self):
        self._by_time = {}

    def finish(self):
        self.events.sort(key=lambda e: (e["t"], e["order"]))
        self._by_time = {}
        for e in self.events:
            self._by_time.setdefault(e["t"], []).append(e)
        return self


def _event(events, t, type_, payload, incident=True, station="MCI"):
    events.append({"t": int(t), "type": type_, "station": station, "payload": payload,
                   "incident": incident, "order": len(events)})


def _flight_params(rng, flights, cfg) -> dict:
    """Spare fuel and diversion airport for every inbound flight (drawn in a fixed order)."""
    lo, hi = cfg["physics"]["fuel_extra_min"]
    alts = cfg["physics"]["inbound_alternates"]            # airport -> extra minutes of flying to get there
    out = {}
    for f in sorted(flights, key=lambda f: f["id"]):
        if f["direction"] == "arrival":
            fuel, alt = rng.randint(lo, hi), rng.choice(list(alts))
            out[f["id"]] = {"fuel_minutes_extra": fuel, "alternate": alt, "alternate_extra_min": alts[alt]}
    return out


def _restriction_events(events, program, start, announced_end, end, reason):
    """The FAA notices: what a real ATCSCC advisory publishes (program, announced end, stated reason). Nothing else."""
    _event(events, start, f"{program}_started", {"program": program, "expected_end": announced_end, "reason": reason})
    if end != announced_end:
        _event(events, announced_end, f"{program}_extended", {"program": program, "expected_end": end, "reason": reason})
    _event(events, end, f"{program}_ended", {"program": program, "reason": reason})


def build_scenario(seed: int, name: str, flights: list[dict]) -> Scenario:
    """flights: dicts with id, direction, sched_dep, sched_arr (minutes), aircraft_id, crew_id."""
    cfg = load_config()
    if name == "a31_storm":
        return _a31_storm(seed, flights, cfg)
    if name == "random":
        return _random(seed, flights, cfg)
    raise ValueError(f"Unknown scenario '{name}'. Choose one of: {', '.join(SCENARIOS)}")


# ---------------------------------------------------------------- the class story

def _a31_storm(seed, flights, cfg) -> Scenario:
    s = cfg["scenarios"]["a31_storm"]
    rng = random.Random(f"scenario:a31_storm:{seed}")
    params = _flight_params(rng, flights, cfg)
    params[s["flight"]] = {"fuel_minutes_extra": s["fuel_minutes_extra"], "alternate": s["alternate"],
                           "alternate_extra_min": s["alternate_extra_min"]}
    start, announced, end = hhmm(s["stop_start"]), hhmm(s["announced_end"]), hhmm(s["extended_end"])
    cause = cfg["causes"][s["cause"]]
    events: list[dict] = []
    _restriction_events(events, s["program"], start, announced, end, cfg["causes"][s["cause"]]["faa_notice_reason"])
    r0, r1 = hhmm(s["lightning_closure"]["start"]), hhmm(s["lightning_closure"]["end"])
    _event(events, r0, "ramp_closed", {"reason": "lightning", "expected_end": r1})
    _event(events, r1, "ramp_reopened", {"reason": "lightning cleared"})
    return Scenario("a31_storm", seed, s["cause"], s["program"], start, end, announced,
                    _incident(s["cause"], cause), events, params).finish()


# ---------------------------------------------------------------- the random day

def _incident(cause_name, cause) -> dict:
    return {"kind": cause["incident_kind"], "title": cause["title"], "likely_cause": cause["likely_cause"],
            "controllable": cause["controllable"], "cause": cause_name}


def _random(seed, flights, cfg) -> Scenario:
    rng = random.Random(f"scenario:random:{seed}")
    names = sorted(cfg["causes"])
    cause_name = rng.choices(names, weights=[cfg["causes"][n]["weight"] for n in names])[0]
    cause = cfg["causes"][cause_name]
    programs = sorted(cause["programs"])
    program = rng.choices(programs, weights=[cause["programs"][p] for p in programs])[0]

    lo, hi = (hhmm(x) for x in cause["start_window"])
    start = rng.randint(lo, hi)
    total = rng.randint(*cause["duration_min"])
    end = start + total
    announced = end
    if rng.random() < cause["extension_chance"]:
        extra = rng.randint(*cause["extension_min"])
        if total - extra >= cfg["scenario"]["min_stop_before_extension_min"]:
            announced = end - extra

    events: list[dict] = []
    _restriction_events(events, program, start, announced, end, cause["faa_notice_reason"])
    se = cfg["side_events"]

    # lightning closes the ramp (thunderstorms only)
    ls = se["lightning_ramp_closure"]
    if cause_name in ls["applies_to"] and rng.random() < ls["chance"]:
        r0 = start + rng.randint(*ls["start_offset_min"])
        r1 = r0 + rng.randint(*ls["duration_min"])
        _event(events, r0, "ramp_closed", {"reason": "lightning", "expected_end": r1})
        _event(events, r1, "ramp_reopened", {"reason": "lightning cleared"})

    deps = sorted((f for f in flights if f["direction"] == "departure"), key=lambda f: (f["sched_dep"], f["id"]))

    # a crew is close to its duty limit
    ce = se["crew_out_of_duty"]
    if rng.random() < ce["chance"]:
        t = start + rng.randint(*ce["start_offset_min"])
        pool = [f for f in deps if f["crew_id"] and t <= f["sched_dep"] <= end]
        if pool:
            f = rng.choice(pool)
            _event(events, t, "crew_duty_low", {"crew_id": f["crew_id"], "flight_id": f["id"],
                                                 "minutes_left": rng.randint(*ce["minutes_left"])})

    # an aircraft needs an inspection
    ie = se["aircraft_inspection"]
    if rng.random() < ie["chance"]:
        t = start + rng.randint(*ie["start_offset_min"])
        inbound_arr = {f["aircraft_id"]: f["sched_arr"] for f in flights if f["direction"] == "arrival"}
        pool = [f for f in deps if t < f["sched_dep"] <= end + ie["window_after_stop_min"] and inbound_arr.get(f["aircraft_id"], -1) < t]
        if pool:
            f = rng.choice(pool)
            _event(events, t, "aircraft_inspection_needed",
                   {"aircraft_id": f["aircraft_id"], "flight_id": f["id"],
                    "duration_min": rng.randint(*ie["duration_min"])})

    # the terminal fills up
    te = se["terminal_crowd_growth"]
    if rng.random() < te["chance"]:
        t = start + rng.randint(*te["start_offset_min"])
        _event(events, t, "terminal_crowd_growth",
               {"extra_people": rng.randint(*te["extra_people"]), "ramp_up_min": rng.randint(*te["ramp_up_min"]),
                "decay_min": te["decay_min"]})

    params = _flight_params(rng, flights, cfg)
    return Scenario("random", seed, cause_name, program, start, end, announced,
                    _incident(cause_name, cause), events, params).finish()
