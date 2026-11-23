"""Build every row for the shared live view from the class files.

Everything is deterministic: the same SEED always gives exactly the same data.
Rules for conflicting files (decided with the project owner):
  * the Daily Operating Schedule wins for times, destinations, aircraft type, passenger totals and priority
  * the recovery dataset wins for crew hours, maintenance and special-needs counts
  * conflicts are recorded in data_issues, never silently fixed
"""
from backend.screens import screens_for
import random
import string
from datetime import datetime, time, timedelta

import yaml

from backend.seed import class_files as cf
from backend.settings import ROOT, SAMPLE_PASSENGERS, SEED, SIM_DATE, TZ, read_limits

# Every number comes from config/seed.yaml; legal and safety numbers come from config/rules/.
CFG = yaml.safe_load((ROOT / "config" / "seed.yaml").read_text(encoding="utf-8"))
LIMITS = read_limits()


def _hhmm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)

# ---------------------------------------------------------------- constants and assumptions

AS_OF = datetime.combine(SIM_DATE, time(0, 0), TZ) + timedelta(minutes=_hhmm(CFG["day_start"]))   # start of the simulated day

SOR = {  # which official system owns each kind of fact (simulated stand-ins)
    "flights": "Amadeus Altea (simulated)",
    "passengers": "Amadeus Altea (simulated)",
    "crews": "Crew Optimization (simulated)",
    "aircraft": "Maintenance tracking (simulated)",
    "bags": "Baggage system (simulated)",
    "hotels": "Hotel vendor (simulated)",
    "stations": "Airport operations (simulated)",
    "staff": "Station staffing (simulated)",
    "capacity": "Revenue management (simulated)",
}

SEATS = CFG["aircraft"]["seats"]
BASES = set(CFG["bases"])
DIVERSION_OPTIONS = set(CFG["diversion_options"])
OVERNIGHT_AIRCRAFT = CFG["aircraft"]["overnight_count"]
TURN_MIN = CFG["turn_min"]
MIN_CONNECT_MIN = LIMITS["min_connection_min"]                         # a rule: lives in config/rules/
MAX_CONNECT_MIN = CFG["max_connection_min"]

EXTRA_NAMES = {
    "MCI": "Kansas City", "ICT": "Wichita", "TUL": "Tulsa", "OMA": "Omaha", "DSM": "Des Moines",
    "STL": "St. Louis", "MSP": "Minneapolis", "MKE": "Milwaukee", "IND": "Indianapolis",
    "CMH": "Columbus", "BNA": "Nashville", "HOU": "Houston Hobby", "AUS": "Austin",
    "SAT": "San Antonio", "ABQ": "Albuquerque", "SLC": "Salt Lake City", "SFO": "San Francisco",
    "OAK": "Oakland", "SMF": "Sacramento", "SEA": "Seattle", "BOS": "Boston", "DCA": "Washington Reagan",
    "ORF": "Norfolk", "FLL": "Fort Lauderdale", "MIA": "Miami", "JAX": "Jacksonville",
    "SAV": "Savannah", "SJD": "Los Cabos", "PVR": "Puerto Vallarta", "MBJ": "Montego Bay",
    "PUJ": "Punta Cana", "PVD": "Providence", "MDW": "Chicago Midway", "LGA": "New York LaGuardia",
}

BLOCK_MIN = CFG["block_min_by_region"]

# Simulated facilities at stations that matter for diversions (assumptions, labelled as such in seed.yaml).
STATION_FACILITIES = CFG["station_facilities"]

FIRST = ["Maya", "Liam", "Olivia", "Noah", "Emma", "Elijah", "Ava", "James", "Sophia", "Lucas", "Isabella",
         "Mateo", "Mia", "Aiden", "Amelia", "Ethan", "Harper", "Daniel", "Evelyn", "Leo", "Aria", "Omar",
         "Priya", "Kenji", "Fatima", "Diego", "Grace", "Samuel", "Chloe", "Ravi", "Zoe", "Andre", "Nora",
         "Hassan", "Lena", "Marcus", "Ivy", "Tomas", "Ruth", "Jamal"]
LAST = ["Torres", "Nguyen", "Smith", "Johnson", "Garcia", "Brown", "Patel", "Kim", "Martinez", "Davis",
        "Lopez", "Wilson", "Anderson", "Thomas", "Moore", "Jackson", "Lee", "Harris", "Clark", "Lewis",
        "Walker", "Hall", "Young", "Khan", "Okafor", "Silva", "Rossi", "Novak", "Haddad", "Larsen"]

