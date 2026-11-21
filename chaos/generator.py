"""The chaos generator: random worst-case days at MCI, built from random ingredients. Same chaos_seed = same day.

generate(chaos_seed) plays the day through the existing simulator (extended with new real-source notices) and returns:
  (a) the events the world sees: the simulator's own events plus "overlay" events that real sources would publish
      (FAA advisories, airport ops notices, police/TSA notices, system monitor changes) and messy data;
  (b) a SEALED TRUTH (what really happened) that only the judge reads. It is written to chaos/truth/<run_id>.json (gitignored).

This is test harness code. The agent knows nothing about it.
"""
import json
import random
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from backend.settings import ROOT
from simulator.clock import at, hhmm
from simulator.config import load_config
from simulator.scenario import Scenario, _event, _flight_params

CHAOS_FILE = ROOT / "config" / "chaos.yaml"
TRUTH_DIR = ROOT / "chaos" / "truth"
CONTROLLABLE = {"weather": False, "faa_program": False, "airport": False, "security": False, "it_outage": True, "crew": True}


@lru_cache
def load_chaos() -> dict:
    return yaml.safe_load(CHAOS_FILE.read_text(encoding="utf-8"))


def run_id_for(chaos_seed: int) -> str:
    return f"chaos-{chaos_seed}"


@dataclass
class Plan:
    seed: int
    ingredients: list
    program: dict | None = None            # {kind, program, cause, reason, start, end, notices:[(t, type, expected_end)], ...}
    second: dict | None = None             # a second incident without an FAA program
    crew_wave: dict | None = None
    feed_outages: list = field(default_factory=list)      # [{feed, start, end, why}]
    conflicts: list = field(default_factory=list)          # [{kind, ...}]
    scenario_events: list = field(default_factory=list)    # events the simulator world reacts to
    overlay: list = field(default_factory=list)            # events only the feeds publish (the world doesn't react)
    messy: dict = field(default_factory=dict)
    normal_variation: list = field(default_factory=list)


def _rng(seed):
    return random.Random(f"chaos:{seed}")


def _between(rng, window):
    lo, hi = (hhmm(x) if isinstance(x, str) else x for x in window)
    return rng.randint(lo, hi)


def _pick_ingredients(rng, c) -> list:
    names = sorted(c["ingredients"])
    w = [c["ingredients"][n] for n in names]
    if rng.random() < c["combos"]["storm_plus_messy"]:
        return ["thunderstorm", "messy_data"]
    if rng.random() < c["combos"]["two_incidents"]:
        p = rng.choice(c["primary"])
        s = rng.choice([x for x in c["second_incident"] if x != p])
        return [p, f"second:{s}"]
    counts = sorted(c["ingredients_per_day"])
    n = rng.choices(counts, weights=[c["ingredients_per_day"][k] for k in counts])[0]
    picked = []
    while len(picked) < n:
        x = rng.choices(names, weights=w)[0]
        if x not in picked:
            picked.append(x)
    if "quiet" in picked:
        return ["quiet"]
    prim = [x for x in picked if x in c["primary"]]
    for extra in prim[1:]:                       # only one FAA program per day; another primary becomes a second incident
        picked.remove(extra)
        if extra in c["second_incident"] and not any(x.startswith("second:") for x in picked):
            picked.append(f"second:{extra}")
    return picked


