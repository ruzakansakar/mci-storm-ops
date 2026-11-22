"""The Communication judge: how well the agent detects channel mismatches, scored against the simulator's answer key.

The simulator knows exactly which screens were wrong, from when to when, and why (simulator/channels.py). Only this judge reads
that; the agent never can. Days are played in memory: the simulator day (seed 7's a31_storm, or a chaos day), the real agent
(backend/agents/communication/detect.py) through MemoryContext, which makes the same cuts as AgentContext (events delivered by
now, only its feeds and the payload fields its contract lists, nothing from a feed while it is silent).

  python -m chaos.judge_communication                     # seed 7 + 20 random chaos days, summary page reports/chaos/communication.html
  python -m chaos.judge_communication --seed7-only

Measures: recall, precision, detection delay, cause accuracy (label only), critical cases flagged critical, no mismatch called on a
stale feed, zero reads of names or contacts.
"""
import argparse
import html
import json
import random
from collections import Counter
from datetime import datetime, timedelta

from backend.agents.communication.detect import Memory, load_settings, scan
from backend.contracts import load_contracts
from backend.contracts.feeds import Feeds
from backend.contracts.loader import PII_COLUMNS
from backend.settings import ROOT
from simulator.clock import at

OUT = ROOT / "reports" / "chaos"
CAUSE_LABEL = {"waiting_for_update": "sync_delay", "stuck": "stale_copy", "failed_delivery": "message_failed",
               "bag_system": "bag_system", "seat_hold": "seat_hold", "unknown": None}
TARGETS = {"recall": 0.98, "precision": 0.98, "delay_min": 1, "critical": 1.0}
VULNERABLE = ("child_alone", "wheelchair")
RANDOM_DAYS = 20
FIRST_RANDOM_SEED = 8405000           # chaos seeds 8405000-8405019: never used anywhere before


# ---------------------------------------------------------------- the answer key (judge only)

class AnswerKey:
    """Every minute: which (passenger, channel, field) are really wrong, the simulator's own cause, and whether it is critical."""

    def __init__(self, world_passengers):
        self.open, self.periods = {}, []
        self.segment = {pid: p["segment"] for pid, p in world_passengers.items()}

    def tick(self, t, world, channels):
        now = set(channels.dis)
        for key in list(self.open):
            if key not in now:
                p = self.open.pop(key)
                p["end"] = t
                self.periods.append(p)
        for key, cause in channels.dis.items():
            p = self.open.setdefault(key, {"key": key, "start": t, "end": None, "critical": False})
            p["cause"] = cause
            if not p["critical"] and self._critical(t, key, world, channels):
                p["critical"] = True

    def _critical(self, t, key, world, channels):
        pid, ch, f = key
        if f == "bag_routing":
            return False
        shown, truth = channels.shown[(pid, ch)], channels.truth[pid]
        if shown["status"] != "cancelled" and (truth["status"] == "cancelled" or any(
                world.flights.get(x, {}).get("status") == "cancelled" for x in shown["itinerary"])):
            return True
        if self.segment.get(pid) in VULNERABLE:
            for x in list(truth["itinerary"]) + list(shown["itinerary"]):
                fl = world.flights.get(x)
                if fl and fl["status"] not in ("departed", "arrived", "cancelled"):
                    return fl["est_dep"] - t <= 120
        return False

    def finish(self):
        for p in self.open.values():
            self.periods.append(p)
        self.open = {}
        return self.periods


# ---------------------------------------------------------------- the day, and the agent's view of it

