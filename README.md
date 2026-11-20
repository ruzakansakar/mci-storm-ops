# MCI Ground-Stop Co-Pilot

A class project: an AI operations co-pilot for Southwest Airlines at Kansas City (MCI) during any ground stop
(weather, FAA outage, IT outage, airport or security event).

Southwest's own systems stay the official record. The co-pilot keeps a time-stamped copy (the **shared live view**),
checks that the app, texts, gate screens and call center all tell each passenger the same thing, and proposes
actions for humans to approve.

All passenger and staff data here is **synthetic**, generated from the class files.

## Build phases

| Phase | What | Status |
|---|---|---|
| 1 | Database + data from the class files | **done** |
| 2 | Rules as tested code (`config/rules.yaml`) | **done** |
| 3 | Simulator + debug page (+ realism and rules cleanup) | **done** |
| 4 | Orchestrator + Situation agent | |
| 5 | Guardrail + Network Recovery (milestone 1) | |
| 6 | Communication, consistency check, Passenger Care, Station & Staff (milestone 2) | |
| 7 | API + live updates | |
| 8 | Front end (milestone 3) | |
| 9 | AI pieces | |
| 10 | Scale tests, numbers, demo | |

## Set up (VS Code, no Docker)

1. Make a free database at [neon.tech](https://neon.tech). Copy its connection string (starts with `postgresql://`).
2. In VS Code: **Terminal → New Terminal**, then:

```bash
git clone https://github.com/ruzakansakar/mci-storm-ops.git
cd mci-storm-ops
cp .env.example .env          # Windows PowerShell: copy .env.example .env
```

3. Open `.env` and paste your Neon string after `DATABASE_URL=`. Never commit `.env` (it's already in `.gitignore`).
4. Install and load:

```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m scripts.reset_db    # builds every table and loads the data (numbers come from config/seed.yaml)
python -m scripts.summary     # plain-English check
python -m scripts.rules_demo  # the rules in plain English + the A31 example
pytest                        # automated checks (the simulator ones take ~40 s)
```

## What Phase 1 gives you

- **67 departures** from the class schedule (`WN1001`–`WN1067`) and **56 generated inbound flights** (`WN2xxx`).
  The slides say 11 aircraft overnight at MCI, so the first 11 departures have no inbound leg.
- **A31 from Denver** (`WN2031`): 158 passengers, 42 connecting, 3 wheelchair, crew with 4 h 20 min of duty left (from the slides).
- **Passenger groups** for every flight (child alone, wheelchair, international, connecting, standard) that add up exactly to the flight total.
- **~320 sample passengers** you can follow one by one, with bookings, bags and what each of 4 screens shows them.
  Example: Maya Torres, landing on A31 and connecting to Baltimore.
- Crews and reserve crews, hotels, later-flight capacity, route tiers, van and hotel rates, and an MCI staff roster.
- Empty tables ready for later phases: events, incidents, recommendations, decisions, audit log, mismatches, metric runs.
- **`data_issues`**: every conflict between the class files, recorded with what we did about it (nothing silently fixed).

## The rules (Phase 2)

Every number the agents use lives in `config/rules.yaml`: the safety ladder, legal limits,
who approves what, and compensation. Change a number there and rerun `pytest`.

- **Safety ladder:** life, aircraft, infrastructure, legal, vulnerable passengers (these remove unsafe options),
  then network, convenience, cost (these only rank what's left). Unknown data counts as unsafe.
- **Who decides:** `auto`, `needs_approval` (with the approver's role) or `human_only`. Anything not listed is human-only,
  and nothing that touches rungs 1-4 can be automatic.
- **Children flying alone** (Southwest unaccompanied minor policy): ages 5-11, nonstop or same plane only, never international.
  A rebooking that puts a child on a connection is removed; the international check reads this section of `rules.yaml`.
- **Tarmac food and water** (law, 14 CFR 259.4): an option with 2+ hours on the tarmac is removed unless it has a food-and-water plan.
- **Delayed bags** (law, DOT refund rule, 14 CFR 260.5): the checked-bag fee is refunded if a bag is late by more than 12 h (domestic),
  or 15 / 30 h (international, by flight length). Southwest charges for checked bags ($45 first, $55 second since 9 Apr 2026;
  sources are in `rules.yaml`); `bag_fee_usd` is an assumption.
- **Every rule has a source note** in `rules.yaml`: `law` (with the regulation), `southwest` (with its basis) or `assumption`.
  A test fails if a rule has none. Legal and safety numbers live only in `rules.yaml`; the simulator reads them from that file
  (as plain YAML), and a test fails if `simulator.yaml` defines a key that `rules.yaml` already owns.
- **Fuel:** `aircraft.fuel_minutes_extra` is spare *holding* fuel above the legal reserve (fuel to reach the alternate plus
  45 min, 14 CFR 121.639), which is already on board and not counted. A plan is unsafe if
  `fuel_minutes_extra - hold_minutes - minutes_to_alternate` is below `limits.fuel_buffer_min` (10 min). Unknown fuel counts as unsafe.
  A31 example (55 min): hold 45 min is kept (10 left), hold 30 then divert to St. Louis with 15 min extra flying is kept (10 left),
  hold 50 min is removed (5 left), Tulsa is removed (no gate). The simulator reads the same `fuel_buffer_min` from `rules.yaml` as the point where a holding plane
  diverts (it is not repeated in `simulator.yaml`).
- **Compensation:** legal floor for everyone (refund if cancelled or 3+ h late, 6+ h international, and rebooking declined),
  Southwest commitments only for controllable causes (meal + $75 voucher at 3+ h, hotel if overnight and not local),
  optional goodwill that a supervisor approves.

## The simulator (Phase 3)

```bash
python -m simulator.run --seed 7 --scenario a31_storm     # the class story; resets the database first
python -m simulator.run --seed 7 --scenario random --mode today --until 23:59
python -m simulator.report --latest                       # writes reports/<run_id>.html (double-click to open)
python -m simulator.report --run-id a31_storm-s7-today
```

`--mode copilot` exists but only prints "not built yet (Phase 4+)". A full day takes about 10 seconds on Neon.
All numbers are in `config/simulator.yaml` (made-up ones are marked `# assumption`).

**How it works, in plain English**

1. **A script for the day.** From the seed it picks what goes wrong: a cause (storm, FAA program, IT outage, airport or
   security problem), when the stop starts and how long it lasts, and sometimes side problems (lightning closes the ramp,
   a crew nearly out of hours, an aircraft needing inspection, a crowd building in the terminal).
   `a31_storm` is the fixed class story: storm stop 11:30-14:00 (first announced to end at 13:00), lightning closes the ramp
   12:10-12:45, and A31 is airborne from Denver with 55 min of spare fuel.
2. **The truth.** Minute by minute the simulator moves flights, planes, crews, bags and bookings, the way Southwest's systems would
   record them *today*, with no co-pilot: flights wait out the stop, a flight that would leave more than 150 min late is
   cancelled, a plane diverts once its spare holding fuel minus the extra flying to its alternate airport falls to 10 min (the same sum as the rules' fuel check), and people on a cancelled flight get the next flight
   to the same place that has room (or are left with no seat). It makes no clever choices.
3. **The screens.** The app, text messages, gate screens and call center each copy the truth later than it happened:
   app fast, call center slowest, and slower still when many things change at once. Sometimes a copy never arrives, and that
   screen stays wrong until the next change. A screen never shows the right answer early.
4. **What gets saved.** Every event (append-only; the database refuses edits), every truth change in the audit log, one
   incident, and at the end one row of numbers in `metric_runs` (delays, cancellations, passengers hit, how many people had a wrong screen,
   for how long, and so on). Nothing is written to `mismatches`: finding mismatches is a later phase. `count_disagreements()`
   in `simulator/channels.py` measures them.
5. **Same seed, same day.** Only seeded random numbers are used, so the same seed and scenario always give the same events and numbers.

**A more realistic storm.** Today's process (no co-pilot) now also has the usual knock-on effects, so the class story produces
cancellations naturally instead of only delays:
- **Schedule thinning:** when a ground stop of 60+ minutes is announced, the lowest-priority departures (P4 first, then P3) scheduled
  during the stop and up to 2 hours after it are cancelled, up to a cap.
- **Crew hours:** waiting burns crew duty (starting from the hours in the class dataset). A crew with too little duty left for its flight
  plus the rest of its day (plus the buffer in `rules.yaml`) is swapped for a reserve crew from the Reserve Crews data if one is free,
  available in time and long enough on duty; otherwise the flight is cancelled ("crew out of legal duty").
- **Aircraft out of position:** a diverted aircraft that can't get back in time delays its next flight, and cancels it past the cutoff.
- **Stranded overnight:** if no seat is left today (rebooking never fills a plane past 93%), passengers go on a next-day flight on
  7 Oct, are marked `overnight` in `passengers`, and we record whether they live locally. No hotel or money decisions are made here.
- A test checks that across seeds 1-20, every storm or ground stop of 2+ hours cancels between 5% and 25% of the flights at MCI
  (arrivals and departures) in the window from the start of the stop to 2 hours after it ends.

The simulator never imports `backend/rules`: it only causes problems, it does not decide anything.

## Rules for conflicting files

- The Daily Operating Schedule wins for times, destinations, aircraft type, passenger totals and priority.
- The recovery dataset wins for crew hours, maintenance and special-needs counts.
- The slides win for the A31 story numbers.
- Anything we had to assume (flight times, inbound origins, station facilities) is marked `source = 'assumption'` or `'generated'`.

## Folders

```
backend/db/schema.sql      every table
backend/seed/class_files.py reads the class files in data/raw
backend/seed/build.py      turns them into rows (deterministic by SEED)
backend/seed/load.py       writes the rows into PostgreSQL
backend/rules/             safety ladder, who decides, compensation
config/rules.yaml          every rule number
config/simulator.yaml      every simulator number
config/seed.yaml           every number used to build the starting data
simulator/                 scenario, world (truth), channels (screens), run, report (debug page)
reports/                   generated debug pages (not committed)
scripts/reset_db.py        wipe and rebuild
scripts/summary.py         human-readable check
tests/                     pytest checks
data/raw/                  the class files
```