def plan_day(chaos_seed: int) -> Plan:
    """The day's ingredients and timings (no simulator yet)."""
    c = load_chaos()
    rng = _rng(chaos_seed)
    plan = Plan(chaos_seed, _pick_ingredients(rng, c))
    ing = plan.ingredients
    if "timing_extreme" in ing and not any(x in c["primary"] for x in ing):
        ing.insert(0, "thunderstorm")
    prim = next((x for x in ing if x in c["primary"]), None)
    if prim:
        plan.program = _program(rng, c, prim, extreme="timing_extreme" in ing)
    sec = next((x.split(":", 1)[1] for x in ing if x.startswith("second:")), None)
    if sec == "crew_wave" and plan.program:
        plan.crew_wave = _crew_wave_plan(rng, c, plan.program["start"], plan.program["end"] - 10)
    elif sec:
        plan.second = _second(rng, c, sec, plan.program)
    if plan.crew_wave is None and (sec == "crew_wave" or "crew_wave" in ing):
        plan.crew_wave = _crew_wave_plan(rng, c)
    if "feed_outage" in ing:
        f = c["feed_outage"]
        length = rng.randint(*f["length_min"])
        if plan.program:
            s = rng.randint(plan.program["start"] + 5, max(plan.program["start"] + 6, plan.program["end"] - 5))
        else:
            s = _between(rng, ["07:00", "21:00"])
        plan.feed_outages.append({"feed": rng.choice(f["feeds"]), "start": s, "end": s + length, "why": "feed outage"})
    if "conflict" in ing:
        _conflict(rng, c, plan)
    if "normal_variation" in ing:
        plan.normal_variation = [{"t": None}] * rng.randint(*c["normal_variation"]["delays"])
    if "messy_data" in ing:
        m = c["messy_data"]
        plan.messy = {"duplicates": rng.randint(*m["duplicates"]), "late": rng.randint(*m["late"]),
                      "malformed": rng.randint(*m["malformed"]), "unknown": rng.randint(*m["unknown"])}
    plan.rng_state = rng.getstate()
    return plan


def _program(rng, c, kind, extreme=False) -> dict:
    if kind == "thunderstorm":
        t = c["thunderstorm"]
        start = _between(rng, t["start_window"])
        first = rng.randint(*t["first_length_min"])
        if extreme:
            if rng.random() < 0.5:
                start = hhmm(c["day"]["start"])
            else:
                start = hhmm(c["timing_extreme"]["late_start"])
                first = rng.randint(*c["timing_extreme"]["late_length_min"])
        p = {"kind": kind, "program": "ground_stop", "cause": "weather", "reason": "thunderstorms", "start": start}
        notices = [(start, "ground_stop_started", start + first)]
        announced = start + first
        exts = rng.choices(sorted(t["extensions_weights"]), weights=[t["extensions_weights"][k] for k in sorted(t["extensions_weights"])])[0]
        for _ in range(exts):
            new = announced + rng.randint(*t["extension_min"])
            if new - start > t["max_length_min"]:
                break
            notices.append((announced - 5, "ground_stop_extended", new))
            announced = new
        if rng.random() < t["shorten_chance"] and announced - start > 60:
            new = announced - rng.randint(*t["shorten_min"])
            notices.append((max(notices[-1][0] + 5, new - 30), "ground_stop_extended", new))
            announced = new
        end = announced
        if rng.random() < t["end_early_chance"]:
            end = max(notices[-1][0] + 5, announced - rng.randint(*t["end_early_min"]))
        p.update(announced_first=start + first, end=end, notices=notices, ramp=None)
        if rng.random() < t["ramp_closure_chance"] and end - start > 30:
            r0 = rng.randint(start + 5, max(start + 6, end - 20))
            r1 = min(end, r0 + rng.randint(*t["ramp_closure_min"]))
            p["ramp"] = (r0, r1)
        return p
    spec = c[kind]
    start, length = _between(rng, spec["start_window"]), rng.randint(*spec["length_min"])
    if extreme:
        start = hhmm(c["day"]["start"])
    cause, program, reason = {
        "ground_delay_program": ("faa_program", "ground_delay", "volume / staffing"),
        "it_outage": ("it_outage", "ground_stop", "airline request (company systems)"),
        "power_outage": ("airport", "ground_stop", "airport power outage"),
        "runway_closure": ("airport", "ground_delay", "runway closure"),
        "security": ("security", "ground_stop", "security"),
    }[kind]
    return {"kind": kind, "program": program, "cause": cause, "reason": reason, "start": start, "end": start + length,
            "announced_first": start + length, "notices": [(start, f"{program}_started", start + length)], "ramp": None}


def _second(rng, c, kind, program) -> dict:
    spec = c[kind]
    length = rng.randint(*(spec.get("second_length_min") or spec["length_min"]))
    if program:
        s = rng.randint(program["start"], max(program["start"] + 1, program["end"] - 10))
    else:
        s = _between(rng, spec["start_window"])
    cause = {"it_outage": "it_outage", "power_outage": "airport", "security": "security"}[kind]
    return {"kind": kind, "cause": cause, "start": s, "end": s + length}