def play(seed: int, data: dict, chaos: bool) -> dict:
    from simulator.run import simulate
    key = {}

    def on_tick(t, world, channels):
        if "k" not in key:
            key["k"] = AnswerKey(world.passengers)
        key["k"].tick(t, world, channels)
    if chaos:
        from chaos.generator import plan_day, scenario_for
        res = simulate(seed, "chaos", data=data, prebuilt=scenario_for(plan_day(seed)), run_id=f"chaos-{seed}", on_tick=on_tick)
    else:
        res = simulate(seed, "a31_storm", data=data, on_tick=on_tick)
    contract = load_contracts()["communication"]
    events = []
    for n, e in enumerate(res.world.events):
        if e["type"] not in contract.event_types:
            continue
        events.append({"id": n + 1, "occurred_at": at(e["t"]), "published_at": None, "type": e["type"], "station": e.get("station"),
                       "payload": dict(e["payload"] or {})})
    schedule = [{"id": f["id"], "direction": f["direction"], "origin": f["origin"], "destination": f["destination"],
                 "sched_dep": at(f["sched_dep"]), "sched_arr": at(f["sched_arr"])} for f in res.world.flights.values()
                if not f.get("_next_day")]
    segments = [{"id": pid, "segment": p["segment"]} for pid, p in res.world.passengers.items()]
    return {"seed": seed, "chaos": chaos, "events": events, "schedule": schedule, "passengers": segments,
            "answer": key["k"].finish(), "metrics": res.metrics}


class MemoryContext:
    """Stands in for AgentContext for the communication agent: the same cuts, and it keeps what the agent writes."""

    def __init__(self, day, now, feeds, sink):
        self.day, self.now, self.feeds, self.sink = day, now, feeds, sink
        self.contract = load_contracts()["communication"]
        self.run_id = "memory"

    def _feed(self, e):
        feed, fields = self.contract.event_types[e["type"]]
        if feed in self.contract.feed_split:
            name, names = self.contract.feed_split[feed]
            feed = e["payload"].get(name)
            if feed not in names:
                return None, None
        return feed, fields

    def read_events(self, since=None, delivered_since=None):
        out = []
        for e in self.day["events"]:
            delivered = e["published_at"] or e["occurred_at"]
            if delivered > self.now or (since and e["occurred_at"] <= since) or (delivered_since and delivered <= delivered_since):
                continue
            feed, fields = self._feed(e)
            if feed is None:
                continue
            silent = self.feeds.window(feed, self.now)
            if silent and e["occurred_at"] >= silent[0]:
                continue
            out.append({"id": e["id"], "occurred_at": e["occurred_at"], "type": e["type"], "station": e["station"], "feed": feed,
                        "payload": {k: v for k, v in e["payload"].items() if k in fields}})
        return sorted(out, key=lambda e: e["occurred_at"])

    def read(self, table, columns=None, **_):
        allowed = self.contract.reads.get(table)
        cols = columns or allowed
        bad = [c for c in cols if c not in (allowed or [])]
        pii = [c for c in cols if c in PII_COLUMNS.get(table, ())]
        if bad or pii:
            self.sink["forbidden_reads"].append((table, bad + pii))
            raise PermissionError(f"{table}.{bad + pii}")
        rows = self.day["schedule"] if table == "flights" else self.day["passengers"] if table == "passengers" else None
        if rows is None:
            self.sink["forbidden_reads"].append((table, cols))
            raise PermissionError(table)
        return [{c: r.get(c) for c in cols} for r in rows]

    def feed_last(self, feed):
        return self.feeds.last_heartbeat(feed, self.now)

    def feed_age_sec(self, feed):
        return (self.now - self.feed_last(feed)).total_seconds()

    def write(self, table, values, mode="insert", row_id=None):
        rows = self.sink["mismatches"]
        if mode == "insert":
            rid = len(rows) + 1
            rows[rid] = {**values, "id": rid, "severities": [values["severity"]], "causes": [values["cause"]]}
            return rid
        r = rows[row_id]
        r.update(values)
        if "severity" in values:
            r["severities"].append(values["severity"])
        if "cause" in values:
            r["causes"].append(values["cause"])
        return row_id

    def write_many(self, table, rows, mode="insert", row_ids=None):
        if mode == "insert":
            return [self.write(table, r) for r in rows]
        return [self.write(table, r, mode="update", row_id=i) for r, i in zip(rows, row_ids)]

    def record_events(self, items):
        for t, p in items:
            self.record_event(t, p)

    def record_event(self, type_, payload=None, station=None):
        self.sink["events"].append({"at": self.now, "type": type_, "payload": payload})

    def propose(self, action, output, **_):
        self.sink["recs"].append({"at": self.now, "action": action, "title": output["title"], "decision_ids": output["decision_ids"]})
        return len(self.sink["recs"])


