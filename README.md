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
| 4b-1 | Communication agent, part 1: detect channel mismatches | **done** |
| 4b-2 | Communication agent, part 2: fix screens + today vs with co-pilot | **done** |
| 4c | Shared state: Communication reads the Situation agent's picture; app/SMS screens only for passengers who have them | **done** |
| 4b-3 | Communication messages + conflict holds that clear (evidence or a person) | **done** |
| 4d | Network Recovery agent (advisory: crews, delays/cancellations, hold or divert, recovery) | **done** |
| 4e | Passenger Care agent (hotels, transport, compensation status, vulnerable passengers, rebooking advice) | **done** |
| 4e-2 | Network Recovery -> Communication (held drafts, crew drafts, staff notes) | **done** |
| 4f | Station & Staff agent (tarmac care, gates, ground staff, wheelchair and child help) + link to Network Recovery | **done** |
| Later | Plain-code orchestrator | planned; no guardrail agent |
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

`--mode copilot` corrects screens automatically after the rule check. Run today first, then copilot: the second run preserves
today's events, numbers, truth tables and `channel_views` (today's screens), and saves its own events, recommendations and
numbers alongside them under the `-copilot` run id. Re-running today starts a fresh pair and rebuilds the database.
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
python -m chaos.calibrate                 # end-time calibration by group on training days 1-100
python -m chaos.calibrate --seeds 5000-5059          # the same table on other days (measured only)
python -m chaos.calibrate --search --write           # choose end margins and confidences on training days 1-100 ONLY
python -m chaos.memory_replay --seeds 1-100 --show   # the whole agent + judge on many days in memory (no database, ~0.3 s a day)
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
- **Calibration.** The end-time margins and end confidences in `config/agent_settings/situation.yaml` are chosen on chaos days 1-100
  only, then frozen; every other day is only measured. See "Part 2" below for the table by group.
- **Agent fixes that came out of it:** a "recovery" status (operations back, aftermath remains), one open proposal per decision per
  incident (a new value supersedes the old one, `recommendations.supersedes`), an FAA status check only after 30 min without an FAA update,
  defensive reading of messy inputs (duplicates counted once, late events put back in order, broken events set aside), and naming a
  second problem when two happen at once.

### Part 2: fixes after the 60-day run (chaos seeds 3791350-3791409: 37 PASS, 18 WARN, 5 FAIL)

Root causes, each with a test in `tests/test_situation_fixes.py`:

| Day | What was really wrong | Whose bug |
|---|---|---|
| 3791364, 3791374 | The range was the announced end + a flat 60 min whatever the cause, and said "high" (0.8) for storms, where it was right only 70% of the time on training days. Duplicate FAA notices were already ignored. | agent |
| 3791390 | One FAA notice, no messy data involved: the storm ended 30 min **early**, and the earliest end was always the announced end. | agent |
| 3791409 | The second crew cancellation (11:05) was delivered 17 min late. The replay clock woke the agent on an event's *happened* time, not its *delivered* time, so it next looked at 11:25. The judge also started the clock at 11:05, before anyone could see it. | agent (clock) + judge |
| 3791386 | Times: the database returns UTC, so 06:45 was printed as 11:45 (display only). Guessing: the 06:45-07:15 silence sat inside the IT outage's 06:09-08:26 silence and the heartbeat simulator took the inner window, so the feed looked fresh at 06:45. No A09: system health said the system was **down** then, so there was no "health up" to conflict with. | simulator (heartbeats) + generator (truth) + judge (display, nested silence) |
| recovery WARNs | The agent's "N flights cancelled in the last 60 min" kept it abnormal for up to an hour after the cause was gone, and the agent and the judge each had their own idea of "operations back". | agent + judge |

What changed:
- **One definition of "operations back"** (`backend/rules/recovery.py`, quiet time `operations_back_quiet_min` in
  `config/rules/operations.yaml`), used by the agent and the judge: no FAA program, ramp open, nothing holding, every feed and system
  healthy, and no flight newly delayed or cancelled in the last 30 min. abnormal -> recovery when operations are back; recovery ->
  normal when no cancelled-flight passengers wait for a seat and no aircraft is out of position (stranded overnight passengers keep
  it in recovery until A10). Delays and cancellations within 2 h after an incident's operations came back are its aftermath, not a new
  incident.