def _crew_wave_plan(rng, c, lo=None, hi=None) -> dict:
    w = c["crew_wave"]
    s = rng.randint(lo, max(lo + 1, hi)) if lo is not None else _between(rng, w["start_window"])
    return {"start": s, "end": s + rng.randint(*w["window_min"]), "crews": rng.randint(*w["crews"])}


def _conflict(rng, c, plan):
    kinds = list(c["conflict"]["kinds"])
    p = plan.program
    ok = []
    if p and p["program"] == "ground_stop" and p["end"] - p["start"] >= 60:
        ok.append("false_end")
    if p and p.get("ramp") and p["ramp"][0] + 3 < hhmm("23:59"):
        ok.append("departed_while_ramp_closed")
    ok.append("health_up_feed_down")
    kind = rng.choice([k for k in kinds if k in ok])
    if kind == "false_end":
        t = p["end"] - rng.randint(*c["conflict"]["false_end_before_min"])
        plan.conflicts.append({"kind": kind, "t": max(p["start"] + 10, t)})
    elif kind == "departed_while_ramp_closed":
        plan.conflicts.append({"kind": kind, "t": p["ramp"][0] + 3})
    else:
        feed = rng.choice(["flight_ops", "station_ops"])
        s = rng.randint(p["start"] + 5, max(p["start"] + 6, p["end"] - 5)) if p else _between(rng, ["07:00", "21:00"])
        plan.feed_outages.append({"feed": feed, "start": s, "end": s + rng.randint(*c["conflict"]["feed_down_min"]),
                                  "why": "conflict: system health stays up"})
        plan.conflicts.append({"kind": kind, "t": s, "feed": feed})


# ---------------------------------------------------------------- the scenario the simulator plays

def scenario_for(plan: Plan):
    """A function the simulator calls with the day's flights; it returns the Scenario the world plays."""
    def build(flights):
        cfg = load_config()
        rng = random.Random(f"chaos-world:{plan.seed}")
        events = []
        p = plan.program
        if p:
            for t, typ, end in p["notices"]:
                _event(events, t, typ, {"program": p["program"], "expected_end": end, "reason": p["reason"]})
            if p["end"] <= hhmm("23:59"):
                _event(events, p["end"], f"{p['program']}_ended", {"program": p["program"], "reason": p["reason"]})
            if p.get("ramp"):
                _event(events, p["ramp"][0], "ramp_closed", {"reason": "lightning", "expected_end": p["ramp"][1]})
                _event(events, p["ramp"][1], "ramp_reopened", {"reason": "lightning cleared"})
        if plan.crew_wave:
            w = plan.crew_wave
            deps = sorted((f for f in flights if f["direction"] == "departure" and f["crew_id"] and w["start"] <= f["sched_dep"] <= w["end"] + 30),
                          key=lambda f: (f["sched_dep"], f["id"]))
            for f in deps[: w["crews"]]:
                t = max(w["start"], f["sched_dep"] - rng.randint(20, 60))
                _event(events, t, "crew_duty_low", {"crew_id": f["crew_id"], "flight_id": f["id"],
                                                    "minutes_left": rng.randint(*load_chaos()["crew_wave"]["minutes_left"])})
        if p:
            start, end, ann, cause, prog = p["start"], p["end"], p["announced_first"], p["cause"], p["program"]
        else:
            start = end = ann = hhmm("05:00")
            cause, prog = ("crew" if plan.crew_wave else "none"), "none"
        incident = {"kind": cause, "title": "chaos day", "likely_cause": cause, "controllable": CONTROLLABLE.get(cause), "cause": cause}
        params = _flight_params(rng, flights, cfg)
        return Scenario("chaos", plan.seed, cause, prog, start, end, ann, incident, events, params).finish()
    return build


# ---------------------------------------------------------------- what the feeds publish on top of the world

