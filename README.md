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
| 3 | Simulator + debug page | **done** |
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
python -m scripts.reset_db    # builds every table and loads the data
python -m scripts.summary     # plain-English check
python -m scripts.rules_demo  # the rules in plain English + the A31 example
pytest                        # 56 automated checks (the simulator ones take ~20 s)
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
- **Fuel:** `aircraft.fuel_minutes_extra` is spare *holding* fuel above the legal reserve (fuel to reach the alternate plus
  45 min, 14 CFR 121.639), which is already on board and not counted. A plan is unsafe if
  `fuel_minutes_extra - hold_minutes - minutes_to_alternate` is below `limits.fuel_buffer_min` (10 min). Unknown fuel counts as unsafe.
  A31 example (55 min): hold 45 min is kept (10 left), hold 30 then divert to St. Louis with 15 min extra flying is kept (10 left),
  hold 50 min is removed (5 left), Tulsa is removed (no gate). The simulator uses the same 10 min as the point where a holding plane
  diverts (`diversion.fuel_floor_min` in `config/simulator.yaml`); a test keeps the two numbers equal.
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
   cancelled, a plane that burns its spare fuel while holding diverts, and people on a cancelled flight get the next flight
   to the same place that has room (or are left with no seat). It makes no clever choices.
3. **The screens.** The app, text messages, gate screens and call center each copy the truth later than it happened:
   app fast, call center slowest, and slower still when many things change at once. Sometimes a copy never arrives, and that
   screen stays wrong until the next change. A screen never shows the right answer early.
4. **What gets saved.** Every event (append-only; the database refuses edits), every truth change in the audit log, one
   incident, and at the end one row of numbers in `metric_runs` (delays, cancellations, passengers hit, how many people had a wrong screen,
   for how long, and so on). Nothing is written to `mismatches`: finding mismatches is a later phase. `count_disagreements()`
   in `simulator/channels.py` measures them.
5. **Same seed, same day.** Only seeded random numbers are used, so the same seed and scenario always give the same events and numbers.

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
simulator/                 scenario, world (truth), channels (screens), run, report (debug page)
reports/                   generated debug pages (not committed)
scripts/reset_db.py        wipe and rebuild
scripts/summary.py         human-readable check
tests/                     pytest checks
data/raw/                  the class files
```
