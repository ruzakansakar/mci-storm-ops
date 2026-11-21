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
| 2 | Rules as tested code (`config/rules/`) | **done** |
| 3b | Rules catalog + agent contracts (`config/rules/`, `config/agents/`) | **done** |
| 3c | Agreed setup, rule check, decision catalog, shared AI guardrails | **done** |
| 3 | Simulator + debug page (+ realism and rules cleanup) | **done** |
| 4a | Situation agent | **done** |
| 4b | Orchestrator | |
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

Every number the agents use lives in the folder `config/rules/`, one file per kind of rule. Change a number there and rerun `pytest`.

| File | What is in it |
|---|---|
| `law.yaml` | Federal law: pilot duty and rest (14 CFR 117, including the full Table B), flight attendant duty and rest (121.467), dispatchers (121.465), tarmac delays (259.4), fuel (121.639), airport curfews, customs, wheelchair rules (part 382) |
| `southwest.yaml` | Southwest policy and commitments: children flying alone, vouchers, bag fee, refunds, no rebooking on other airlines |
| `operations.yaml` | Our assumptions and airport/station facts: buffers, lightning and the ramp, ground staff hour limits, TSA and Southwest station hours |
| `safety_ladder.yaml` | The eight rungs |
| `autonomy.yaml` | Who decides what |

**Every rule has the same fields:** `id`, a plain description, `value`, `source` (`law` / `southwest` / `assumption` / `class_files`),
`citation` (for example `14 CFR 117.19` or a URL), `enforced` (true or false) and `checked_by` (the function that checks it).
A test fails if a rule is missing a field, or says `enforced: true` with no real function behind it.

```bash
python -m scripts.rules_catalog     # every rule as a table: id, value, source, enforced
```