def replay(day: dict, feeds: Feeds | None = None, full_every: bool = False) -> dict:
    s = load_settings()
    feeds = feeds or Feeds()
    sink = {"mismatches": {}, "events": [], "recs": [], "forbidden_reads": [], "errors": []}
    memory = Memory()
    t, end = at(5 * 60), at(23 * 60 + 59)
    while t <= end:
        try:
            scan(MemoryContext(day, t, feeds, sink), memory, settings=s, full=full_every)
        except PermissionError:
            raise
        except Exception as err:                     # same as production: record it and carry on (nothing partial is kept)
            sink["errors"].append((t, f"{type(err).__name__}: {err}"))
        t += timedelta(minutes=1)
    sink["memory"] = memory
    return sink


# ---------------------------------------------------------------- scoring

def judge(day: dict, sink: dict, feeds: Feeds) -> dict:
    real = [p for p in day["answer"]]
    found = list(sink["mismatches"].values())
    by_key = {}
    for r in found:
        by_key.setdefault((r["passenger_id"], r["channel"], r["field"]), []).append(r)
    end_of_day = at(23 * 60 + 59)

    def overlaps(r, p):
        a0, a1 = r["since"], r.get("cleared_at") or end_of_day + timedelta(minutes=1)
        b0, b1 = at(p["start"]), at(p["end"]) if p["end"] is not None else end_of_day + timedelta(minutes=1)
        return a0 < b1 and b0 < a1
    matched_rows, delays, cause_ok, cause_n, crit_ok, crit_n, misses, wrong_cause = set(), [], 0, 0, 0, 0, [], Counter()
    for p in real:
        hits = [r for r in by_key.get(p["key"], []) if overlaps(r, p)]
        if not hits:
            misses.append(p)
            continue
        r = min(hits, key=lambda r: r["detected_at"])
        for h in hits:
            matched_rows.add(h["id"])
        # the clock starts when the change could be seen: if its channel's feed (or the truth feed) was silent, when it came back
        visible = at(p["start"])
        for feed in ("reservations", p["key"][1]):
            w = feeds.window(feed, visible)
            if w and w[1]:
                visible = max(visible, w[1])
        delays.append(max(0.0, (r["detected_at"] - visible).total_seconds() / 60))
        mine = CAUSE_LABEL.get(hits[-1]["cause"])
        cause_n += 1
        if mine == p["cause"]:
            cause_ok += 1
        else:
            wrong_cause[(hits[-1]["cause"], p["cause"])] += 1
        if p["critical"]:
            crit_n += 1
            crit_ok += any("critical" in h["severities"] for h in hits)
    stale_calls = 0
    for r in found:
        for feed in ("reservations", r["channel"]):
            w = feeds.window(feed, r["detected_at"])
            if w and r["detected_at"] - w[0] > timedelta(seconds=load_contracts()["communication"].max_data_age_sec):
                stale_calls += 1
    precision = len(matched_rows) / len(found) if found else 1.0
    recall = (len(real) - len(misses)) / len(real) if real else 1.0
    return {"seed": day["seed"], "chaos": day["chaos"], "real": len(real), "found": len(found), "recall": recall, "precision": precision,
            "delay_avg": sum(delays) / len(delays) if delays else 0.0, "delay_max": max(delays) if delays else 0.0,
            "cause_accuracy": cause_ok / cause_n if cause_n else None, "wrong_causes": {f"agent {a} / simulator {b}": n for (a, b), n in wrong_cause.most_common(6)},
            "critical_real": crit_n, "critical_flagged": crit_ok, "stale_calls": stale_calls,
            "forbidden_reads": len(sink["forbidden_reads"]), "errors": len(sink["errors"]),
            "escalations": len(sink["recs"]), "missed": [(list(p["key"]), p["start"], p["end"], p["cause"]) for p in misses[:5]]}