SEGMENTS = ["child_alone", "wheelchair", "international", "connecting", "standard"]


def at(minutes: int) -> datetime:
    """A time on the simulated day from minutes after midnight (can roll past midnight)."""
    return datetime.combine(SIM_DATE, time(0, 0), TZ) + timedelta(minutes=minutes)


def build() -> dict:
    rng = random.Random(SEED)
    sched = cf.read_schedule()
    ds = cf.read_dataset()
    regions, tiers, tier_fixes = cf.read_tiers()
    transport, hotel_tiers = cf.read_transport()
    issues: list[dict] = []

    def issue(category, item, text, resolution, severity="warning"):
        issues.append({"category": category, "item": item, "issue": text,
                       "resolution": resolution, "severity": severity})

    ac_by_id = {r["Aircraft ID"]: r for r in ds["aircraft"]}
    crew_by_id = {r["Crew ID"]: r for r in ds["crew"]}
    pax_by_ac = {r["Aircraft"]: r for r in ds["passengers"]}

    # ------------------------------------------------------------ stations
    names = dict(EXTRA_NAMES)
    for s in sched:
        names.setdefault(s["dest"], s["dest_name"])
        if s["next_dest_name"]:
            names.setdefault(s["next_dest"], s["next_dest_name"])
    codes = {"MCI"} | {s["dest"] for s in sched} | {s["next_dest"] for s in sched} | set(regions) | set(tiers)
    codes |= DIVERSION_OPTIONS
    for r in ds["aircraft"]:
        codes |= {r[f"Next Leg {i}"] for i in range(1, 5) if r[f"Next Leg {i}"]}
    stations = []
    for code in sorted(codes):
        fac = STATION_FACILITIES.get(code, {})
        stations.append({
            "code": code, "name": names.get(code, code), "region": regions.get(code),
            "is_base": code in BASES, "is_diversion_option": code in DIVERSION_OPTIONS,
            "gates": fac.get("gates"), "wheelchair_staff": fac.get("wheelchair_staff"),
            "has_customs": fac.get("has_customs"), "terminal_capacity": fac.get("terminal_capacity"),
            "people_in_terminal": 0 if code == "MCI" else None, "ramp_open": True,
            "source": "assumption" if fac else "class_files", "system_of_record": SOR["stations"], "as_of": AS_OF,
        })

    # ------------------------------------------------------------ outbound flights (the schedule)
    by_time = sorted(sched, key=lambda s: s["dep_min"])
    overnight = {s["aircraft"] for s in by_time[:OVERNIGHT_AIRCRAFT]}
    flights, groups, aircraft, crews = [], [], [], []

    def block(code):
        return BLOCK_MIN.get(regions.get(code), CFG["default_block_min"])

    for s in sched:
        fid = f"WN{1000 + s['n']}"
        priority = s["priority"]
        if not priority:
            priority = CFG["missing_priority"]
            issue("missing_priority", fid, f"Flight {s['n']} has no priority in the schedule",
                  f"Set to {CFG['missing_priority']} (consolidation candidate), as agreed")
        flights.append({
            "id": fid, "direction": "departure", "schedule_no": s["n"], "origin": "MCI",
            "destination": s["dest"], "sched_dep": at(s["dep_min"]),
            "sched_arr": at(s["dep_min"] + block(s["dest"])), "aircraft_id": s["aircraft"],
            "crew_id": s["crew"], "next_destination": s["next_dest"], "priority": priority,
            "pax_total": s["pax"], "source": "class_schedule",
        })
        d = pax_by_ac.get(s["aircraft"])
        if d:
            if d["Booked Pax"] != s["pax"]:
                issue("pax_count", fid, f"{s['pax']} passengers in the schedule vs {d['Booked Pax']} in the dataset",
                      "Schedule total used; dataset special-needs counts kept", "info")
            if d["Departure"] and abs(_hhmm(d["Departure"]) - s["dep_min"]) > 0:
                issue("departure_time", fid, f"Departs {_fmt(s['dep_min'])} in the schedule vs {d['Departure']} in the dataset",
                      "Schedule time used", "info")
            if d.get("Operational Priority") and s["priority"] and d["Operational Priority"] != s["priority"]:
                issue("priority", fid, f"Priority {s['priority']} in the schedule vs {d['Operational Priority']} in the dataset",
                      "Schedule priority used", "info")
            if s["dest"] == "CUN" and d["Unaccompanied Minors"]:
                issue("child_alone_international", fid,
                      f"{d['Unaccompanied Minors']} children flying alone listed on the Cancun flight; Southwest doesn't offer that service internationally",
                      "Kept on purpose so the Guardrail can catch it later")
        groups += _split_groups(fid, s["pax"], d, "class_dataset")

    # ------------------------------------------------------------ inbound flights (generated)
    hubs = [c for c, t in tiers.items() if t in (1, 2) and c in names and c != "MCI"]
    inbound_by_ac = {}
    for s in sched:
        if s["aircraft"] in overnight:
            continue
        fid = f"WN{2000 + s['n']}"
        a31 = CFG["a31"]
        origin = a31["inbound_origin"] if s["aircraft"] == "A31" else rng.choice(hubs)
        arr = s["dep_min"] - TURN_MIN
        pax = a31["pax"] if s["aircraft"] == "A31" else rng.randint(*CFG["inbound"]["pax_range"])
        crew_id = None
        if s["aircraft"] == "A31":
            c = a31["crew"]
            crew_id = c["id"]
            crews.append({
                "id": c["id"], "kind": "full_crew", "pilots": c["pilots"], "flight_attendants": c["flight_attendants"],
                "base": c["base"], "current_station": c["base"], "is_reserve": False, "reserve_status": None,
                "available_from": None, "pilot_duty_left_min": c["duty_left_min"], "fa_duty_left_min": c["duty_left_min"],
                "min_rest_hours": c["min_rest_hours"], "legality_risk": c["legality_risk"],
                "hotel_if_cancelled": c["hotel_if_cancelled"], "deadhead_planned": c["deadhead_planned"],
                "restrictions": None, "source": "class_slides",
            })
        flights.append({
            "id": fid, "direction": "arrival", "schedule_no": s["n"], "origin": origin, "destination": "MCI",
            "sched_dep": at(arr - block(origin)), "sched_arr": at(arr), "aircraft_id": s["aircraft"],
            "crew_id": crew_id, "next_destination": s["dest"], "priority": flights[s["n"] - 1]["priority"],
            "pax_total": pax, "source": "class_slides" if s["aircraft"] == "A31" else "generated",
        })
        inbound_by_ac[s["aircraft"]] = origin
        if s["aircraft"] == "A31":
            fake = {"Connecting Pax": a31["connecting"], "Unaccompanied Minors": a31["minors"],
                    "Wheelchair/Service": a31["wheelchair"], "International Pax": a31["international"]}
            groups += _split_groups(fid, pax, fake, "class_slides")
        else:
            ib = CFG["inbound"]
            fake = {"Connecting Pax": round(pax * rng.uniform(*ib["connecting_fraction"])),
                    "Unaccompanied Minors": rng.choice(ib["minors_choices"]),
                    "Wheelchair/Service": rng.randint(*ib["wheelchair_range"]),
                    "International Pax": rng.randint(*ib["international_range"])}
            groups += _split_groups(fid, pax, fake, "generated")
    issue("inbound_flights", "56 inbound flights",
          "The schedule only lists departures, and the dataset puts every aircraft at MCI. But the slides say only 11 aircraft overnight at MCI and A31 arrives from Denver.",
          "The first 11 departures are treated as overnight aircraft; the other 56 get a generated inbound flight arriving 50 min before departure (A31 from Denver, per the slides)")

    # ------------------------------------------------------------ aircraft
    sched_by_ac = {s["aircraft"]: s for s in sched}
    for ac_id, s in sorted(sched_by_ac.items()):
        r = ac_by_id.get(ac_id, {})
        if r and r["Type"] != s["type"]:
            issue("aircraft_type", ac_id, f"{s['type']} in the schedule vs {r['Type']} in the dataset",
                  "Schedule type used", "info")
        aircraft.append({
            "id": ac_id, "type": s["type"], "seats": SEATS.get(s["type"], CFG["aircraft"]["default_seats"]),
            "current_station": inbound_by_ac.get(ac_id, "MCI"), "fuel_pct": r.get("Fuel %"),
            "maint_status": r.get("Maintenance Status") or "Routine",
            "maint_due_at": _due(r.get("Maintenance Deadline")),
            "next_legs": [r[f"Next Leg {i}"] for i in range(1, 5) if r.get(f"Next Leg {i}")],
            "recovery_priority": r.get("Recovery Priority"), "source": "class_dataset",
        })

    # ------------------------------------------------------------ crews (schedule crews + reserves)
    for cid, r in sorted(crew_by_id.items()):
        rest = r["Minimum Rest Hrs"]
        if rest is not None and rest < LIMITS["pilot_min_rest_hours"]:
            issue("crew_rest", cid, f"Minimum rest listed as {rest} h, below the {LIMITS['pilot_min_rest_hours']} h legal minimum rest "
                                    "for pilots (14 CFR 117.25(e)): a real legal problem",
                  "Kept as listed. The simulator flags it at 05:00 as crew_illegal_rest; the orchestrator's rule check blocks "
                  "any plan that uses this crew")
        crews.append({
            "id": cid, "kind": "full_crew", "pilots": CFG["crews"]["pilots"],
            "flight_attendants": CFG["crews"]["flight_attendants"], "base": r["Crew Base"],
            "current_station": r["Current Location"], "is_reserve": False, "reserve_status": None, "available_from": None,
            "pilot_duty_left_min": round(r["Pilot Duty Hrs Remaining"] * 60),
            "fa_duty_left_min": round(r["FA Duty Hrs Remaining"] * 60), "min_rest_hours": rest,
            "legality_risk": r["Legality Risk"], "hotel_if_cancelled": r["Hotel Needed if Cancelled"] == "Yes",
            "deadhead_planned": r["Deadhead Planned"] == "Yes", "restrictions": None, "source": "class_dataset",
        })
    c31 = crew_by_id.get("C31")
    if c31:
        issue("a31_slides_vs_data", "A31",
              f"Slides: A31 airborne from Denver with 158 passengers and 4 h 20 min of crew duty. Dataset: crew C31 has {c31['Pilot Duty Hrs Remaining']} pilot / {c31['FA Duty Hrs Remaining']} FA hours; schedule: A31 departs MCI for Denver at 12:35",
              "A31's inbound flight from Denver (WN2031) uses the slide numbers with its own crew CX31; the 12:35 departure (WN1031) keeps crew C31")
    for r in ds["reserves"]:
        pilots = "Pilot" in r["Resource Type"]
        mins = round(r["Duty Hrs Available"] * 60)
        crews.append({
            "id": r["Reserve ID"], "kind": "pilot_pair" if pilots else "fa_team",
            "pilots": CFG["crews"]["pilots"] if pilots else 0,
            "flight_attendants": 0 if pilots else CFG["crews"]["flight_attendants"], "base": r["Location"],
            "current_station": r["Location"], "is_reserve": True, "reserve_status": r["Status"],
            "available_from": at(_hhmm(r["Available Time"])),
            "pilot_duty_left_min": mins if pilots else None, "fa_duty_left_min": None if pilots else mins,
            "min_rest_hours": None, "legality_risk": None, "hotel_if_cancelled": None, "deadhead_planned": None,
            "restrictions": None if r["Constraint / Skill"] == "None" else [r["Constraint / Skill"]],
            "source": "class_dataset",
        })

    # ------------------------------------------------------------ hotels, capacity, tiers, transport
    hotels = [{
        "id": f"H{i + 1}", "name": r["Hotel"], "station": "MCI", "rooms_total": r["Total Rooms"],
        "rooms_sold": r["Rooms Sold"], "rooms_available": r["Rooms Available"], "rate_usd": r["Disruption Rate $"],
        "distance_miles": r["Miles from MCI"], "shuttle": r["Shuttle"], "bus_capacity": r["Bus Capacity per Trip"],
        "last_checkin": r["Last Check-in"], "use_preference": r["Use Preference"], "source": "class_dataset",
    } for i, r in enumerate(ds["hotels"])]
    issue("hotel_tiers_wording", "Transportation & hotels file",
          "Hotel tiers mention First Class and Business Class travelers; Southwest has no first or business class",
          "Loaded as written; 'premium' passengers should be defined by fare or loyalty tier instead", "info")
    later = [{
        "destination": r["Destination"], "scheduled_flights": r["Scheduled MCI Flights"],
        "later_flights": r["Later Flights Remaining"], "open_seats_later": r["Total Open Seats Later"],
        "next_later_departure": r["Next Later Departure"], "connection_alternative": r["Connection Alternative"],
        "recovery_capacity": r["Recovery Capacity"], "source": "class_dataset",
    } for r in ds["later"]]
    route_tiers = [{"destination": c, "region": regions.get(c), "tier": t, "source": "class_tiers"} for c, t in sorted(tiers.items())]
    for fix in tier_fixes:
        issue("tier_typo", fix.split(" ")[0], f"Airport code typo in the tiers file: {fix}", "Corrected", "info")
    issue("simulation_day", "Calendar",
          "The dataset and schedule are dated Tuesday, Oct 6, 2026; the slides describe the storm on 'Day 2, Wednesday'",
          f"All times use {SIM_DATE.isoformat()} (the dataset date)", "info")

    # ------------------------------------------------------------ individual passengers, bookings, bags
    passengers, bookings, bags, views = _sample_passengers(rng, flights, groups)

    # ------------------------------------------------------------ staff at MCI
    staff = _staff(rng)

    return {
        "stations": stations, "aircraft": aircraft, "crews": crews, "flights": flights,
        "passenger_groups": groups, "passengers": passengers, "bookings": bookings, "bags": bags,
        "channel_views": views, "hotels": hotels, "staff": staff, "later_capacity": later,
        "route_tiers": route_tiers, "transport": transport, "hotel_tiers": hotel_tiers, "data_issues": issues,
    }


