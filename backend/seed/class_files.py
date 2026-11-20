"""Read the class files in data/raw into plain Python lists and dicts.

Nothing here touches the database; it only reads and tidies the files.
"""
import re

import docx
import openpyxl

from backend.settings import RAW

DATASET = RAW / "Southwest_MCI_Disruption_Recovery_Dataset.xlsx"
SCHEDULE = RAW / "Southwest_MCI_Daily_Operating_Schedule.docx"
TIERS = RAW / "Southwest_flights_tiers.xlsx"
TRANSPORT = RAW / "Transportation_hotels.xlsx"


def _rows(ws):
    it = ws.iter_rows(values_only=True)
    head = [h.strip() if isinstance(h, str) else h for h in next(it)]
    return [dict(zip(head, r)) for r in it if any(v is not None for v in r)]


def _minutes(text: str, pm: bool) -> int:
    h, m = map(int, re.findall(r"\d+", text)[:2])
    if pm and h < 12:
        h += 12
    return h * 60 + m


def read_schedule() -> list[dict]:
    """The 67 departures from the Daily Operating Schedule (times in minutes after midnight)."""
    table = docx.Document(SCHEDULE).tables[0]
    out, pm = [], False
    for r in table.rows[1:]:
        c = [x.text.strip() for x in r.cells]
        if not c[0].isdigit():
            continue
        if "PM" in c[1]:
            pm = True
        kind, ac = c[3].rsplit(" ", 1)
        dest_words, next_words = c[2].split(), c[6].split()
        out.append({
            "n": int(c[0]),
            "dep_min": _minutes(c[1], pm),
            "dest": dest_words[-1],
            "dest_name": " ".join(dest_words[:-1]),
            "aircraft": ac,
            "type": "737 MAX 8" if kind.strip() == "MAX 8" else kind.strip(),
            "pax": int(c[4]),
            "crew": c[5],
            "next_dest": next_words[-1],
            "next_dest_name": " ".join(next_words[:-1]) or None,
            "priority": c[7] or None,
        })
    return out


def read_dataset() -> dict:
    wb = openpyxl.load_workbook(DATASET, data_only=True)
    return {
        "aircraft": _rows(wb["Aircraft"]),
        "crew": _rows(wb["Crew"]),
        "passengers": _rows(wb["Passenger Recovery"]),
        "later": _rows(wb["Later Flight Capacity"]),
        "reserves": _rows(wb["Reserve Crews"]),
        "hotels": _rows(wb["Hotel Inventory"]),
    }


# The tiers file has a few typos; we correct them and record each one as a data issue.
TIER_TYPOS = {"MDM": "MDW", "BOX": "BOS", "PhX": "PHX"}


def read_tiers() -> tuple[dict, dict, list[str]]:
    """Returns (region by airport, tier by airport, list of typo corrections made)."""
    ws = openpyxl.load_workbook(TIERS, data_only=True).worksheets[0]
    regions, tiers, fixes = {}, {}, []
    current_tier = None
    for r in ws.iter_rows(values_only=True):
        cells = [c for c in r if c is not None]
        if not cells:
            continue
        label = str(cells[0])
        if label.startswith("Tier"):
            current_tier = int(label.split()[1])
            continue
        codes = [str(c) for c in cells[1:]]
        for code in codes:
            fixed = TIER_TYPOS.get(code, code)
            if fixed != code:
                fixes.append(f"{code} -> {fixed}")
            if current_tier:
                tiers[fixed] = current_tier
            elif len(fixed) == 3:
                regions[fixed] = label.replace("Sout ", "South ").strip()
    return regions, tiers, fixes


def read_transport() -> tuple[list[dict], list[dict]]:
    """Van / rideshare rates and hotel rate tiers from the Transportation & hotels file."""
    ws = openpyxl.load_workbook(TRANSPORT, data_only=True).worksheets[0]
    rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
    transport, hotel_tiers, radius = [], [], None
    for r in rows:
        label = str(r[0]).strip() if r[0] else ""
        if "Van Rental" in label or "van rental" in label:
            cap = 16 if label.startswith("16") else 8
            transport.append({"name": f"{cap}-passenger van with driver", "capacity": cap, "hourly": r[2], "per_trip": None})
        elif label.startswith("Uber XL"):
            transport.append({"name": "Uber XL (contracted, up to 10 miles)", "capacity": 5, "hourly": None, "per_trip": r[3]})
        elif "radius" in label or "raidus" in label:
            radius = int(re.findall(r"(\d+) mile", label)[0])
        elif label.startswith("Hotels") and r[4] is not None:
            letters = [x.upper() for x in re.findall(r"\b([A-Za-z])\b", label.split("-")[0].replace("Hotels", ""))]
            kind = "Luxury" if "Luxury" in label else "Moderate" if "Moderate" in label else "Economy"
            hotel_tiers.append({
                "tier": f"{kind}, {radius} mi ({', '.join(letters)})",
                "radius": radius,
                "count": len(letters),
                "rate": r[4],
                "for": re.sub(r"^(Luxury|Moderate|Economy)\W*", "", label.split("-", 1)[1].strip(" -")).replace("Busienss", "Business"),
            })
    return transport, hotel_tiers
