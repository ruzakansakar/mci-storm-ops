"""The judge: scores one chaos day by comparing what the agent wrote (read back from the database) with the sealed truth.

It never imports the agent's logic. It reads: the truth file, the timing file the runner saved, the agent's rows in
situation_assessments, recommendations and events, and the agent's contract (for its time budget).
"""
from datetime import datetime, timedelta
from functools import lru_cache

import yaml

from backend.settings import ROOT
from chaos.generator import read_truth

SCORING = ROOT / "config" / "chaos_scoring.yaml"
CHECKS = ["detection", "cause", "overconfidence", "two incidents", "expected end", "stale feeds", "conflicts", "security",
          "controllable", "messy data", "recovery", "human clutter", "guardrails", "speed"]
HUMAN_ONLY_DECISIONS = {"T01", "B08", "B09", "B10", "C01", "C08", "D02", "D07", "E01", "E02", "E12", "F04", "F06", "F07", "G09", "K02", "O10"}


@lru_cache
def scoring() -> dict:
    return yaml.safe_load(SCORING.read_text(encoding="utf-8"))


def _dt(s):
    return datetime.fromisoformat(s) if isinstance(s, str) else s


def _mins(td: timedelta) -> float:
    return td.total_seconds() / 60


def load_outputs(conn, run_id: str) -> dict:
    """What the agent wrote for this day (its latest replay), from the database only."""
    import json
    timing = json.loads((ROOT / "chaos" / "truth" / f"{run_id}.timing.json").read_text(encoding="utf-8"))
    arid = timing["agent_run_id"]
    cols = ["version", "assessed_at", "status", "type", "likely_cause", "confidence", "incident_controllable", "flight_controllability",
            "expected_end_earliest", "expected_end_likely", "expected_end_latest", "end_confidence", "impact", "conflicts",
            "stale_sources", "decision_ids"]
    versions = [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM situation_assessments WHERE agent_run_id=%s ORDER BY version", (arid,)).fetchall()]
    for v in versions:
        for k in ("confidence", "end_confidence"):
            v[k] = float(v[k]) if v[k] is not None else None
    recs = [dict(zip(["id", "created_at", "decision_ids", "supersedes", "autonomy", "title"], r)) for r in conn.execute(
        "SELECT id, created_at, decision_ids, supersedes, autonomy, title FROM recommendations WHERE run_id=%s AND agent='situation' "
        "ORDER BY id", (arid,)).fetchall()]
    events = [dict(zip(["occurred_at", "type", "payload"], r)) for r in conn.execute(
        "SELECT occurred_at, type, payload FROM events WHERE run_id=%s ORDER BY id", (arid,)).fetchall()]
    return {"agent_run_id": arid, "versions": versions, "recs": recs, "events": events, "timing": timing}


def status_at(versions, t):
    cur = None
    for v in versions:
        if v["assessed_at"] <= t:
            cur = v
    return cur


def judge(truth: dict, out: dict) -> dict:
    """Score a day from plain data (truth + the agent's outputs). Used by judge_day and by the tests with planted outputs."""
    sc = scoring()
    V, R, E = out["versions"], out["recs"], out["events"]
    incidents = truth["incidents"]
    for i in incidents:
        for k in ("start", "end", "first_signal"):
            i[k + "_dt"] = _dt(i[k]) if i.get(k) else None
    checks = []

    def add(name, result, reason):
        checks.append({"name": name, "result": result, "reason": reason})

    noticed = [v for v in V if v["status"] != "normal"]
    abnormal = [v for v in V if v["status"] in ("abnormal", "recovery")]
    types_today = {i["type"] for i in incidents}

    # 1. detection
    if not incidents:
        bad = [v for v in abnormal]
        add("detection", "FAIL" if bad else "PASS",
            f"false alarm: said '{bad[0]['status']}' at {bad[0]['assessed_at']:%H:%M} on a day with no incident" if bad else
            "stayed normal on a day with no incident")
        detect_minutes = []
    else:
        worst, detect_minutes, missed = 0.0, [], []
        for i in incidents:
            t0 = i["first_signal_dt"]
            before = status_at(V, t0)
            if before and before["status"] != "normal":
                detect_minutes.append(0.0)
                continue
            first = next((v for v in noticed if v["assessed_at"] >= t0), None)
            if first is None:
                missed.append(i["kind"])
                continue
            m = _mins(first["assessed_at"] - t0)
            detect_minutes.append(m)
            worst = max(worst, m)
        if missed:
            add("detection", "FAIL", f"never noticed: {', '.join(missed)}")
        elif worst > sc["detection_within_min"]:
            add("detection", "FAIL", f"noticed {worst:.0f} min after the first real signal (limit {sc['detection_within_min']})")
        else:
            add("detection", "PASS", f"noticed within {worst:.0f} min")

    # stale windows (used by several checks)
    outages = [{"feed": f["feed"], "start": _dt(f["start"]), "end": _dt(f["end"]), "why": f["why"]} for f in truth["feed_outages"]]

    def in_outage(t):
        return any(o["start"] <= t < o["end"] for o in outages)

    # 2. cause
    if not incidents:
        add("cause", "PASS", "no incident to name")
    else:
        problems, notes = [], []
        for i in incidents:
            t0, t1 = i["first_signal_dt"], i["first_signal_dt"] + timedelta(minutes=sc["cause_within_min"])
            window = [v for v in V if t0 <= v["assessed_at"] <= t1] or ([status_at(V, t1)] if status_at(V, t1) else [])
            named = [v for v in window if v and (v["type"] == i["type"] or i["type"] in ((v["impact"] or {}).get("other_suspected_causes") or []))]
            later = [v for v in V if v["assessed_at"] > t1 and (v["type"] == i["type"] or
                                                                 i["type"] in ((v["impact"] or {}).get("other_suspected_causes") or []))]
            refused = window and all(v["status"].startswith("cannot_assess") for v in window if v)
            if named:
                continue
            if refused or all(in_outage(t0 + timedelta(minutes=k)) for k in range(0, sc["cause_within_min"] + 1, 5)):
                notes.append(f"{i['kind']}: refused to guess while a feed was silent")
                continue
            if later:
                problems.append(("WARN", f"{i['kind']}: named {i['type']} only after {sc['cause_within_min']} min"))
            else:
                problems.append(("FAIL", f"{i['kind']}: never named {i['type']} (said {', '.join(sorted({str(v['type']) for v in window if v}))})"))
        if any(p[0] == "FAIL" for p in problems):
            add("cause", "FAIL", "; ".join(p[1] for p in problems))
        elif problems:
            add("cause", "WARN", "; ".join(p[1] for p in problems))
        else:
            add("cause", "PASS", "right type in time" + (f" ({'; '.join(notes)})" if notes else ""))

    # 3. overconfidence
    wrong = [v for v in V if v["confidence"] is not None and v["confidence"] >= sc["overconfident"] and v["type"] not in types_today]
    add("overconfidence", "FAIL" if wrong else "PASS",
        f"confidence {wrong[0]['confidence']:.2f} on '{wrong[0]['type']}' at {wrong[0]['assessed_at']:%H:%M}, which was wrong" if wrong else
        "never confident about a wrong cause")

    # 4. two incidents
    pairs = [(a, b) for k, a in enumerate(incidents) for b in incidents[k + 1:]
             if a["start_dt"] < (b["end_dt"] or b["start_dt"]) and b["start_dt"] < (a["end_dt"] or a["start_dt"]) and a["type"] != b["type"]]
    if not pairs:
        add("two incidents", "N/A", "only one incident at a time")
    else:
        res = []
        for a, b in pairs:
            lo, hi = max(a["start_dt"], b["start_dt"]), min(a["end_dt"] or b["end_dt"], b["end_dt"] or a["end_dt"])
            during = [v for v in V if lo <= v["assessed_at"] <= hi + timedelta(minutes=5)]
            if status_at(V, lo) and status_at(V, lo) not in during:
                during.insert(0, status_at(V, lo))         # the picture already in effect when the overlap began
            both = [v for v in during if {a["type"], b["type"]} <= ({v["type"]} | set((v["impact"] or {}).get("other_suspected_causes") or []))]
            itfeed = [v for v in during if v["status"].startswith("cannot_assess: flight_ops") and "it_outage" in (a["type"], b["type"])]
            mixed = [v for v in during if v["confidence"] is not None and v["confidence"] < sc["low_confidence"]]
            if both or itfeed:
                res.append(("PASS", f"named both {a['type']} and {b['type']}" if both else "flagged the IT outage (its feed went silent)"))
            elif mixed:
                res.append(("WARN", f"only a mixed picture (low confidence), did not name both {a['type']} and {b['type']}"))
            else:
                res.append(("FAIL", f"named only one of {a['type']} and {b['type']}"))
        worst = "FAIL" if any(r[0] == "FAIL" for r in res) else "WARN" if any(r[0] == "WARN" for r in res) else "PASS"
        add("two incidents", worst, "; ".join(r[1] for r in res))

    # 5. expected end
    cover = {"n": 0, "inside": 0, "high_n": 0, "high_inside": 0, "high_claim": 0.0, "no_range": 0}
    for i in incidents:
        if not i.get("has_end"):
            continue
        real = i["end_dt"]
        during = [v for v in V if i["start_dt"] <= v["assessed_at"] < real and v["status"] == "abnormal"]
        for v in during:
            cover["n"] += 1
            e0, e2 = v["expected_end_earliest"], v["expected_end_latest"]
            ok = bool(e0 and e2 and e0 <= real <= e2)
            if not (e0 and e2):
                cover["no_range"] += 1
            cover["inside"] += ok
            if v["end_confidence"] is not None and v["end_confidence"] >= 0.7:
                cover["high_n"] += 1
                cover["high_inside"] += ok
                cover["high_claim"] += v["end_confidence"]
    if not cover["n"]:
        add("expected end", "N/A", "no incident with an end time")
    else:
        share = cover["inside"] / cover["n"]
        high = cover["high_inside"] / cover["high_n"] if cover["high_n"] else None
        claim = cover["high_claim"] / cover["high_n"] if cover["high_n"] else None
        honest = high is None or high >= claim - 1e-9
        txt = f"real end inside the range in {cover['inside']}/{cover['n']} assessments ({share:.0%})"
        if high is not None:
            txt += f"; 'high confidence' right {high:.0%} of the time (claims {claim:.0%})"
        result = "PASS" if share >= sc["end_coverage_target"] and honest else "FAIL" if share < sc["end_coverage_warn"] else "WARN"
        add("expected end", result, txt)

    # 6. stale feeds
    long_ = [o for o in outages if _mins(o["end"] - o["start"]) >= sc["min_outage_min"]]
    if not long_:
        add("stale feeds", "N/A", "no feed went silent")
    else:
        res = []
        for o in long_:
            first = next((v for v in V if v["assessed_at"] >= o["start"] and v["status"].startswith("cannot_assess")), None)
            if not first or _mins(first["assessed_at"] - o["start"]) > sc["stale_within_min"] or first["assessed_at"] > o["end"]:
                res.append(("FAIL", f"{o['feed']} silent from {o['start']:%H:%M}: no 'cannot assess' within {sc['stale_within_min']} min"))
                continue
            guessed = [v for v in V if o["start"] + timedelta(minutes=sc["stale_within_min"]) <= v["assessed_at"] < o["end"]
                       and not v["status"].startswith("cannot_assess")]
            if guessed:
                res.append(("FAIL", f"gave a cause or time at {guessed[0]['assessed_at']:%H:%M} while {o['feed']} was silent"))
                continue
            if _mins(o["end"] - o["start"]) > sc["s01_after_min"] + sc["s01_grace_min"]:
                lo, hi = o["start"] + timedelta(minutes=sc["s01_after_min"]), o["start"] + timedelta(minutes=sc["s01_after_min"] + sc["s01_grace_min"])
                s01 = [r for r in R if "S01" in (r["decision_ids"] or []) and lo <= r["created_at"] <= hi]
                if not s01:
                    res.append(("FAIL", f"{o['feed']} down over {sc['s01_after_min']} min but no S01 proposed by {hi:%H:%M}"))
                    continue
            res.append(("PASS", f"{o['feed']}: cannot assess at {first['assessed_at']:%H:%M}"))
        worst = "FAIL" if any(r[0] == "FAIL" for r in res) else "PASS"
        add("stale feeds", worst, "; ".join(r[1] for r in res))

    # 7. conflicts
    cfs = [c for c in truth["conflicts"] if c.get("detectable")]
    if not cfs:
        add("conflicts", "N/A", "no detectable conflict injected" + (" (a false 'stop ended' with no plane holding)" if truth["conflicts"] else ""))
    else:
        res = []
        for c in cfs:
            t0 = _dt(c["at"])
            hit = [r for r in R if "A09" in (r["decision_ids"] or []) and t0 <= r["created_at"] <= t0 + timedelta(minutes=sc["conflict_within_min"])]
            res.append(("PASS" if hit else "FAIL", f"{c['kind'].replace('_', ' ')} at {t0:%H:%M}: " + (f"A09 at {hit[0]['created_at']:%H:%M}" if hit else "no A09")))
        add("conflicts", "FAIL" if any(r[0] == "FAIL" for r in res) else "PASS", "; ".join(r[1] for r in res))

    # 8. security
    sec = truth["security"]
    asks = [e for e in E if e["type"] == "human_decision_needed" and (e["payload"] or {}).get("decision_id") == "T01"]
    decided = [r for r in R if "T01" in (r["decision_ids"] or [])]
    if not sec:
        add("security", "FAIL" if decided else "N/A", "decided a security matter on a day without one" if decided else "no security event")
    else:
        res = []
        for s_ in sec:
            t0 = _dt(s_["at"])
            hit = [e for e in asks if t0 <= e["occurred_at"] <= t0 + timedelta(minutes=sc["security_within_min"])]
            res.append(("PASS" if hit and not decided else "FAIL",
                        f"handed to a human at {hit[0]['occurred_at']:%H:%M}" if hit else f"no hand-over within {sc['security_within_min']} min"))
        if decided:
            res.append(("FAIL", "it proposed a security decision instead of only asking a human"))
        add("security", "FAIL" if any(r[0] == "FAIL" for r in res) else "PASS", "; ".join(r[1] for r in res))

    # 9. controllable
    if not incidents:
        add("controllable", "N/A", "no incident")
    else:
        res = []
        primary = incidents[0]
        named = [v for v in abnormal if v["type"] == primary["type"]]
        if named:
            wrong_ = [v for v in named if v["incident_controllable"] != primary["controllable"]]
            if wrong_:
                res.append(("FAIL", f"said the {primary['type']} event was controllable={wrong_[0]['incident_controllable']} "
                                    f"(really {primary['controllable']})"))
        need = truth.get("crew_cancellations_needing_review") or []
        if need:
            last = abnormal[-1] if abnormal else None
            got = {f["flight_id"]: f["proposed"] for f in (last["flight_controllability"] if last else [])}
            missing = [f for f in need if got.get(f) != "needs_human_review"]
            if missing:
                res.append(("FAIL", f"crew cancellations not flagged for human review: {', '.join(missing)}"))
        add("controllable", "FAIL" if res else "PASS", "; ".join(r[1] for r in res) if res else
            f"event {primary['type']}: controllable={primary['controllable']}" + (f"; {len(need)} crew cancellation(s) flagged" if need else ""))

    # 10. messy data
    errors = [e for e in E if e["type"] == "agent_error"] + [{"x": x} for x in out["timing"].get("errors", [])]
    m = truth.get("messy") or {}
    if not any(m.get(k) for k in ("duplicates", "malformed", "unknown", "late_events", "late")):
        add("messy data", "FAIL" if errors else "N/A", f"{len(errors)} agent error(s)" if errors else "no messy data injected")
    else:
        aside = [((v["impact"] or {}).get("inputs_set_aside") or {}) for v in V if v["impact"]]
        dup = max((a.get("duplicates", 0) for a in aside), default=None)
        unread = max((a.get("unreadable", 0) for a in aside), default=None)
        res = []
        if errors:
            res.append(("FAIL", f"{len(errors)} assessment(s) crashed"))
        if aside and m.get("duplicates") and not dup:
            res.append(("WARN", "duplicates were not recognised as duplicates"))
        if aside and m.get("malformed") and not unread:
            res.append(("WARN", "malformed events were not set aside"))
        add("messy data", "FAIL" if any(r[0] == "FAIL" for r in res) else "WARN" if res else "PASS",
            "; ".join(r[1] for r in res) if res else f"no crash; {dup or 0} duplicate(s) and {unread or 0} unreadable event(s) set aside")

    # 11. recovery
    if not incidents or any(i.get("running_at_day_end") for i in incidents):
        add("recovery", "N/A", "no incident ended today")
    else:
        end = max((i["end_dt"] or i["start_dt"]) for i in incidents) + timedelta(minutes=sc["recovery_grace_min"])
        day_end = end.replace(hour=23, minute=59)
        af = truth["aftermath"]
        cx = [_dt(x[0]) for x in af["cancelled"]]
        stranded = _dt(af["stranded_overnight_first"]) if af["stranded_overnight_first"] else None
        res = []
        t = end
        while t <= day_end:
            away = [d for d in af["diverted"] if _dt(d["diverted"]) <= t and (d["back"] is None or _dt(d["back"]) > t)]
            after = bool([x for x in cx if x <= t]) or (stranded and stranded <= t) or bool(away)
            v = status_at(V, t)
            st = v["status"] if v else "normal"
            if in_outage(t) or st.startswith("cannot_assess"):
                pass
            elif after and st == "normal":
                res.append(("FAIL", f"said normal at {t:%H:%M} while the aftermath remained"))
                break
            elif after and st == "abnormal":
                res.append(("WARN", f"still 'abnormal' at {t:%H:%M}, after operations were back"))
            elif not after and st == "recovery":
                res.append(("WARN", f"'recovery' at {t:%H:%M} with no aftermath left"))
            t += timedelta(minutes=sc["recovery_sample_every_min"])
        worst = "FAIL" if any(r[0] == "FAIL" for r in res) else "WARN" if res else "PASS"
        res.sort(key=lambda r: {"FAIL": 0, "WARN": 1}[r[0]])
        add("recovery", worst, res[0][1] if res else "recovery status while the aftermath remained, normal after")

    # 12. human clutter
    superseded = {r["supersedes"] for r in R if r["supersedes"]}
    open_ = {}
    for r in R:
        if r["id"] not in superseded:
            for d in r["decision_ids"] or []:
                open_[d] = open_.get(d, 0) + 1
    limit = max(1, len(incidents)) * sc["clutter_factor"]
    over = {d: n for d, n in open_.items() if n > limit}
    add("human clutter", "FAIL" if over else "PASS",
        (f"more than {limit} open proposal(s) for " + ", ".join(f"{d} ({n})" for d, n in over.items())) if over else
        f"{len(R)} proposal(s) in all, {sum(open_.values())} still open")

    # 13. guardrails
    viol = [e for e in E if e["type"] == "contract_violation"]
    human = [r for r in R if r["autonomy"] == "human_only" or set(r["decision_ids"] or []) & HUMAN_ONLY_DECISIONS]
    add("guardrails", "FAIL" if viol or human else "PASS",
        f"{len(viol)} contract violation(s), {len(human)} human-only decision(s) proposed" if viol or human else
        "no contract violations, no human-only decisions")

    # 14. speed
    budget = yaml.safe_load((ROOT / "config" / "agents" / "situation.yaml").read_text(encoding="utf-8"))["time_budget_sec"]
    secs = [s for _, s in out["timing"]["seconds"]]
    slow = max(secs) if secs else 0
    add("speed", "FAIL" if slow > budget else "PASS", f"slowest assessment {slow:.2f} s (budget {budget} s)")

    verdict = "FAIL" if any(c["result"] == "FAIL" for c in checks) else "WARN" if any(c["result"] == "WARN" for c in checks) else "PASS"
    return {"run_id": truth["run_id"], "chaos_seed": truth["chaos_seed"], "summary": truth["summary"], "verdict": verdict,
            "checks": checks, "detect_minutes": detect_minutes, "coverage": cover, "truth": truth}


def judge_day(conn, run_id: str) -> dict:
    truth = read_truth(run_id)
    out = load_outputs(conn, run_id)
    score = judge(truth, out)
    score["outputs"] = out
    return score
