"""Read-only data for the front end: one simulated day, as the screens need it.

Nothing here writes to the database, and nothing reads passenger names or contact details.
Times are minutes after midnight, local time (America/Chicago), so the browser can replay the day.
"""
from datetime import datetime

import yaml

from backend.database import connect
from backend.settings import ROOT, SIM_DATE, TZ

AIRPORTS_FILE = ROOT / "config" / "airports.yaml"

FLIGHT_EVENTS = (
    "flight_airborne", "flight_departed", "flight_arrived", "flight_delayed", "flight_delay_updated",
    "flight_holding", "flight_diverted", "flight_resumed", "flight_cancelled", "tarmac_started", "tarmac_ended",
)
WORLD_EVENTS = ("ground_stop_started", "ground_stop_extended", "ground_stop_ended", "ramp_closed", "ramp_reopened")
ACTIVITY_EVENTS = (
    "crew_illegal_rest", "staffing_gap_found", "staff_called_in", "gate_assigned", "human_approved",
    "decision_escalated", "break_due", "situation_assessed", "crew_warning", "fuel_warning", "recovery_action_applied",
    "hotel_room_assigned", "message_delivered", "process_fallback", "agents_disagree",
)
ROLE_NAMES = {
    "station_manager": "Station manager", "customer_service_supervisor": "Customer service supervisor",
    "network_operations_control": "Network operations", "crew_scheduling": "Crew scheduling",
    "ramp_supervisor": "Ramp supervisor", "director_of_operations": "Duty manager",
}
AGENT_NAMES = {
    "situation": "Situation", "network_recovery": "Network Recovery", "passenger_care": "Passenger Care",
    "station_staff": "Station & Staff", "communication": "Communication", "orchestrator": "Orchestrator",
}


def _minute(ts: datetime) -> float:
    local = ts.astimezone(TZ)
    day_offset = (local.date() - SIM_DATE).days
    return round(day_offset * 1440 + local.hour * 60 + local.minute + local.second / 60, 2)


def _hhmm(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.astimezone(TZ).strftime("%H:%M")


def runs() -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT run_id, count(*) FROM events WHERE run_id IS NOT NULL AND run_id NOT LIKE 'chaos-%%' "
                    "GROUP BY run_id ORDER BY run_id")
        out = []
        for run_id, n in cur.fetchall():
            mode = run_id.rsplit("-", 1)[-1]
            out.append({"run_id": run_id, "mode": mode, "events": n,
                        "label": {"today": "Today's process", "copilot": "Co-pilot advises",
                                  "orchestrated": "Co-pilot, approved decisions applied"}.get(mode, mode)})
        return out


def _activity_text(kind: str, p: dict) -> tuple[str, str] | None:
    """(who, text) in plain words, or None to skip."""
    role = ROLE_NAMES.get(p.get("role") or p.get("approved_by") or "", p.get("role") or "")
    if kind == "staffing_gap_found":
        return "Station & Staff", f"{p.get('short_by')} {p.get('role', '').replace('_', ' ')} short {_hhmm(p['from'])}-{_hhmm(p['to'])}"
    if kind == "staff_called_in":
        return "Station & Staff", f"Called in {p.get('staff_id')} ({p.get('role', '').replace('_', ' ')}), approved by {role or 'station manager'}"
    if kind == "gate_assigned":
        return "Station & Staff", f"Gate {p.get('gate')} set for {p.get('flight_id')}"
    if kind == "human_approved":
        return role or "A person", f"Approved: {str(p.get('action', '')).replace('_', ' ')}"
    if kind == "decision_escalated":
        return "Orchestrator", f"No answer in time, passed to {ROLE_NAMES.get(p.get('to_role'), p.get('to_role'))}: {str(p.get('action', '')).replace('_', ' ')}"
    if kind == "break_due":
        return "Station & Staff", f"Break due for {p.get('staff_id')} at {_hhmm(p.get('at'))}"
    if kind == "situation_assessed":
        return "Situation", "New picture of what is happening"
    if kind == "crew_warning":
        return "Network Recovery", f"Crew warning on {p.get('flight_id', 'a flight')}"
    if kind == "fuel_warning":
        return "Network Recovery", f"Fuel warning for {p.get('flight_id', 'a flight')}"
    if kind == "recovery_action_applied":
        return "Orchestrator", f"Approved change applied: {str(p.get('action', '')).replace('_', ' ')} {p.get('flight_id', '')}".strip()
    if kind == "hotel_room_assigned":
        return "Passenger Care", "Hotel room assigned"
    if kind == "message_delivered":
        return None
    if kind == "process_fallback":
        return "Today's process", f"Acted without a co-pilot decision: {str(p.get('kind', p.get('decision', ''))).replace('_', ' ')}"
    if kind == "agents_disagree":
        return "Orchestrator", "Two agents disagree; a person was asked"
    return None


