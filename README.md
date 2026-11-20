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
| 3 | Simulator + debug page | next |
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
pytest                        # 38 automated checks
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
- **Compensation:** legal floor for everyone (refund if cancelled or 3+ h late, 6+ h international, and rebooking declined),
  Southwest commitments only for controllable causes (meal + $75 voucher at 3+ h, hotel if overnight and not local),
  optional goodwill that a supervisor approves.

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
scripts/reset_db.py        wipe and rebuild
scripts/summary.py         human-readable check
tests/                     pytest checks
data/raw/                  the class files
```