def verdict(sc: dict) -> str:
    ok = (sc["recall"] >= TARGETS["recall"] and sc["precision"] >= TARGETS["precision"] and sc["delay_max"] <= TARGETS["delay_min"]
          and (sc["critical_real"] == 0 or sc["critical_flagged"] == sc["critical_real"]) and sc["stale_calls"] == 0
          and sc["forbidden_reads"] == 0 and sc["errors"] == 0)
    return "PASS" if ok else "FAIL"


def feeds_for_day(seed: int, chaos: bool) -> Feeds:
    """Some random days get a silent channel or truth feed (to check that no mismatch is called on a stale feed)."""
    if not chaos:
        return Feeds()
    rng = random.Random(f"comm-outage:{seed}")
    if rng.random() < 0.4:
        feed = rng.choice(["reservations", "app", "sms", "gate_screen", "call_center"])
        start = at(rng.randint(9 * 60, 20 * 60))
        return Feeds(outages={feed: [(start, start + timedelta(minutes=rng.randint(10, 45)))]})
    return Feeds()


def run_all(seeds_random: list[int], data: dict, seed7: bool = True) -> list[dict]:
    out = []
    days = ([(7, False)] if seed7 else []) + [(s, True) for s in seeds_random]
    for seed, chaos in days:
        day = play(seed, data, chaos)
        feeds = feeds_for_day(seed, chaos)
        sink = replay(day, feeds)
        sc = judge(day, sink, feeds)
        sc["outage"] = {f: [(a.isoformat()[11:16], b.isoformat()[11:16]) for a, b in w] for f, w in feeds.outages.items()}
        sc["verdict"] = verdict(sc)
        out.append(sc)
    return out


def summary(scores: list[dict]) -> dict:
    rnd = [s for s in scores if s["chaos"]]

    def agg(xs):
        real, found = sum(s["real"] for s in xs), sum(s["found"] for s in xs)
        hit = sum(s["recall"] * s["real"] for s in xs)
        prec = sum(s["precision"] * s["found"] for s in xs)
        cn = [s for s in xs if s["cause_accuracy"] is not None]
        return {"days": len(xs), "real": real, "found": found, "recall": hit / real if real else 1.0,
                "precision": prec / found if found else 1.0, "delay_max": max((s["delay_max"] for s in xs), default=0),
                "delay_avg": sum(s["delay_avg"] for s in xs) / len(xs) if xs else 0,
                "cause_accuracy": sum(s["cause_accuracy"] * s["real"] for s in cn) / sum(s["real"] for s in cn) if cn else None,
                "critical": f"{sum(s['critical_flagged'] for s in xs)}/{sum(s['critical_real'] for s in xs)}",
                "stale_calls": sum(s["stale_calls"] for s in xs), "forbidden_reads": sum(s["forbidden_reads"] for s in xs),
                "pass": sum(1 for s in xs if s["verdict"] == "PASS")}
    return {"seed7": agg([s for s in scores if not s["chaos"]]), "random": agg(rnd)}