def overlay_events(plan: Plan, world_events: list[dict], flights: dict) -> list[dict]:
    """Events real sources publish that the simulator world doesn't make (notices, messy data, conflicts). Each has a t
    (minute it happened), a feed (actor_name), and optionally late_t (minute it was delivered)."""
    c = load_chaos()
    rng = random.Random(f"chaos-overlay:{plan.seed}")
    out = []

    def add(t, typ, payload, feed, late_t=None, station="MCI"):
        out.append({"t": t, "type": typ, "payload": payload, "feed": feed, "late_t": late_t, "station": station})

    def notices(kind, s, e):
        if kind == "power_outage":
            add(s, "airport_ops_notice", {"kind": "power_outage", "area": "Terminal A gates and jet bridges", "expected_end": e}, "airport_ops")
            add(e, "airport_ops_notice", {"kind": "power_restored", "area": "Terminal A gates and jet bridges"}, "airport_ops")
        elif kind == "runway_closure":
            add(s, "airport_ops_notice", {"kind": "runway_closed", "area": "runway 1L/19R", "expected_end": e}, "airport_ops")
            add(e, "airport_ops_notice", {"kind": "runway_reopened", "area": "runway 1L/19R"}, "airport_ops")
        elif kind == "security":
            add(s, "police_tsa_notice", {"kind": "security_threat", "action": "concourse evacuation", "area": "Terminal A"}, "police_tsa")
            add(e, "police_tsa_notice", {"kind": "all_clear", "area": "Terminal A"}, "police_tsa")
        elif kind == "it_outage":
            add(s, "system_status_changed", {"system": c["systems"]["flight_ops"], "status": "down"}, "system_monitor")
            add(e, "system_status_changed", {"system": c["systems"]["flight_ops"], "status": "up"}, "system_monitor")

    if plan.program:
        notices(plan.program["kind"], plan.program["start"], plan.program["end"])
    if plan.second:
        notices(plan.second["kind"], plan.second["start"], plan.second["end"])
    for cf in plan.conflicts:
        if cf["kind"] == "false_end":
            add(cf["t"], "ground_stop_ended", {"program": "ground_stop", "reason": plan.program["reason"]}, "faa_feed")
        elif cf["kind"] == "departed_while_ramp_closed":
            waiting = [f for f in flights.values() if f["direction"] == "departure" and f["sched_dep"] < cf["t"]
                       and not any(e["type"] == "flight_departed" and e["payload"].get("flight_id") == f["id"] and e["t"] <= cf["t"]
                                   for e in world_events)]
            f = sorted(waiting, key=lambda f: f["id"])[0] if waiting else sorted(flights.values(), key=lambda f: f["id"])[0]
            cf["flight_id"] = f["id"]
            add(cf["t"], "flight_departed", {"flight_id": f["id"], "to": f["destination"], "delay_min": cf["t"] - f["sched_dep"]}, "flight_ops")
    if plan.normal_variation:
        busy = []
        for x in (plan.program, plan.second, plan.crew_wave):
            if x:
                busy.append((x["start"] - 120, x["end"] + 120))
        nv = c["normal_variation"]
        deps = sorted((f for f in flights.values() if f["direction"] == "departure"
                       and not any(a <= f["sched_dep"] <= b for a, b in busy)), key=lambda f: f["sched_dep"])
        last = -10**6
        picked = []
        for f in deps:
            if f["sched_dep"] - last >= nv["spacing_min"] and rng.random() < 0.5:
                picked.append(f)
                last = f["sched_dep"]
            if len(picked) >= len(plan.normal_variation):
                break
        plan.normal_variation = []
        for f in picked:
            d = rng.randint(*nv["delay_min"])
            t = f["sched_dep"] - 15
            add(t, "flight_delayed", {"flight_id": f["id"], "delay_min": d, "new_departure": _clock(f["sched_dep"] + d),
                                      "reason": "late inbound aircraft", "pax": f.get("pax_total")}, "flight_ops")
            plan.normal_variation.append({"t": t, "flight_id": f["id"], "delay_min": d})
    if plan.messy:
        m, cm = plan.messy, c["messy_data"]
        visible = [e for e in world_events if e["type"] in ("flight_delayed", "flight_delay_updated", "flight_cancelled", "flight_holding",
                                                             "flight_diverted", "ground_stop_extended", "ramp_closed", "station_status")]
        for e in rng.sample(visible, min(m["duplicates"], len(visible))):
            add(e["t"], e["type"], dict(e["payload"]), _feed_of(e["type"]), station=e["station"] or "MCI")
        t0 = plan.program["start"] + 10 if plan.program else _between(rng, ["08:00", "20:00"])
        bad = [("flight_delayed", {"flight_id": 404, "delay_min": "lots", "new_departure": "soon"}, "flight_ops"),
               ("flight_cancelled", {"reason": None}, "flight_ops"),
               ("ground_stop_extended", {"program": "ground_stop", "expected_end": "later"}, "faa_feed"),
               ("ramp_closed", {"expected_end": [1, 2]}, "weather_feed")]
        for i in range(m["malformed"]):
            typ, payload, feed = bad[i % len(bad)]
            add(t0 + 3 * i, typ, payload, feed)
        for i in range(m["unknown"]):
            typ = cm["unknown_types"][i % len(cm["unknown_types"])]
            add(t0 + 7 + 4 * i, typ, {"area": "MCI", "severity": "moderate", "note": "advisory issued"},
                "faa_feed" if typ == "volcanic_ash_advisory" else "airport_ops")
    return sorted(out, key=lambda e: (e["t"], e["type"]))