# ---------------------------------------------------------------- helpers

def _hhmm(text: str) -> int:
    h, m = map(int, str(text).split(":")[:2])
    return h * 60 + m


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _due(text):
    if not text:
        return None
    md, hm = str(text).split()
    month, day = map(int, md.split("/"))
    h, m = map(int, hm.split(":"))
    return datetime(SIM_DATE.year, month, day, h % 24, m, tzinfo=TZ)


def _split_groups(fid: str, total: int, d: dict | None, source: str) -> list[dict]:
    """Split a flight's passengers into exclusive segments that add up exactly to the total."""
    d = d or {}
    wanted = [
        ("child_alone", d.get("Unaccompanied Minors") or 0),
        ("wheelchair", d.get("Wheelchair/Service") or 0),
        ("international", d.get("International Pax") or 0),
        ("connecting", d.get("Connecting Pax") or 0),
    ]
    out, left = [], total
    for seg, n in wanted:
        n = max(0, min(int(n), left))
        out.append(n)
        left -= n
    out.append(left)
    return [{"flight_id": fid, "segment": seg, "count": n, "source": source} for seg, n in zip(SEGMENTS, out)]


def _sample_passengers(rng: random.Random, flights: list[dict], groups: list[dict]):
    """Create individual passengers sampled from the groups, so they follow each flight's real mix."""
    pools = {}
    for g in groups:
        pools.setdefault(g["flight_id"], {})[g["segment"]] = g["count"]
    fl = {f["id"]: f for f in flights}
    arrivals = sorted((f for f in flights if f["direction"] == "arrival"), key=lambda f: f["sched_arr"])
    departures = sorted((f for f in flights if f["direction"] == "departure"), key=lambda f: f["sched_dep"])

    passengers, bookings, bags, views = [], [], [], []
    used_refs = set()

    def take(fid, seg):
        if pools[fid].get(seg, 0) > 0:
            pools[fid][seg] -= 1
            return True
        return False

    def connection_from(arr):
        ok = [d for d in departures
              if timedelta(minutes=MIN_CONNECT_MIN) <= d["sched_dep"] - arr["sched_arr"] <= timedelta(minutes=MAX_CONNECT_MIN)
              and pools[d["id"]]["connecting"] > 0]
        return rng.choice(ok) if ok else None

    def connection_to(dep):
        ok = [a for a in arrivals
              if timedelta(minutes=MIN_CONNECT_MIN) <= dep["sched_dep"] - a["sched_arr"] <= timedelta(minutes=MAX_CONNECT_MIN)
              and pools[a["id"]]["connecting"] > 0]
        return rng.choice(ok) if ok else None

    def add(name, seg, itinerary, final, local=False):
        pid = f"PAX{len(passengers) + 1:04d}"
        ref = ""
        while not ref or ref in used_refs:
            ref = "".join(rng.choices(string.ascii_uppercase + string.digits, k=6))
        used_refs.add(ref)
        pc = CFG["passengers"]
        contacts = rng.choices([c["channels"] for c in pc["contact_channels"]],
                               weights=[c["weight"] for c in pc["contact_channels"]])[0]
        # story override, applied after the random draw so the random stream (and every other passenger) is unchanged
        contacts = list((pc.get("story_overrides") or {}).get(name, {}).get("contact_channels", contacts))
        loyalty = rng.choices(list(pc["loyalty"]), weights=list(pc["loyalty"].values()))[0]
        passengers.append({
            "id": pid, "full_name": name, "booking_ref": ref, "segment": seg, "final_destination": final,
            "lives_locally": local, "loyalty_tier": loyalty, "contact_channels": contacts, "source": "generated",
        })
        for i, f in enumerate(itinerary, 1):
            bookings.append({"passenger_id": pid, "seq": i, "flight_id": f["id"], "source": "generated"})
        n_bags = 0 if seg == "child_alone" and rng.random() < pc["child_alone_no_bag_chance"] \
            else rng.choices(pc["bags"]["counts"], weights=pc["bags"]["weights"])[0]
        routing = [f["id"] for f in itinerary]
        my_bags = []
        for _ in range(n_bags):
            tag = f"WN{rng.randint(100000, 999999)}"
            my_bags.append(tag)
            bags.append({"tag": tag, "passenger_id": pid, "routing": routing,
                         "current_location": itinerary[0]["origin"], "source": "generated"})
        for ch in screens_for(contacts):                     # app/SMS only if on file; gate screen and call center for all
            views.append({"passenger_id": pid, "channel": ch, "shown_itinerary": routing, "shown_status": "confirmed",
                          "shown_destination": final, "shown_bag_routing": routing if my_bags else [],
                          "updated_at": AS_OF})

    # Maya: the example passenger used in the story (A31 inbound from Denver, connecting to Baltimore)
    a31_in = fl["WN2031"]
    bwi = next(d for d in departures if d["destination"] == "BWI" and d["sched_dep"] - a31_in["sched_arr"] >= timedelta(minutes=MIN_CONNECT_MIN))
    take("WN2031", "connecting"), take(bwi["id"], "connecting")
    add("Maya Torres", "connecting", [a31_in, bwi], "BWI")

    # How many individuals to sample from each flight (A31's inbound gets extra so the story has people to follow)
    total_pax = sum(f["pax_total"] for f in flights)
    quota = {f["id"]: max(1, round(f["pax_total"] * SAMPLE_PASSENGERS / total_pax)) for f in flights}
    quota["WN2031"] = CFG["passengers"]["a31_sample"]

    for f in sorted(flights, key=lambda f: f["id"]):
        for _ in range(quota[f["id"]] - (1 if f["id"] == "WN2031" else 0)):
            seg = rng.choices(SEGMENTS, weights=[max(pools[f["id"]][s], 0) for s in SEGMENTS])[0] \
                if sum(pools[f["id"]].values()) else None
            if seg is None:
                break
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if seg == "connecting":
                other = connection_from(f) if f["direction"] == "arrival" else connection_to(f)
                if other is None:
                    seg = "standard" if pools[f["id"]]["standard"] > 0 else None
                    if seg is None:
                        continue
                else:
                    take(f["id"], "connecting"), take(other["id"], "connecting")
                    itin = [f, other] if f["direction"] == "arrival" else [other, f]
                    add(name, "connecting", itin, itin[-1]["destination"])
                    continue
            take(f["id"], seg)
            if f["direction"] == "arrival":
                add(name, seg, [f], "MCI", local=rng.random() < CFG["passengers"]["lives_locally_chance"]["arrival"])
            else:
                add(name, seg, [f], f["destination"], local=rng.random() < CFG["passengers"]["lives_locally_chance"]["departure"])
    return passengers, bookings, bags, views


def _staff(rng: random.Random) -> list[dict]:
    roles = list(CFG["staff"]["roles"].items())
    starts = [_hhmm(x) for x in CFG["staff"]["shift_starts"]]
    out = []
    for role, n in roles:
        for i in range(n):
            start = at(rng.choice(starts))
            out.append({
                "id": f"{role[:3].upper()}{i + 1:03d}", "role": role, "station": "MCI",
                "shift_start": start, "shift_end": start + timedelta(minutes=CFG["staff"]["shift_length_min"]),
                "last_break": start, "source": "generated",
            })
    return out