def write_page(scores: list[dict]) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "communication.json").write_text(json.dumps(scores, indent=1, default=str), encoding="utf-8")
    sm = summary(scores)
    e = html.escape

    def pct(x):
        return "-" if x is None else f"{x:.1%}"
    rows = "".join(f"<tr><td>{e('seed 7 (a31_storm)' if not s['chaos'] else f'chaos-{s[chr(115)+chr(101)+chr(101)+chr(100)]}')}</td>"
                   f"<td>{e(s['verdict'])}</td><td>{s['real']}</td><td>{s['found']}</td><td>{pct(s['recall'])}</td>"
                   f"<td>{pct(s['precision'])}</td><td>{s['delay_max']:.0f}</td><td>{pct(s['cause_accuracy'])}</td>"
                   f"<td>{s['critical_flagged']}/{s['critical_real']}</td><td>{s['stale_calls']}</td><td>{s['forbidden_reads']}</td>"
                   f"<td>{e(json.dumps(s['outage']) if s['outage'] else '-')}</td></tr>" for s in scores)
    head = "".join(f"<tr><td>{e(k)}</td><td>{v['pass']}/{v['days']}</td><td>{v['real']}</td><td>{v['found']}</td><td>{pct(v['recall'])}</td>"
                   f"<td>{pct(v['precision'])}</td><td>{v['delay_max']:.0f}</td><td>{pct(v['cause_accuracy'])}</td><td>{v['critical']}</td>"
                   f"<td>{v['stale_calls']}</td><td>{v['forbidden_reads']}</td></tr>" for k, v in sm.items() if v["days"])
    cols = ("<tr><th>day</th><th>verdict</th><th>real wrong periods</th><th>found</th><th>recall</th><th>precision</th>"
            "<th>worst delay (min)</th><th>cause right</th><th>critical flagged</th><th>calls on stale feed</th><th>name reads</th>")
    page = (f"<html><head><meta charset='utf-8'><title>Communication agent: detection</title>"
            "<style>body{font-family:Segoe UI,Arial;margin:24px}table{border-collapse:collapse}td,th{border-bottom:1px solid #ddd;"
            "padding:4px 8px;font-size:13px;text-align:left}</style></head><body><h1>Communication agent (4b-1): detecting mismatches</h1>"
            f"<p>Targets: recall at least {TARGETS['recall']:.0%}, precision at least {TARGETS['precision']:.0%}, detection within "
            f"{TARGETS['delay_min']} min, every critical case flagged critical. Days played in memory.</p>"
            f"<h2>Summary</h2><table>{cols.replace('<th>day</th><th>verdict</th>', '<th>set</th><th>pass</th>')}</tr>{head}</table>"
            f"<h2>Each day</h2><table>{cols}<th>silent feed injected</th></tr>{rows}</table></body></html>")
    path = OUT / "communication.html"
    path.write_text(page, encoding="utf-8")
    return str(path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed7-only", action="store_true")
    ap.add_argument("--days", type=int, default=RANDOM_DAYS)
    ap.add_argument("--first-seed", type=int, default=FIRST_RANDOM_SEED)
    args = ap.parse_args(argv)
    from backend.seed.build import build
    data = build()
    seeds = [] if args.seed7_only else list(range(args.first_seed, args.first_seed + args.days))
    scores = run_all(seeds, data)
    for s in scores:
        print(f"{'seed 7' if not s['chaos'] else 'chaos-' + str(s['seed']):14s} {s['verdict']}  real {s['real']:4d} found {s['found']:4d} "
              f"recall {s['recall']:.1%} precision {s['precision']:.1%} delay max {s['delay_max']:.0f} min | cause right "
              f"{(s['cause_accuracy'] or 0):.0%} | critical {s['critical_flagged']}/{s['critical_real']} | stale calls {s['stale_calls']} | "
              f"name reads {s['forbidden_reads']} | errors {s['errors']} | escalations {s['escalations']}"
              + (f" | silent {s['outage']}" if s["outage"] else ""))
        if s["missed"]:
            print("      missed e.g.", s["missed"][:3])
        if s["wrong_causes"]:
            print("      cause (agent, simulator) mismatches:", s["wrong_causes"])
    sm = summary(scores)
    print(json.dumps(sm, indent=1, default=str))
    print("page:", write_page(scores))
    if seeds:                                         # the chaos index: the Situation agent on the same days, with a Communication column
        from chaos.memory_replay import play_day, score as situation_score
        from chaos.pages import write_day_page, write_index
        sit = []
        for seed in seeds:
            sc = situation_score(play_day(seed, data))
            write_day_page(None, sc)
            sit.append(sc)
        print("index:", write_index(sit, title="Chaos days: the Situation and Communication agents (played in memory)"))


if __name__ == "__main__":
    main()