def _feed_of(typ):
    if typ.startswith(("ground_stop", "ground_delay")):
        return "faa_feed"
    if typ.startswith("ramp"):
        return "weather_feed"
    if typ == "station_status":
        return "station_ops"
    return "flight_ops"


def _clock(m):
    d, r = divmod(int(m), 1440)
    return f"{r // 60:02d}:{r % 60:02d}" + (f"+{d}" if d else "")


# ---------------------------------------------------------------- the sealed truth

def truth_for(plan: Plan, world, overlay: list[dict]) -> dict:
    c = load_chaos()
    ev = world.events
    incidents = []

    def iso(m):
        return at(int(m)).isoformat() if m is not None else None
    p = plan.program
    if p:
        incidents.append({"n": 1, "kind": p["kind"], "type": p["cause"], "controllable": CONTROLLABLE[p["cause"]], "start": iso(p["start"]),
                          "end": iso(p["end"]), "first_signal": iso(p["start"]), "has_end": True, "program": p["program"],
                          "announced_first_end": iso(p["announced_first"]), "notices": len(p["notices"]),
                          "ramp": [iso(p["ramp"][0]), iso(p["ramp"][1])] if p.get("ramp") else None,
                          "running_at_day_end": p["end"] > hhmm("23:59")})
    if plan.second:
        s = plan.second
        incidents.append({"n": len(incidents) + 1, "kind": s["kind"], "type": s["cause"], "controllable": CONTROLLABLE[s["cause"]],
                          "start": iso(s["start"]), "end": iso(s["end"]), "first_signal": iso(s["start"]), "has_end": False,
                          "program": None})
    crew_cx = sorted((e["t"], e["payload"]["flight_id"]) for e in ev if e["type"] == "flight_cancelled"
                     and e["payload"].get("reason") == "crew out of legal duty")
    if plan.crew_wave:
        w = plan.crew_wave
        mine = [x for x in crew_cx if w["start"] <= x[0] <= w["end"] + 120]
        need = c["crew_wave"]["incident_if_cancellations"]
        if len(mine) >= need:
            incidents.append({"n": len(incidents) + 1, "kind": "crew_wave", "type": "crew", "controllable": True, "start": iso(w["start"]),
                              "end": iso(mine[-1][0]), "first_signal": iso(mine[need - 1][0]), "has_end": False, "program": None})
    uncontrollable = [i for i in incidents if i["controllable"] is False]
    review = []
    for t, fid in crew_cx:
        for i in uncontrollable:
            if i["start"] <= iso(t) <= (i["end"] or iso(t)):
                review.append(fid)
    cancelled = sorted((e["t"], e["payload"]["flight_id"]) for e in ev if e["type"] == "flight_cancelled")
    stranded = sorted(e["t"] for e in ev if e["type"] == "passenger_stranded_overnight")
    diverted = []
    for e in ev:
        if e["type"] == "flight_diverted":
            back = next((x["t"] for x in ev if x["type"] == "flight_arrived" and x["payload"].get("flight_id") == e["payload"]["flight_id"]
                         and x["t"] > e["t"]), None)
            diverted.append({"flight_id": e["payload"]["flight_id"], "diverted": iso(e["t"]), "back": iso(back)})
    conflicts = []
    silent = [(f["feed"], f["start"], f["end"]) for f in plan.feed_outages]
    if p and p["kind"] == "it_outage":
        silent.append(("flight_ops", p["start"], p["end"]))
    if plan.second and plan.second["kind"] == "it_outage":
        silent.append(("flight_ops", plan.second["start"], plan.second["end"]))
    for cf in plan.conflicts:
        item = {"kind": cf["kind"], "at": iso(cf["t"])}
        if cf["kind"] == "false_end":
            held = {e["payload"]["flight_id"] for e in ev if e["type"] == "flight_holding" and e["t"] <= cf["t"]}
            landed_by = {e["payload"]["flight_id"] for e in ev if e["type"] in ("flight_arrived", "flight_diverted") and e["t"] <= cf["t"] + 20}
            item["detectable"] = bool(held - landed_by)
        else:
            item["detectable"] = True
        # the agent can only see a conflict if the feeds that show it are talking, and before the day ends
        needs = {"false_end": ("flight_ops", "faa_feed"), "departed_while_ramp_closed": ("flight_ops", "weather_feed", "faa_feed")}.get(cf["kind"], ())
        window_end = cf["t"] + (25 if cf["kind"] == "false_end" else 5)
        if any(f in needs and s0 < window_end and e0 > cf["t"] for f, s0, e0 in silent) or window_end > hhmm("23:59"):
            item["detectable"] = False
        if cf.get("feed"):
            item["feed"] = cf["feed"]
        if cf.get("flight_id"):
            item["flight_id"] = cf["flight_id"]
        conflicts.append(item)
    outages = [{"feed": f["feed"], "start": iso(f["start"]), "end": iso(f["end"]), "why": f["why"]} for f in plan.feed_outages]
    if p and p["kind"] == "it_outage":
        outages.append({"feed": "flight_ops", "start": iso(p["start"]), "end": iso(p["end"]), "why": "Southwest IT outage"})
    if plan.second and plan.second["kind"] == "it_outage":
        outages.append({"feed": "flight_ops", "start": iso(plan.second["start"]), "end": iso(plan.second["end"]), "why": "Southwest IT outage"})
    security = [{"at": i["start"]} for i in incidents if i["type"] == "security"]
    truth = {
        "run_id": run_id_for(plan.seed), "chaos_seed": plan.seed, "ingredients": plan.ingredients,
        "quiet": not incidents, "incidents": incidents, "feed_outages": outages, "conflicts": conflicts, "security": security,
        "crew_cancellations_needing_review": sorted(set(review)),
        "aftermath": {"cancelled": [[iso(t), f] for t, f in cancelled], "stranded_overnight_first": iso(stranded[0]) if stranded else None,
                      "diverted": diverted},
        "messy": {**plan.messy, "unknown_types": [e["type"] for e in overlay if e["type"] in c["messy_data"]["unknown_types"]]},
        "normal_variation": [{"at": iso(x["t"]), "flight_id": x["flight_id"]} for x in plan.normal_variation],
    }
    truth["summary"] = describe(truth)
    return truth


