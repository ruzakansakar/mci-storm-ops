"""Turn the class files in data/raw into JSON the dashboard can load.

All data is SIMULATED (classroom dataset, not real Southwest data).
Run:  python scripts/build_data.py
Writes: dashboard/src/data/ops.json
"""
import json
import re
from pathlib import Path

import docx
import openpyxl

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "dashboard" / "src" / "data" / "ops.json"


def rows(ws):
    it = ws.iter_rows(values_only=True)
    head = next(it)
    return [dict(zip(head, r)) for r in it if any(v is not None for v in r)]


def hhmm_to_min(text, pm):
    h, m = map(int, re.findall(r"\d+", text)[:2])
    if pm and h < 12:
        h += 12
    return h * 60 + m


def load_schedule():
    d = docx.Document(RAW / "Southwest_MCI_Daily_Operating_Schedule.docx")
    table = d.tables[0]
    out, pm = [], False
    for r in table.rows[1:]:
        c = [x.text.strip() for x in r.cells]
        if "PM" in c[1]:
            pm = True
        kind, ac = c[3].rsplit(" ", 1)
        out.append({
            "n": int(c[0]),
            "dep": hhmm_to_min(c[1], pm),
            "dest": c[2].split()[-1],
            "destName": " ".join(c[2].split()[:-1]),
            "aircraft": ac,
            "type": kind.replace("MAX 8", "737 MAX 8"),
            "pax": int(c[4]),
            "crew": c[5],
            "nextDest": c[6].split()[-1],
            "priority": c[7] or None,
        })
    return out


CATEGORIES = [
    ("passengers in the schedule", "Passenger counts differ between the schedule and the recovery dataset"),
    ("Aircraft type", "Aircraft types differ between the schedule and the dataset"),
    ("Minimum rest", "Crew rest listed below the 10-hour legal minimum for pilots"),
    ("Destination", "Destinations differ between files"),
    ("No priority", "Flights with no priority assigned"),
    ("Children flying alone", "Children flying alone on an international flight"),
]


def summarize(conflicts):
    out = []
    for key, label in CATEGORIES:
        hits = [c for c in conflicts if key in c["issue"]]
        if hits:
            out.append({"category": label, "count": len(hits), "examples": hits[:3]})
    return out


def main():
    wb = openpyxl.load_workbook(RAW / "Southwest_MCI_Disruption_Recovery_Dataset.xlsx", data_only=True)
    aircraft = {r["Aircraft ID"]: r for r in rows(wb["Aircraft"])}
    crew = {r["Crew ID"]: r for r in rows(wb["Crew"])}
    pax = {r["Aircraft"]: r for r in rows(wb["Passenger Recovery"])}
    later = rows(wb["Later Flight Capacity"])
    reserves = rows(wb["Reserve Crews"])
    hotels = rows(wb["Hotel Inventory"])

    flights, conflicts = [], []
    for s in load_schedule():
        a, c, p = aircraft.get(s["aircraft"], {}), crew.get(s["crew"], {}), pax.get(s["aircraft"], {})
        f = {
            **s,
            "id": f"WN{1000 + s['n']}",
            "fuelPct": a.get("Fuel %"),
            "maint": a.get("Maintenance Status"),
            "maintDue": a.get("Maintenance Deadline"),
            "connecting": p.get("Connecting Pax", 0),
            "um": p.get("Unaccompanied Minors", 0),
            "wheelchair": p.get("Wheelchair/Service", 0),
            "intl": p.get("International Pax", 0),
            "hotelRooms": p.get("Hotel Rooms Est.", 0),
            "laterSeats": p.get("Seats on Later Flights", 0),
            "pilotHrs": c.get("Pilot Duty Hrs Remaining"),
            "faHrs": c.get("FA Duty Hrs Remaining"),
            "minRest": c.get("Minimum Rest Hrs"),
            "crewBase": c.get("Crew Base"),
            "legalityRisk": c.get("Legality Risk"),
        }
        flights.append(f)
        # Data-quality checks (the Guardrail agent's job)
        if p and p.get("Destination") != s["dest"]:
            conflicts.append({"item": s["aircraft"], "issue": f"Destination is {s['dest']} in the schedule but {p.get('Destination')} in the recovery dataset"})
        if p and p.get("Booked Pax") != s["pax"]:
            conflicts.append({"item": s["aircraft"], "issue": f"{s['pax']} passengers in the schedule vs {p.get('Booked Pax')} in the recovery dataset"})
        if a and a.get("Type") != f["type"]:
            conflicts.append({"item": s["aircraft"], "issue": f"Aircraft type {f['type']} in the schedule vs {a.get('Type')} in the dataset"})
        if c and (c.get("Minimum Rest Hrs") or 99) < 10:
            conflicts.append({"item": s["crew"], "issue": f"Minimum rest listed as {c['Minimum Rest Hrs']} h, below the 10 h legal minimum for pilots"})
        if not s["priority"]:
            conflicts.append({"item": f["id"], "issue": "No priority (P1-P4) assigned"})
        if p and p.get("Unaccompanied Minors", 0) and p.get("International Pax", 0) == p.get("Booked Pax"):
            conflicts.append({"item": f["id"], "issue": "Children flying alone listed on an international flight; Southwest doesn't offer that service"})

    data = {
        "note": "SIMULATED classroom data. Not real Southwest Airlines data.",
        "flights": flights,
        "laterCapacity": later,
        "reserves": reserves,
        "hotels": hotels,
        "conflicts": summarize(conflicts),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1, default=str))
    print(f"{len(flights)} flights, {len(conflicts)} data conflicts in {len(data['conflicts'])} groups -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
