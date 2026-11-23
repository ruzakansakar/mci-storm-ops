WHAT THIS IS
A one-person class project: a "Ground-Stop Co-Pilot" for Southwest Airlines at Kansas City (MCI). It is an agentic AI operations layer that sits ON TOP of Southwest's existing systems (they stay the official record). It handles any ground stop (weather, FAA program, IT outage, airport, security), from the airline's, airport's, passengers' and employees' points of view. Its focus is making sure every passenger hears the same true story from every channel (app, SMS, gate screen, call center). All data is synthetic, built from class files in data/raw/. Simulation date 2026-10-06, America/Chicago.

ENVIRONMENT
Windows, PowerShell, VS Code, Python venv (.venv), PostgreSQL on Neon (cloud, about 50–100 ms per round trip, so batch writes). No Docker. psycopg v3, pytest. Repo: github.com/ruzakansakar/mci-storm-ops.

AGREED ARCHITECTURE (decided by the project owner; do not change)
- Orchestrator: plain code, no AI. Routes work, runs the rule check on every proposal, catches conflicts between agents, escalates to humans. NOT BUILT YET.
- 5 agents: situation (built), communication (part 1 built), network_recovery (flights, aircraft AND pilots/flight attendants), station_staff (ground staff, gates, ramp, deplaning help), passenger_care (rebooking, hotels, money, vulnerable passengers).
- There is NO guardrail agent and NO crew agent. Rule checking is code in backend/rules/check.py, called by the orchestrator. The orchestrator can block but never override; only a named human can override, only where a law rule allows it, and it is logged (backend/rules/override.py).
- AI is used sparingly: only Situation summaries and Communication message drafts, always with a template fallback. Currently AI is OFF everywhere.