- **Announced end:** the notice with the latest issue time wins (duplicates, late or out-of-order notices can't move it); a re-issued
  "started" notice is an update; two notices with the same issue time and different ends are an A09 conflict.
- **End range:** by the program's own cause and the number of changes so far, with extras for evening, a growing storm and messy FAA
  data (all calibrated; the extras came out 0 on training days). Confidence = the group's real coverage on training days, rounded down;
  never "high" in a group with fewer than 30 training moments; never higher after more changes. Past the announced end with no word,
  or "ended" while planes still hold: medium at most.
- **Slow build-ups:** the agent now wakes on every delay and cancellation when it is delivered; two crew cancellations within 2 h are an
  early "crew suspected" at 0.45, rising to 0.65 as more crews time out; newly delayed flights are compared with that hour's normal level.
- **Judge fixes** (each a real judge error, not a loosening): times printed in MCI local time; the detection clock starts when the
  first signal was delivered; an agent already saying "cannot assess" (or with an S01 already open) when a nested silence begins
  counts; "health up, feed down" is no conflict while health says down; two incidents overlap only once both could be seen; the
  recovery check uses the shared definition and stops at 23:59.
- **Results:** fresh days 7318420-7318479 (never tuned on): 56 PASS, 4 WARN, 0 FAIL (old batch: 37 / 18 / 5). Training days 1-100
  in memory: 92 PASS, 8 WARN, 0 FAIL. Real end inside the range on training moments: 80% -> 93%, weather 70% -> 89%, and "high"
  is right at least as often as it claims in every group (it was not for storms, mornings, 0 changes, or messy data).
- Smaller: one S01 for a feed and the system behind it (they used to replace each other every 5 minutes), and a flap back to abnormal
  within 30 min of "normal" is the same incident (no second set of proposals).

## The Communication agent, part 1: detecting mismatches (Phase 4b-1)

Every passenger should hear the same true story from every channel (app, SMS, gate screen, call center). Phase 4b-1 only **finds**
what each channel shows wrongly; it fixes nothing and writes no messages. Code: `backend/agents/communication/`, settings (every
number an assumption): `config/agent_settings/communication.yaml`, contract (written for all three phases):
`config/agents/communication.yaml`.

```bash
python -m backend.agents.communication.run --run-id a31_storm-s7-today --at 14:58                      # what is wrong at 14:58
python -m backend.agents.communication.run --run-id a31_storm-s7-today --replay                        # the whole day (about 2-3 min on Neon)
python -m backend.agents.communication.run --run-id a31_storm-s7-today --passenger PAX0001 --replay    # Maya, next to what really happened
python -m chaos.judge_communication       # score it against the simulator's answer key: seed 7 + 20 random chaos days, in memory
python -m simulator.report --latest       # the debug page has a "Communication agent" section
```

- **The truth feed.** The simulator now publishes each tracked passenger's trip record (`trip_record_updated`: itinerary, status,
  destination, bag routing) at the start of the day and whenever it changes, as a real reservation system would. The agent rebuilds
  the truth and what every channel shows at time T from events only (`state_at.py`), never from the end-of-day tables.
- **No answer key.** The channel events still carry the simulator's `lag_min` and `cause_if_late` (the debug page uses them), but the
  contract lists only the fields a real system publishes, so AgentContext strips them. The only failure signal is the delivery receipt.