def day(run_id: str) -> dict:
    coords = yaml.safe_load(AIRPORTS_FILE.read_text(encoding="utf-8"))
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, name, is_base FROM stations ORDER BY code")
        airports = [{"code": c, "name": n, "base": bool(b), "lat": coords[c][0], "lon": coords[c][1]}
                    for c, n, b in cur.fetchall() if c in coords]

        cur.execute("SELECT id, direction, origin, destination, sched_dep, sched_arr, pax_total, aircraft_id, priority "
                    "FROM flights WHERE (sched_dep AT TIME ZONE %s)::date = %s ORDER BY sched_dep",
                    (str(TZ), SIM_DATE))
        flights = [{"id": f, "dir": d, "from": o.strip(), "to": t.strip(), "dep": _minute(sd), "arr": _minute(sa),
                    "pax": px, "aircraft": ac, "priority": pr}
                   for f, d, o, t, sd, sa, px, ac, pr in cur.fetchall()]

        cur.execute("SELECT occurred_at, type, payload FROM events WHERE run_id = %s AND type = ANY(%s) ORDER BY occurred_at, id",
                    (run_id, list(FLIGHT_EVENTS + WORLD_EVENTS)))
        flight_events, world = [], []
        for at, kind, p in cur.fetchall():
            row = {"t": _minute(at), "type": kind, **{k: v for k, v in (p or {}).items()
                                                       if k in ("flight_id", "to", "delay_min", "new_departure", "diverted_to",
                                                                "expected_landing", "reason", "expected_end", "program",
                                                                "extra_flying_min")}}
            (world if kind in WORLD_EVENTS else flight_events).append(row)

        cur.execute("SELECT occurred_at, payload FROM events WHERE run_id = %s AND type = 'station_status' ORDER BY occurred_at",
                    (run_id,))
        station = [{"t": _minute(at), "ramp_open": p.get("ramp_open"), "people": p.get("people_in_terminal"),
                    "capacity": p.get("terminal_capacity")} for at, p in cur.fetchall()]

        cur.execute("SELECT occurred_at, type, payload FROM events WHERE run_id = %s AND type = ANY(%s) ORDER BY occurred_at, id",
                    (run_id, list(ACTIVITY_EVENTS)))
        activity, crew_flags, grouped = [], 0, {}
        for at, kind, p in cur.fetchall():
            p = p or {}
            if kind == "crew_illegal_rest":
                crew_flags += 1
                continue
            t = _minute(at)
            if kind in ("break_due", "staffing_gap_found", "gate_assigned", "staff_called_in"):
                grouped.setdefault((t, kind), []).append(p)   # many at once: one line each kind per minute
                continue
            said = _activity_text(kind, p)
            if said:
                activity.append({"t": t, "who": said[0], "text": said[1], "kind": kind})
        for (t, kind), ps in grouped.items():
            if len(ps) == 1:
                who, text = _activity_text(kind, ps[0])
            elif kind == "break_due":
                who, text = "Station & Staff", f"Breaks due at {_hhmm(ps[0].get('at'))} for {len(ps)} staff"
            elif kind == "staffing_gap_found":
                worst = {}
                for q in ps:
                    r = q.get("role", "").replace("_", " ")
                    worst[r] = max(worst.get(r, 0), q.get("short_by") or 0)
                who, text = "Station & Staff", "Staff gaps ahead: " + ", ".join(f"up to {n} {r}" for r, n in worst.items())
            elif kind == "gate_assigned":
                who, text = "Station & Staff", "Gates set for " + ", ".join(sorted({q.get('flight_id') for q in ps}))
            else:
                who, text = "Station & Staff", f"Called in {len(ps)} staff, approved by the station manager"
            activity.append({"t": t, "who": who, "text": text, "kind": kind})
        activity.sort(key=lambda a: a["t"])

        cur.execute("SELECT at, row FROM orchestrator_log WHERE run_id = %s ORDER BY at", (run_id,))
        ticks = [{"t": _minute(at), "ran": r.get("ran", []), "queue": r.get("queue", 0),
                  "incidents": [i for i in r.get("incidents", []) if i.get("status") != "closed"]}
                 for at, r in cur.fetchall()]

        # Situation's pictures, newest version last. Only runs with the co-pilot have them.
        cur.execute("SELECT version, assessed_at, status, type, likely_cause, confidence, expected_end_earliest, "
                    "expected_end_likely, expected_end_latest, end_confidence, summary, impact, evidence "
                    "FROM situation_assessments WHERE run_id = %s ORDER BY version", (run_id,))
        situation = []
        for (v, at, status, kind, cause, conf, e1, e2, e3, end_conf, summary, impact, evidence) in cur.fetchall():
            impact = impact or {}
            arr, dep = impact.get("arrivals") or {}, impact.get("departures") or {}
            situation.append({
                "version": v, "t": _minute(at), "status": status, "type": kind, "cause": cause,
                "confidence": float(conf) if conf is not None else None,
                "end_earliest": _hhmm(e1) if e1 else None, "end_likely": _hhmm(e2) if e2 else None,
                "end_latest": _hhmm(e3) if e3 else None,
                "end_confidence": float(end_conf) if end_conf is not None else None,
                "summary": summary,
                "impact": {"diverted": arr.get("ever_diverted", 0), "holding": arr.get("holding", 0),
                           "cancelled": (arr.get("cancelled", 0) or 0) + (dep.get("cancelled", 0) or 0),
                           "delayed": (arr.get("ever_delayed_30_plus", 0) or 0) + (dep.get("ever_delayed_30_plus", 0) or 0),
                           "passengers": impact.get("passengers_on_affected_flights", 0)},
                "sources": sorted({e.get("source") for e in (evidence or []) if e.get("source")}),
            })

    return {
        "run_id": run_id, "date": SIM_DATE.isoformat(), "timezone": str(TZ),
        "airports": airports, "flights": flights, "flight_events": flight_events, "world": world,
        "station": station, "activity": activity, "orchestrator": ticks, "situation": situation,
        "roster_flags": crew_flags, "agents": AGENT_NAMES,
    }