KEY FILES
- backend/db/schema.sql: all tables. events and audit_log are append-only (triggers). situation_assessments is append-only.
- backend/seed/ (class_files.py, build.py, load.py) + config/seed.yaml: initial data. scripts/reset_db.py rebuilds the database.
- config/rules/ (law.yaml, southwest.yaml, operations.yaml, safety_ladder.yaml, autonomy.yaml): every rule, each with source/citation/enforced/checked_by. backend/rules/: the checks (safety ladder, crew legality, tarmac, curfews, compensation, check.py, override.py).
- config/agents/*.yaml: one contract per agent (reads, writes, actions with enabled_in phase, never-list, stores, output schema, max_data_age_sec, time_budget_sec, escalate_when, ai_allowed, success). config/agents/_shared.yaml: 11 shared AI guardrails (G01–G11).
- backend/contracts/: AgentContext (every agent reads and writes ONLY through it; it enforces the contract, strips non-public payload fields, cuts reads at time T, checks freshness, enforces privacy, schema, decision ids, audit log), loader, review, decisions.
- config/decisions.yaml: 195 decisions (who proposes, who decides: auto / needs_approval + role / human_only, rules, triggers).
- simulator/ (run, scenario, world, channels, store, report): plays a day minute by minute. Today mode = Southwest's current process with no co-pilot. Channels copy the truth late; some updates are lost. config/simulator.yaml holds every number (frozen; don't tune).
- backend/agents/situation/: Situation agent (state_at.py rebuilds the world at time T from events only; assess.py rules; run.py --at / --replay). Settings: config/agent_settings/situation.yaml.
- backend/agents/communication/: Communication agent part 1, detection only (state_at.py, detection, run.py --at / --replay / --passenger). Settings: config/agent_settings/communication.yaml.
- chaos/: random worst-case days (generator.py, sealed truth files), judge.py (Situation), judge_communication.py, calibrate.py, memory_replay.py (fast in-memory replay, about 0.3 s a day, use it for batches), pages.py, run.py.
- reports/: HTML pages opened by double-click (simulator.report, chaos pages).

THE SEED 7 STORY (locked by a test; must never change)
python -m simulator.run --seed 7 --scenario a31_storm gives: thunderstorm ground stop at MCI 11:30–14:00 (first announced to end 13:00, extended once), ramp closed for lightning 12:10–12:45; 17 delayed, 5 cancelled (WN1032, WN1033, WN1035, WN1038, WN1040), 9 diverted, 559 stranded overnight. A31 (WN2031) from Denver holds, diverts to STL at 12:15 and lands at MCI at 14:10. WN1033 (Maya Torres's Baltimore flight) gets reserve pilots R04, then is cancelled for flight attendant duty; Maya (PAX0001) is rebooked overnight on WN3005 at 06:30 on 2026-10-07. In today mode her four screens catch up 17–37 minutes late (app 15:12, sms 15:14, gate 15:20, call center 15:32, stuck on "diverted" from a lost update).

PROTECTIONS EVERY AGENT MUST HAVE (enforced in AgentContext, not in agent code)
1. No answer key: agents never read simulator internals (simulator incidents, seed, scenario, simulator.yaml, metric_runs, chaos truth files, payload fields a real source wouldn't publish such as lag_min or cause_if_late).
2. No future: every read is cut at the agent's time T; state is rebuilt from events up to T, never from end-of-day tables.
3. Freshness: per-feed heartbeats; a stale feed means "cannot assess / cannot check", never a guess.
4. Privacy: passenger names and contacts only for passenger_care and communication (from 4b-3 on), and only for passengers they declare they work on.
Agents never write truth tables (flights, bookings, passengers, crews, aircraft, bags, stations). Truth changes only through an approved action.

WORKING RULES
- Every number in a config file, marked "# assumption" (or its source).
- Fix root causes; never special-case a test, a seed or a chaos event name. Never loosen a judge to pass; if a judge is wrong, fix it and list the change with its reason.
- Calibrate only on chaos seeds 1–100; evaluate on fresh, untouched seeds.
- Use the in-memory replay for batches; use the database only for seed 7 and final checks.
- The project owner is not a programmer: final replies are plain English, no jargon, in the required format.

BUILD PHASES
Done: 1 data, 2 rules, 3 simulator, rule catalog + contracts + decision catalog, 4a Situation agent (tested on about 220 chaos days), 4b-1 Communication detection.
Now: 4b-2 Communication fixes + co-pilot mode implemented; finish the recorded verification before merging.
Next: 4c shared state (Communication reads situation_assessments), 4b-3 messages, then the remaining agents one at a time, then the orchestrator, API, front end, AI pieces, scale tests, demo.

PHASE 4b-2 IMPLEMENTATION
- backend/agents/communication/fix.py proposes grouped confirmed corrections; critical cases skip settling; stale feeds and open conflicts hold. Policy thresholds are in the Communication settings.
- backend/execution/executor.py is reusable check-and-dispatch plumbing, not the orchestrator. backend/execution/copilot.py connects the minute loop through BufferedContext (an AgentContext storage adapter).
- Only fix_channel_mismatch and resend_on_other_channel were newly enabled. Message actions remain 4b-3. The contact_channel_types API exposes only supported channel type names; raw contacts and names remain forbidden.
- config/channel_causes.yaml is the shared cause vocabulary. Raw simulator events retain legacy labels for byte-for-byte today-event compatibility; reported labels are canonical. Changed itineraries are not evidence of seat-only holds.
- Corrections use normal channel delay/load/jitter/failure draws, on a separate random stream. New correction requests can race pending copies; version checks prevent older copies undoing newer corrections. Already confirmed seats do not repeat reservation holds. Trip and baggage delivery components retain separate delays.
- Run today first, then copilot. Today resets the database; copilot preserves today's records, writes its own events/metrics and never updates operational truth tables. Run-local proposal IDs in execution events correspond to local_proposal_id in saved recommendation options.
- simulator/comparison.py measures both modes; simulator/comparison_page.py renders the paired report. chaos.compare (also chaos.memory_replay --compare) evaluates seed 7 plus 20 fresh recorded seeds, with published chaos overlays, late deliveries and feed outages.
- Recovery includes lost updates. Catch-up median/worst cover completed trip versions only; superseded and unfinished versions are separate counts. Extra correction pushes and total trip updates are both reported.
- Baseline: 542 passed. Original vs new today-mode seed 7 operational truth and complete event streams have identical SHA-256 fingerprints. Cause accuracy changed from 95.7% to 89.7%; normal wait vs stuck is still inferred. Do not loosen the judge or tune on evaluation seeds.