- **Freshness.** Each channel is its own feed with its own heartbeat (the contract's `_split_by: channel`). A stale channel is not
  checked; a stale truth feed stops all checks ("cannot_check: <feed> stale since HH:MM").
- **Privacy.** No names, contacts or booking refs are in the 4b-1 contract, so AgentContext refuses them even with working_on.
- **Phases.** The contract has `phase: 4b-1` and an `enabled_in` per action; AgentContext refuses fix and message actions until their
  phase. `mismatches` is written only by this agent, only its own rows (AgentContext checks), each with sources and decision ids.
- **Scoring.** `chaos/judge_communication.py` reads the simulator's answer key (which screens were really wrong, when, and why) and
  measures recall, precision, detection delay, cause accuracy, critical cases, stale-feed handling and name reads.
  Summary: `reports/chaos/communication.html`; the chaos index gets a Communication column.

## How the co-pilot fixes screens (Phase 4b-2)

Each minute, Communication reads the published trip records and delivery receipts through its contract. It groups wrong
fields into one correction per passenger and channel. Critical cases go first, then nearer departures, then older mistakes.
Other changes wait two minutes to settle. A stale truth feed, stale flight-operations feed, or an open source conflict holds
the correction and records why. Until shared state arrives in 4c, contradictory cancellation/departure signals and explicit
source-conflict notices provide the local conflict check; the policy also has an explicit hook for additional open conflicts.

The executor runs the existing rule checker. Only automatic actions that pass are delivered. A correction contains exactly
the confirmed itinerary, status, destination and baggage routing. It cannot add a promise, compensation, hotel or new time.
The existing system text is reused; no message writing or AI is enabled. Alternative deliveries use channel types only,
projected from the passenger's available channels. Names, addresses and booking references remain unavailable.

Corrections use the same channel delay, load, jitter and failure calculation as ordinary copies. A separate random stream
keeps the original day's weather, flights, bookings and normal delivery draws unchanged. A new correction request may arrive
before a pending ordinary copy; revision checks prevent older copies undoing a newer correction. Already confirmed seats do
not go through another reservation seat hold. These transport semantics are explicit: there is no instant-delivery shortcut.
Trip and baggage components retain their separate transport delays. Gate updates are routed to the flight-display endpoint;
call-center updates to that passenger's notes; app/SMS corrections are pushes.

At most three correction requests per passenger per channel in any rolling 30 minutes are allowed, including resends. The
same version is not repeated on a channel while its delivery is still expected: the agent waits at least 15 minutes and at
least that channel's usual lag as it has measured it today, unless a failed-delivery receipt has arrived since. A failed
delivery, or a screen wrong for 30 minutes, can trigger the same confirmed record on another supported channel the passenger
has (app or SMS), once per trip version. All thresholds are assumptions in the Communication settings.

Both runs store comparison numbers in `metric_runs`. The report overlays their wrong-screen counts, shows every measure side
by side and includes Maya's minute-by-minute views for both modes. Recovery includes lost updates. Median/worst catch-up times
cover versions that actually reached all four screens; replaced versions and versions still wrong at day end are counted
separately rather than silently treated as successes. Push totals count extra corrections/resends, not ordinary system copies.

The shared cause vocabulary is in `config/channel_causes.yaml`. Raw simulator event labels remain compatible with the old
story; reported causes use the common names. An itinerary change is no longer mistaken for a seat-only hold. On seed 7 the
cause-label score changed from 95.7% to 89.7%; normal-wait versus stuck remains an inference, and the judge was not relaxed.

Run these commands in order. Never run tests while a simulation or replay is writing to the database:

```powershell
python -m pytest -q
python -m scripts.reset_db
python -m simulator.run --seed 7 --scenario a31_storm
python -m simulator.run --seed 7 --scenario a31_storm --mode copilot
python -m backend.agents.situation.run --run-id a31_storm-s7-today --replay
python -m backend.agents.communication.run --run-id a31_storm-s7-today --replay
python -m simulator.report --latest
python -m chaos.memory_replay --compare
```

Open `reports/a31_storm-s7-copilot.html` for the seed 7 comparison, and `reports/chaos/comparison.html` for seed 7 plus 20 fresh
random days. The chosen seeds are saved in `reports/chaos/comparison-seeds.json`; replay them with
`python -m chaos.compare --seeds <comma-separated-seeds>`. Paired days assert identical operational truth and non-channel
events. Random-day comparisons include published chaos overlays, late deliveries and feed outages. They are evaluation only;
settings are never calibrated on them. `python -m chaos.judge_communication --seed7-only` re-scores detection in memory.

**Result (verified).** Seed 7: passenger-minutes on a wrong screen 13,625 today vs 6,152 with co-pilot (55% fewer), critical
1,594 vs 570, lost updates still wrong at day end 12 vs 0. Maya's screens after her 14:55 rebooking: app 15:12 -> 14:57, SMS
15:14 -> 14:59, gate 15:20 -> 15:06, call center 15:32 -> 15:17; her call center, stuck on "diverted" all afternoon today, shows
"delayed" at 14:43. On 20 fresh random days: 51% fewer on average (15 days had wrong screens), best 72%, worst 13%, no day
worse, world truth identical on every day. Today mode is byte-for-byte what it was before 4b-2.

## How agents share information (Phase 4c)

Agents never call each other. One agent writes a shared record and another reads it, always through its own AgentContext, under
seven rules (written in `config/agents/_shared.yaml`, enforced in `backend/contracts/shared_state.py`):

1. Agents share information only through shared tables and events, never by calling each other.
2. A shared record is never changed or deleted. Each new version says who made it, as of when, and from what sources.
3. A reader sees only versions made at or before its own current moment, never one from later, even later in the same minute.
4. A reader's contract lists exactly which fields it may read; anything else is refused. Some fields are never shared: the
   Situation agent's "was this flight within Southwest's control" (it decides compensation, so only after a human approves) and
   its internal evidence.
5. A suspicion stays a suspicion: a suspected cause or an expected end time arrives labelled "suspected", with its confidence.
6. An old version is not out of date: Situation only writes a new one when something changes. What matters is whether Situation
   is still running, so it leaves a heartbeat every time it runs, and readers check that.
7. When two agents disagree about the same fact, both views are logged and a human is asked; nobody quietly picks one.

In co-pilot mode each minute runs Situation first (on its normal schedule), then Communication, then the rule check and delivery.
Situation's versions are exactly the same as in its standalone replay. Communication uses Situation's picture to hold corrections on
a flight where Situation reports conflicting data (and to resume when that clears), notices if Situation stops running (corrections
carry on; the supervisor is told after 30 minutes), and keeps the latest picture ready for messages in 4b-3.

**Screens.** The gate screen and the call center apply to every passenger. The app and SMS exist only for passengers who have
them on file (app+SMS 197, SMS only 65, app+email 47, email only 11). Maya Torres has app + SMS: a story override in
`config/seed.yaml`, applied after her random draw so no one else changes.

**Result.** Seed 7: 11,744 passenger-minutes on a wrong screen today vs 6,190 with co-pilot (47% fewer). On the same 20 random days
as before: 48% fewer on average, none worse. On 20 extra days with source conflicts and feed outages
(`reports/chaos/comparison-extra.html`): 49% fewer on average, none worse; 115 corrections held for Situation conflicts on 8 days.

## Messages to passengers and staff, and conflicts that clear (Phase 4b-3)

**Conflicts clear.** The Situation agent now closes a conflict when later evidence settles it, and says which side was right:

- A flight "departed while the ramp was closed" is shown false by a later departure, a new delay at the gate or a cancellation.
- It is shown real by the flight being seen in the air or landing.

If nothing settles a conflict within 30 minutes, it is marked "unresolved: needs a human". A person's answer closes it at once,
recorded as "resolved by a human (role) at HH:MM".

Communication now holds only the detail in dispute (for example the flight's status) and keeps fixing everything else. After 15
minutes of holding, it asks network operations which side is true, with counts only and never names. After 45 minutes it asks
again as urgent. It never picks a side itself.

**Simulated people.** The simulation has no real humans, so `backend/execution/simulated_human.py` stands in for them, in
co-pilot mode only:

- The customer service supervisor and network operations each answer after 5 to 20 minutes, or 2 to 8 when urgent
  (`config/simulated_humans.yaml`).
- They answer a conflict with the real truth. A person can look out of the window; an agent can't.
- They never see anything the rule check blocked.
- Every answer is an event from "simulated_human:<role>".

**Messages.** Templates only (`config/message_templates.yaml`), with no AI. Each message carries the facts it states, and a
message fact check (`backend/rules/message_check.py`) verifies every flight, time, date, city and status against the truth at
send time. It also blocks:

- promises (compensation, vouchers, refunds, hotels, meals, guarantees) unless an approved decision is linked (none exists yet);
- blame, and sensitive details (wheelchair, child alone, medical);
- other passengers' details, names outside the app, SMS over 320 characters, and text that isn't plain English.

Expected end times are never exact: "around 13:00; this may change", or "we don't have a reliable estimate yet". The cause is
named "weather" only when Situation is confident; otherwise it is "an operational issue". Every message says what we know,
what we don't know yet, what to do now, and when the next update comes.

Who decides:

- Restating the truth is automatic: first notice, delay, diversion, "no change yet" (hourly), and a cancellation or rebooking that only states the confirmed new flight and time.
- A passenger who is in the air is told what to do when they land, not "before you go to the gate".
- The supervisor approves anything beyond confirmed facts (options, overnight arrangements), corrections and staff notes.
- One approval covers a batch to more than 50 passengers.

Delivery and anti-spam:

- At most 4 messages per passenger per hour. Messages go only on channels the passenger has, and a failed one is resent once
  on the other.
- A message whose facts change before it arrives is dropped and replaced. One waiting for approval is withdrawn and redrafted.
- The deliberately unused sentence "A Southwest agent will contact you about overnight arrangements": nobody would do it yet,
  because Passenger Care is not built.

## Network Recovery (Phase 4d, advisory)

The Network Recovery agent plans flights, aircraft, pilots and flight attendants. In this phase it only advises: every proposal
goes through the rule check, and the result (passed or blocked, and who would have to approve it) is recorded, but nothing is
applied. The world stays exactly as in today mode.

What it does, every 5 minutes and whenever something important is reported:

- **Crews.** It works out when each crew on a waiting flight would run out of legal duty, given the delay and the Situation
  agent's expected end (likely and worst case). It warns at least an hour ahead and proposes the best legal fix: a reserve, a
  crew whose own flight was cancelled (a swap), or a crew flying in as passengers (a deadhead). Every option is run through the
  crew rules first, and an option they reject is never proposed. Extending a pilot's duty is only ever a question for the
  captain and crew scheduling.
- **Delays and cancellations.** It ranks the departures in the stop window by how much they need protecting:
  - priority, and connecting passengers;
  - the last flight of the day to that city;
  - international flights, load, and seats left later today.

  It proposes new times, and cancels only flights whose passengers fit on later flights, or whose crew can't legally fly.
- **Hold or divert.** For each plane in the air, it works out the latest time it can leave for each alternate airport with
  10 minutes of spare fuel left. It warns early and lists the alternates that pass the fuel and curfew checks. Diverting is for
  the dispatcher and the captain.
- **After diversions.** It plans getting diverted planes back, flags maintenance deadlines and curfews, and proposes the
  restart order when the stop lifts.
- **Its plan** is published in `recovery_plans`, ready for Passenger Care later.

`python -m chaos.judge_network` scores it on seed 7 plus 5 fresh random days (`reports/chaos/network.html`). Cancellations and
stranded passengers for the plan are estimates, because the plan is not applied.

## Passenger Care (Phase 4e)

The Passenger Care agent looks after passengers whose flights are cancelled, who miss a connection, or who are stranded
overnight. Children flying alone and wheelchair users on an affected flight come first.

- **Overnight.** Local passengers go home by contracted car. Everyone else gets a hotel room: rooms are held in blocks early
  (automatic up to 40 rooms, then a supervisor), and each passenger's room is approved by the customer service supervisor. The
  hotel is picked by rooms left, Southwest's preference, shuttle, distance, last check-in and rate. Wheelchair users get an
  accessible room, and unused rooms are released.
- **Money.** The legal refund is always offered. Southwest's own commitments (meal, $75 voucher, the hotel) are only given once
  a person has decided the cause was within Southwest's control. Until then they show as "pending the supervisor's
  controllability decision", and nothing is promised. Costs are tracked against a cap.
- **Children alone** stay with staff and their guardian is called; they are never sent to a hotel alone and are only rebooked
  nonstop. **Wheelchair users** get help requests written down for Station & Staff (the next agent).
- **Rebooking advice:** a better flight than today's, where the Network Recovery plan shows seats. It is advice only; bookings
  are never changed.
- **What is applied.** In co-pilot mode, room holds and approved rooms are applied to the co-pilot's own hotel_assignments
  table. Flights and bookings are never changed.
- **The overnight message.** Once a room is approved, the Communication agent tells the passenger which hotel and how to get
  there, with no money mentioned.

## Network Recovery → Communication (4e-2)

Communication reads only the proposed delays, cancellations, crew actions and approval status allowed by its contract.
It prepares passenger and crew messages immediately, with their originating plan version and proposal number. Internal
staff notes can say a change is under review; they never go to passenger channels.

In co-pilot mode, simulated network operations control and crew scheduling answer only proposals that passed the rule
check. Approval is recorded but does not change flights or crews. A draft stays held as **approved, waiting to take effect**
until an operational application confirmation and current flight/crew records agree with it. Crew messages also retain
O09's supervisor approval. Changed facts cause a new draft; rejection or supersession discards the old one. Passenger
messages are checked again before delivery. Every draft, hold, send and discard is recorded through AgentContext and audited.

The paired debug page includes the full history, exact text, status and originating plan version:

```powershell
python -m simulator.run --seed 7 --scenario a31_storm --mode today
python -m simulator.run --seed 7 --scenario a31_storm --mode copilot
python -m simulator.report --run-id a31_storm-s7-today
```

Run these after pytest has finished: pytest rebuilds the database. Seed 7's C35 swap produces held drafts for Maya and
C35 plus an internal staff note. Approval does not apply that swap, so the passenger and crew drafts are never sent.
The draft omits a report-by time because the proposal does not supply one.

## Station & Staff (Phase 4f)

The Station & Staff agent looks after the people and the ground at MCI. It sees passenger ids and counts only, never names.

- **Tarmac care.** For every plane waiting on the ground (at MCI or at a diversion airport) it works out the legal deadlines:
  a status announcement by 30 minutes, food and water by 2 hours, and the time to start returning before 3 hours (4 hours
  international). Each is met or flagged in time; food and water and offers to get off are proposed to the station manager.
  Returning to a gate and keeping people on board longer are the captain's decisions: the agent only asks, with the facts.
- **Gates.** A gate for every plane arriving in the next hour, kept from landing until its next departure. If a plane's time
  moves, the gate is re-planned. When gates run out it proposes a remote stand with stairs, then asks the airport.
- **Staff.** Gate agents, ramp crews and customer service go to each flight's work, busiest flights first. Nobody is booked
  for two jobs at once, outside their shift, or across a planned break, and every assignment carries the ground-staff limits
  for the rule check. When short, it proposes starting a later shift early (station manager). No ramp work is planned while the
  ramp is closed for lightning, and work already assigned is paused automatically.
- **Wheelchair help and children alone.** Every wheelchair passenger in Passenger Care's plan gets a named wheelchair agent at
  arrival, connection and deplaning (never more than 30 minutes unattended). A child flying alone gets a named staff member
  until the guardian collects them (station manager); this closes Passenger Care's request.
- **Ramp and terminal.** Closing or reopening the ramp is the ramp supervisor's call: it only asks. It warns when the terminal
  nears its capacity.
- **What is applied.** In co-pilot mode, approved (or automatic) gates and staff go to the co-pilot's own gate_assignments and
  staff_assignments tables. Flights, bookings, crews and the staff roster are never changed.
- **The link to Network Recovery.** Network Recovery reads the station's ramp status and gate and staff shortages and never
  proposes a departure while the ramp is closed or in a 15-minute slot with no ramp crew or gate agents left; such a departure
  moves to the next slot that can work it, with the reason.

The debug page's comparison section has a "Station & Staff" part: tarmac deadlines, gates, staff coverage and limits,
wheelchair and child coverage, and the departures Network Recovery moved.

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
backend/agents/station_staff/ the Station & Staff agent (station.py)
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
