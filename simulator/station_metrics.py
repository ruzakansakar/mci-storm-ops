"""Measuring the Station & Staff agent (Phase 4f). Measurement only: it reads the co-pilot's station plans, its gate and staff
ledgers, the rule-check results and the simulator's record of the day (when planes really arrived, when the ramp really
closed). Nothing here goes back to an agent."""
from datetime import datetime, timedelta

import yaml

from backend.settings import ROOT
from backend.execution.ledger import effective  # noqa: F401  (re-exported for the tests and the page)
from simulator.clock import at


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def ground_staff_limits() -> dict:
    """The ground-staff numbers from the rules file (config, read as data)."""
    rules = yaml.safe_load((ROOT / "config" / "rules" / "operations.yaml").read_text(encoding="utf-8"))["rules"]
    by_id = {r["id"]: r["value"] for r in rules}
    return {"max_shift_min": by_id["ground_staff_max_shift_min"], "break_interval_min": by_id["ground_staff_break_interval_min"],
            "break_length_min": by_id["ground_staff_break_length_min"]}


def station_metrics(world, copilot, data) -> dict | None:
    sr = getattr(copilot, "station", None)
    if sr is None or not sr.memory.plans:
        return None
    plans, ev = sr.memory.plans, world.events
    last = plans[-1]
    lim = ground_staff_limits()
    roster = {s["id"]: s for s in data["staff"]}
    work = effective(sr.staff_ledger)
    called = {r["staff_id"]: _dt(r["from_at"]) for r in sr.staff_ledger if r["kind"] == "call_in"}
    # --- the staff limits: no two jobs at once, on shift for all of it, never across a planned break, breaks in time
    by = {}
    for w in work:
        by.setdefault(w["staff_id"], []).append(w)
    breaks = {}
    for b in last.get("breaks") or []:
        breaks.setdefault(b["staff_id"], []).append((_dt(b["from"]), _dt(b["to"])))
    overlaps, outside, over_break, longest, longest_shift = [], [], [], 0, 0
    for sid, ws in by.items():
        ws.sort(key=lambda w: w["from"])
        for x, y in zip(ws, ws[1:]):
            if y["from"] < x["to"]:
                overlaps.append((sid, x["flight_id"], y["flight_id"]))
        start, end = called.get(sid, roster[sid]["shift_start"]), roster[sid]["shift_end"]
        longest_shift = max(longest_shift, int((end - start).total_seconds() // 60))
        for w in ws:
            if w["from"] < start or w["to"] > end:
                outside.append((sid, w["flight_id"], w["task"]))
            if any(w["from"] < b and a < w["to"] for a, b in breaks.get(sid, [])):
                over_break.append((sid, w["flight_id"], w["task"]))
    for sid, s in roster.items():
        if s["station"] != "MCI":
            continue
        start, end = called.get(sid, s["shift_start"]), s["shift_end"]
        marks = [s.get("last_break") or start] + [b for a, b in sorted(breaks.get(sid, []))]
        nexts = [a for a, b in sorted(breaks.get(sid, []))] + [end]
        for m, n in zip(marks, nexts):
            longest = max(longest, int((n - m).total_seconds() // 60))
    # --- no ramp work while the ramp was really closed
    closures, opened = [], None
    for e in ev:
        if e["type"] == "ramp_closed":
            opened = at(e["t"])
        elif e["type"] == "ramp_reopened" and opened is not None:
            closures.append((opened, at(e["t"])))
            opened = None
    ramp_in_closure = [(w["staff_id"], w["flight_id"], w["from"].isoformat(), w["to"].isoformat()) for w in work if w["role"] == "ramp"
                       and any(w["from"] < b and a < w["to"] for a, b in closures)]
    # --- tarmac deadlines, from the last plan version that had each plane
    tarmac = {}
    for p in plans:
        for t in p["tarmac"]:
            tarmac[t["flight_id"]] = t
    tarmac_rows = [{"flight_id": t["flight_id"], "station": t["station"], "minutes": t["elapsed_min"], "kind": t["kind"],
                    "status_announcement": t["status_announcement"]["status"], "food_and_water": t["food_and_water"]["status"],
                    "start_return_by": t["start_return_by"]["status"], "start_return_by_due": t["start_return_by"]["due"],
                    "asked_captain": t["asked_captain"],
                    "ok": all(not x.startswith(("missed", "not yet")) or t["status"] == "ended" and x.startswith("not yet")
                              for x in (t["status_announcement"]["status"], t["food_and_water"]["status"]))
                    and (t["asked_captain"] or t["start_return_by"]["status"].startswith("met"))}
                   for t in tarmac.values()]
    asked = [e for e in ev if e["type"] == "human_decision_needed" and e.get("actor_name") == "station_staff"]
    # --- gates: A31 (and every arrival) against when the plane really landed
    arrived = {e["payload"]["flight_id"]: at(e["t"]) for e in ev if e["type"] == "flight_arrived" and e.get("station") == "MCI"}
    gate_at_landing = {}
    for g in sr.gate_ledger:
        if g["kind"] != "assign":
            continue
        rel = next((x for x in sr.gate_ledger if x["kind"] == "release" and x["recommendation"] == g["recommendation"]), None)
        land = arrived.get(g["flight_id"])
        a, b = _dt(g["from_at"]), _dt(g["to_at"])
        if land and a <= land <= b and (rel is None or _dt(rel["at"]) > land):
            gate_at_landing[g["flight_id"]] = {"gate": g["gate"], "remote_stand": g["remote_stand"], "from": g["from_at"], "to": g["to_at"],
                                              "approved_by": g["approved_by"], "waited_min": g["waited_min"], "assigned_at": g["at"],
                                              "landed": land.isoformat()}
    focus = (world.cfg.get("scenarios", {}).get(world.sc.name) or {}).get("flight")      # the story flight (A31), if any
    # --- wheelchair and child coverage, against the real landing time
    help_rows = []
    W = sr.s["staff"]["wheelchair"]
    reqs = {}
    for p in copilot.care.memory.plans if getattr(copilot, "care", None) else []:
        for r in p["wheelchair_requests"]:
            reqs[r["passenger_id"]] = r
    for pid, r in sorted(reqs.items()):
        for fid in r.get("itinerary") or []:
            land = arrived.get(fid)
            if not land:
                continue
            need = (land, land + timedelta(minutes=W["help_after_arrival_min"]))
            cov = sorted((w["from"], w["to"], w["staff_id"]) for w in work if w["passenger_id"] == pid and w["role"] == "wheelchair")
            gap, t = 0, need[0]
            for a, b, _ in cov:
                if b <= t or a >= need[1]:
                    continue
                gap = max(gap, int((a - t).total_seconds() // 60)) if a > t else gap
                t = max(t, b)
            gap = max(gap, int((need[1] - t).total_seconds() // 60)) if t < need[1] else gap
            help_rows.append({"passenger_id": pid, "flight_id": fid, "landed": land.isoformat(), "staff": sorted({c[2] for c in cov
                              if c[0] < need[1] and need[0] < c[1]}), "longest_unattended_min": gap})
    kids = []
    care_recs = getattr(copilot, "care", None) and copilot.care.context.saved or {}
    for p in copilot.care.memory.plans if getattr(copilot, "care", None) else []:
        for v in p["vulnerable"]:
            if v["kind"] == "child_alone" and v["passenger_id"] not in [k["passenger_id"] for k in kids]:
                kids.append({"passenger_id": v["passenger_id"], "care_decision": v.get("decision"), "itinerary": v.get("itinerary")})
    for k in kids:
        sup = [w for w in work if w["passenger_id"] == k["passenger_id"] and w["task"] == "child supervision"]
        land = next((arrived[f] for f in k["itinerary"] or [] if f in arrived), None)
        at_land = [w for w in sup if land and w["from"] <= land < w["to"]]
        rec = care_recs.get(("recommendations", k["care_decision"])) or {}
        k.update(landed=land.isoformat() if land else None, supervisor=at_land[0]["staff_id"] if at_land else None,
                 approved_by=at_land[0]["approved_by"] if at_land else None,
                 care_request_status=rec.get("status"), supervised_from=min((w["from"] for w in sup), default=None))
        if k["supervised_from"]:
            k["supervised_from"] = k["supervised_from"].isoformat()
    # --- crowding, approvals, shortages
    statuses = [e["payload"] for e in ev if e["type"] == "station_status" and e.get("station") == "MCI"]
    peak = max(statuses, key=lambda p: p["people_in_terminal"]) if statuses else {}
    v = sr.verdicts
    actions = {}
    for x in v:
        actions[x["proposal"]["action"]] = actions.get(x["proposal"]["action"], 0) + 1
    waits = [r["waited_min"] for r in sr.gate_ledger + sr.staff_ledger if r.get("waited_min") is not None and r["kind"] in ("assign", "call_in")]
    short_slots = {}
    for p in plans:
        for s_ in p["staff_shortages"]:
            short_slots[(s_["role"], s_["from"])] = max(short_slots.get((s_["role"], s_["from"]), 0), s_["short_by"])
    worst = {}
    for (role, frm), n in short_slots.items():
        if n > worst.get(role, (0, None))[0]:
            worst[role] = (n, frm)
    nr = getattr(copilot, "network", None)
    moved = sorted({(d["flight_id"], d["new_departure"], d["station_check"]) for p in (nr.memory.plans if nr else [])
                    for d in p["delays"] if d.get("station_check")})
    return {"plan_versions": sr.memory.version, "proposals": len(v), "by_action": actions,
            "rule_check_blocks": sum(1 for x in v if x["verdict"]["result"] != "pass"),
            "automatic": sum(1 for x in v if x["automatic"]), "approval_waits_min": waits,
            "gate_assignments": sum(1 for g in sr.gate_ledger if g["kind"] == "assign"),
            "gate_shortages": sum(len(p["gate_shortages"]) for p in plans[-1:]),
            "staff_assignments": len(work), "call_ins": sum(1 for r in sr.staff_ledger if r["kind"] == "call_in"),
            "call_ins_by_role": {r: sum(1 for x in sr.staff_ledger if x["kind"] == "call_in" and x["role"] == r)
                                 for r in sorted({x["role"] for x in sr.staff_ledger if x["kind"] == "call_in"})},
            "worst_shortage": {r: {"short_by": n, "from": f} for r, (n, f) in worst.items()},
            "limits": lim, "overlaps": overlaps, "outside_shift": outside, "over_planned_break": over_break,
            "longest_without_break_min": longest, "longest_shift_min": longest_shift,
            "ramp_closures": [(a.isoformat(), b.isoformat()) for a, b in closures], "ramp_work_in_closure": ramp_in_closure,
            "tarmac": tarmac_rows, "asked": [{"decision_id": e["payload"]["decision_id"], "at": at(e["t"]).isoformat(),
                                             "flight_id": (e["payload"].get("facts") or {}).get("flight_id")} for e in asked],
            "a31": {"flight_id": focus, **(gate_at_landing.get(focus) or {})} if focus else None,
            "arrivals_with_gate_at_landing": len(gate_at_landing), "arrivals": len(arrived),
            "wheelchair": help_rows, "children": kids,
            "terminal_peak": {"people": peak.get("people_in_terminal"), "capacity": peak.get("terminal_capacity")},
            "crowding_warnings": sum(1 for e in ev if e["type"] == "terminal_crowding_warning"),
            "network_moved_for_station": [{"flight_id": f, "new_departure": t, "why": w} for f, t, w in moved]}