- **Safety ladder:** life, aircraft, infrastructure, legal, vulnerable passengers (these remove unsafe options),
  then network, convenience, cost (these only rank what's left). Unknown data counts as unsafe.
- **Who decides:** `auto`, `needs_approval` (with the approver's role) or `human_only`. Anything not listed is human-only,
  and nothing that touches rungs 1-4 can be automatic.
- **Children flying alone** (Southwest unaccompanied minor policy): ages 5-11, nonstop or same plane only, never international.
  A rebooking that puts a child on a connection is removed; the international check reads the children-alone rules.
  A stranded child stays with Southwest staff and is never sent to a hotel alone.
- **Tarmac food and water** (law, 14 CFR 259.4): an option with 2+ hours on the tarmac is removed unless it has a food-and-water plan.
- **Delayed bags** (law, DOT refund rule, 14 CFR 260.5): the checked-bag fee is refunded if a bag is late by more than 12 h (domestic),
  or 15 / 30 h (international, by flight length). Southwest charges for checked bags ($45 first, $55 second since 9 Apr 2026;
  sources are in `southwest.yaml`).
- **Crew hours** (checks in `backend/rules/crew.py`): pilots use the full Table B (start time x number of flights), the
  up-to-2-hour extension before takeoff (the captain and airline decide; more than 30 min only once before rest), extension after
  takeoff only as needed to land safely, 10 h rest, 30 h off in 168 h, and the 60/190 duty-hour and 100/1,000 flight-hour limits.
  Flight attendants: 14/16/18/20 h duty, 10 or 12 h rest, 24 h off in 7 days, and the beyond-our-control exception.
  Ground staff have no federal limit, so the shift length, break, gap and days-in-a-row limits are assumptions.
- **Tarmac** (`backend/rules/tarmac.py`): `tarmac_timeline()` takes a plane on the ground and returns the deadlines (30 min
  announcement, 2 h food and water, latest time to start returning, the limit) and what blocks getting people off (no gate or stairs,
  no staff, lightning, no customs, TSA closed, wheelchair or children without help). It **never** decides to keep people on board:
  when something blocks deplaning and time is up it answers `captain_decides`, because the safety and air-traffic exceptions are
  human only and have to be logged.
- **Airports** (`backend/rules/airport.py`): curfews (San Diego bans departures 23:30-06:30; Washington National limits night flying to
  quiet aircraft; MCI has none), TSA re-screening and Southwest staff hours at diversion stations, lightning within 5 miles closes the
  ramp, a plane struck by lightning is inspected before it flies, and nobody waits more than 30 minutes alone in a wheelchair.
- **Legal and safety numbers live only in `config/rules/`**; the simulator reads them as plain YAML (it never imports the rules code),
  and a test fails if `simulator.yaml` repeats a name that a rule owns.
- **Fuel:** `aircraft.fuel_minutes_extra` is spare *holding* fuel above the legal reserve (fuel to reach the alternate plus
  45 min, 14 CFR 121.639), which is already on board and not counted. A plan is unsafe if
  `fuel_minutes_extra - hold_minutes - minutes_to_alternate` is below `limits.fuel_buffer_min` (10 min). Unknown fuel counts as unsafe.
  A31 example (55 min): hold 45 min is kept (10 left), hold 30 then divert to St. Louis with 15 min extra flying is kept (10 left),
  hold 50 min is removed (5 left), Tulsa is removed (no gate). The simulator reads the same `fuel_buffer_min` from `config/rules/` as the point where a holding plane
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
  plus the rest of its day (plus the buffer in the rules) is swapped for a reserve crew from the Reserve Crews data if one is free,
  available in time and long enough on duty. The flight then records which reserve flew (`flights.reserve_crew_ids`), the reserve is
  marked used and its own duty clock runs down, and the swap is written to `events` and `audit_log`. If no reserve is free and the
  pilots are only a few minutes short, the captain and airline extend their duty (once per crew, 14 CFR 117.19); otherwise the
  flight is cancelled ("crew out of legal duty").
- **Crews already short in the class data** still fly in today's baseline, but are flagged at 05:00: `crew_illegal_rest` for the 35
  crews whose rest is below the legal 10 h minimum (a real legal problem, with its `data_issues` reference), and `crew_short_for_plan`
  for crews short only for our own planning estimate (flight + one more leg + buffer; not illegal).
- **Pilot duty extension** (kept, approved by the project owner): every `pilot_duty_extended` event is labelled as a human decision
  by the captain and crew scheduling (decision D02).
- **Story lock:** a test freezes the seed 7 a31_storm numbers (17 delayed, 5 cancelled, 9 diverted, 559 stranded overnight, A31 to
  St. Louis at 12:15 and back at 14:10, R04 on WN1033, Maya on WN3005 at 06:30 on 7 Oct). The simulator numbers are frozen; if that
  test fails, check with the project owner before changing it.
- **Aircraft out of position:** a diverted aircraft that can't get back in time delays its next flight, and cancels it past the cutoff.
- **Stranded overnight:** if no seat is left today (rebooking never fills a plane past 93%), passengers go on a next-day flight on
  7 Oct, are marked `overnight` in `passengers`, and we record whether they live locally. No hotel or money decisions are made here.
- A test checks that across seeds 1-20, every storm or ground stop of 2+ hours cancels between 10% and 35% of the departures
  scheduled from the start of the stop to 2 hours after it ends (the band is in `simulator.yaml`).

The simulator never imports `backend/rules`: it only causes problems, it does not decide anything.

## How the co-pilot is put together (agreed setup, before Phase 4)

**Five agents and an orchestrator.** There is no guardrail agent and no separate crew agent.

| Part | What it does | AI? |
|---|---|---|
| orchestrator | Plain code. Routes work, sees all agents, runs the rule check on every proposal, catches conflicts between agents, escalates to humans. | no |
| situation | What is happening, the likely cause, the expected end. | summaries only, with a template fallback |
| network_recovery | Flights, aircraft, and pilots and flight attendants: crew legality, reserve crews, deadheads, crew hotels, tomorrow's crew legality, rebooking plans. | no |
| station_staff | Ground staff (gate, ramp, wheelchair, customer service), gates, ramp, deplaning help. | no |
| passenger_care | Rebooking, hotels, compensation, vulnerable passengers. | no |
| communication | Messages, and every channel telling the same story. | message drafts only, with a template fallback |

**The rule check** (`backend/rules/check.py`, plain code, no AI). The orchestrator runs `check(proposal, pending_proposals, world)` on
every proposal before any human sees it. It runs the safety ladder and every enforced rule that applies (crew duty and rest and Table B,
tarmac steps, curfews, customs, TSA and station hours, lightning and the ramp, inspections, the wheelchair 30 minutes, children-alone
routing, fuel, the money rules), checks the action against `autonomy.yaml` (a human-only action never passes, nothing on rungs 1-4 is
automatic), and looks for conflicts with other pending proposals (delay and cancel the same flight, rebooking onto a flight that is
cancelled or full, an offer or promise no approved decision backs, a message that contradicts the truth, the same crew or plane used
twice). Missing or stale data is a block, never a pass. The answer is `pass` or `block`, the reasons, the rule ids, and the human role
it goes to. An agent's own checks never count as final.

**Only humans override.** The orchestrator can block but can never override a block. `backend/rules/override.py` lets a named person
lift a block only where a `law.yaml` rule allows that exception (its `override_by` list: today only the captain's safety and ATC calls
on the tarmac and the food-and-water exception), and writes the person's role and reason to `audit_log`.

**The rules of the world** live in `config/rules/` (law, Southwest policy, our assumptions, the safety ladder, who decides what).

**The decision catalog** (`config/decisions.yaml`, built from the table in `scripts/build_decisions.py`) lists every decision the
co-pilot may raise: 195 of them, in 20 families (A detect ... T security). Each says what triggers it, its deadline, which agent raises
it, who decides (matching `autonomy.yaml`), which rules the rule check runs, the data it needs, whom it affects, and whether the
a31_storm seed 7 day raises it. `python -m scripts.decisions` prints a summary and the gaps (decisions the simulator can't trigger yet,
rules that had to be added as assumptions).

**Contracts and shared AI guardrails.** `config/agents/` holds one contract per part (what it may read and write, its actions, what it
must never do, its output schema, data age and time limits). `config/agents/_shared.yaml` lists the 11 rules every agent follows (a
source and time for every fact, no stale data, no human-only decisions, no unapproved promises, names only for the passengers it is
working on, schema-exact output, hand doubts to a named human, name the decision ids, rule check before any human sees it, language-model
text checked against the facts, everything in the audit log), each enforced in `AgentContext` or in the rule check.

**How they connect.** An agent reads through `AgentContext` (only what its contract allows) and saves a proposal naming its decision ids.
The orchestrator runs the rule check and records pass or block (events and audit log). Humans see only proposals that passed, plus
every block with its reasons. A named human approves, rejects, or (only where the law allows) overrides. Only an approved action
changes the truth tables.

```bash
python -m scripts.contracts         # one-page summary of the orchestrator + five agents
python -m scripts.decisions         # the decision catalog: counts and gaps
python -m scripts.rules_catalog     # every rule
```

## The Situation agent (Phase 4a)

The first agent. For MCI it answers, again and again through the day: is something abnormal happening, what is it and why,
how long will it last, and who and what is affected. It **reads and reports**; it changes nothing. Code: `backend/agents/situation/`,
settings (every number an assumption): `config/agent_settings/situation.yaml`, contract: `config/agents/situation.yaml`.

```bash
python -m simulator.run --seed 7 --scenario a31_storm                                     # put a day in the database
python -m backend.agents.situation.run --run-id a31_storm-s7-today --at 12:20             # the assessment at 12:20 (nothing saved)
python -m backend.agents.situation.run --run-id a31_storm-s7-today --replay               # the whole day, saving each new version
python -m backend.agents.situation.run --run-id a31_storm-s7-today --replay --stop faa_feed=12:00   # with an FAA feed outage
python -m simulator.report --latest                                                       # the debug page now has a "Situation" section
```

**Three protections, enforced in `AgentContext`, not in the agent's code:**
- **No answer key.** The situation contract can't read incidents (the simulator writes its own, with the true cause), metric_runs,
  passengers or crews. It sees events only from the run it was given, without the run id, seed or actor, and only the payload fields
  a real source publishes (listed per event type in the contract; anything else, such as a planted `true_cause`, is stripped).
  The agent's code never imports the simulator or reads `simulator.yaml`.
- **No future knowledge.** Every events read is cut at the agent's current time. The truth tables hold end-of-day values, so the agent
  reads only the published schedule from `flights` and rebuilds the state at time T from the events (`state_at.py`).
- **Freshness that means something.** Each simulated source (faa_feed, weather_feed, flight_ops, station_ops) sends a heartbeat every
  simulated minute (`backend/contracts/feeds.py`); a quiet hour is not stale. A source can be stopped to simulate an outage: its
  events vanish after the stop, and once it is older than 120 s the agent writes "cannot_assess: <source> stale since HH:MM" (A08)
  and, after 5 minutes, proposes S01.

**What it does, every 5 simulated minutes and at once on trigger events** (FAA notices, ramp closed/reopened, a diversion, a
cancellation, a feed going quiet): detects (A01), classifies by rules with a confidence from how many sources agree (A02), proposes
controllability for the event and for each cancelled or long-delayed flight (A03; crew, maintenance and staffing cancellations during
an uncontrollable event are "needs human review"), gives earliest / likely / latest end times (A05) and proposes a re-plan when the FAA
changes the stop (A06), counts the impact (A07, Q05), spots conflicts between sources (A09), and proposes an FAA status check every
30 min (P02). Security reports go straight to a human (T01). It writes only when something meaningful changed; the summary comes from
a fixed template (the AI hook exists but is switched off).

**Shared state.** Each version is one row in `situation_assessments` (append-only, like events). **This is the table the next
agents will read.** Proposals needing a human go to `recommendations` as pending (the orchestrator's rule check comes later); notes go
to events. Every replay has its own `agent_run_id`, so two replays never mix. If an assessment fails halfway, it is rolled back and one
`agent_error` event is written.

## Chaos testing the Situation agent (Phase 4a-test, part 1)

Random worst-case days the agent has never seen, scored automatically against what really happened.

```bash
python -m chaos.run --surprise            # one brand-new random day: what the agent said, then what really happened, then the score
python -m chaos.run --seed 17             # replay exactly chaos day 17 (to reproduce a failure)
python -m chaos.run --days 20             # 20 new random days (about 4 min on Neon), then the summary page
python -m chaos.run --days 20 --fresh     # the same, after resetting the database
python -m chaos.calibrate                 # end-time ranges on training days 1-100 and test days 101-150
```
Open `reports/chaos/index.html` (double-click) for the summary, and click a day for its timeline.

- **The generator** (`chaos/generator.py`, numbers in `config/chaos.yaml`) builds a day from 1-3 random ingredients: a quiet day,
  normal scattered delays, a thunderstorm ground stop (extended up to 4 times, sometimes shortened or ended early), an FAA ground delay
  program, a Southwest IT outage, an airport power outage, a runway closure, a security evacuation, a wave of crew cancellations, two
  overlapping incidents, a feed going silent, conflicting signals, messy data (duplicates, late deliveries, broken payloads, event types
  nobody has seen) and timing extremes. It plays the day through the simulator and adds the notices real sources would publish
  (airport ops notices, police/TSA notices, system monitor changes). Chaos days write only their own events, never the shared tables,
  so they never touch the a31_storm run.
- **The sealed truth** of each day goes to `chaos/truth/<run_id>.json` (not committed). Only the judge reads it; the agent has no way to
  reach it (it can't open files, and AgentContext only serves the tables in its contract).
- **The judge** (`chaos/judge.py`, rules in `config/chaos_scoring.yaml`) reads the agent's rows back from the database and the truth,
  never the agent's code, and gives 14 checks a PASS / WARN / FAIL with a reason: detection, cause, overconfidence, two incidents,
  expected end, stale feeds, conflicts, security, controllable, messy data, recovery, human clutter, guardrails, speed.
- **Calibration.** The end-time margins and end confidences in `config/agent_settings/situation.yaml` were chosen on chaos days 1-100
  only, then frozen; days 101-150 are only measured. Real end inside the range: 75% -> 81% on training, 81% -> 83% on test. "High
  confidence" is right 82% (training) and 83% (test) of the time, against the about 79-80% it claims.
- **Agent fixes that came out of it:** a "recovery" status (operations back, aftermath remains), one open proposal per decision per
  incident (a new value supersedes the old one, `recommendations.supersedes`), an FAA status check only after 30 min without an FAA update,
  defensive reading of messy inputs (duplicates counted once, late events put back in order, broken events set aside), and naming a
  second problem when two happen at once.

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
backend/rules/             safety ladder, crew, tarmac and airport checks, who decides, compensation
backend/contracts/         agent contracts: loader, AgentContext, feed heartbeats, the orchestrator's review plumbing
backend/agents/situation/  the Situation agent (state_at, assess, run)
config/agent_settings/     settings for each agent (every number an assumption)
chaos/                     chaos testing: generator, judge, pages, calibration (config/chaos.yaml, config/chaos_scoring.yaml)
config/decisions.yaml      the decision catalog
config/rules/              every rule (law, southwest, operations, safety ladder, autonomy)
config/agents/             one contract per part (+ _shared.yaml: the AI guardrails)
config/simulator.yaml      every simulator number
config/seed.yaml           every number used to build the starting data
simulator/                 scenario, world (truth), channels (screens), run, report (debug page)
reports/                   generated debug pages (not committed)
scripts/rules_catalog.py    every rule as a table
scripts/contracts.py       summary of the six contracts
scripts/decisions.py       summary of the decision catalog
scripts/reset_db.py        wipe and rebuild
scripts/summary.py         human-readable check
tests/                     pytest checks
data/raw/                  the class files
```