def describe(truth: dict) -> str:
    words = {"thunderstorm": "thunderstorm ground stop", "ground_delay_program": "FAA ground delay program",
             "it_outage": "Southwest IT outage", "power_outage": "airport power outage", "runway_closure": "runway closure",
             "security": "security threat (evacuation)", "crew_wave": "wave of crew cancellations"}
    parts = []
    for i in truth["incidents"]:
        t0, t1 = i["start"][11:16], (i["end"] or "")[11:16]
        extra = f", {i['notices'] - 1} change(s) to the end" if i.get("notices", 1) > 1 else ""
        parts.append(f"{words.get(i['kind'], i['kind'])} {t0}-{t1}{extra}")
    if not parts:
        parts.append("normal day with a few scattered delays" if truth["normal_variation"] else "quiet day, nothing happens")
    for f in truth["feed_outages"]:
        if f["why"] != "Southwest IT outage":
            parts.append(f"{f['feed']} silent {f['start'][11:16]}-{f['end'][11:16]}")
    for cf in truth["conflicts"]:
        parts.append(f"conflict: {cf['kind'].replace('_', ' ')} at {cf['at'][11:16]}")
    if truth["messy"].get("duplicates"):
        parts.append("messy data")
    return "; ".join(parts)


def write_truth(truth: dict) -> Path:
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    path = TRUTH_DIR / f"{truth['run_id']}.json"
    path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    return path


def read_truth(run_id: str) -> dict:
    return json.loads((TRUTH_DIR / f"{run_id}.json").read_text(encoding="utf-8"))
