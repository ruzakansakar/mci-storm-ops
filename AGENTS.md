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
- config/agents/*.yaml: one contract per agent (reads, writes, actions with enabled_in phase, never-list, stores, output schema, max_data_age_sec, time_budget_sec, escalate_when, ai_allowed, success). config/agents/_shared.yaml: 11 shared AI guardrails (G01–G11) and 7 shared-state rules (SS1–SS7).
- backend/contracts/: AgentContext (every agent reads and writes ONLY through it; it enforces the contract, strips non-public payload fields, cuts reads at time T, checks freshness, enforces privacy, schema, decision ids, audit log), loader, review, decisions.
- config/decisions.yaml: 195 decisions (who proposes, who decides: auto / needs_approval + role / human_only, rules, triggers).
- simulator/ (run, scenario, world, channels, store, report): plays a day minute by minute. Today mode = Southwest's current process with no co-pilot. Channels copy the truth late; some updates are lost. config/simulator.yaml holds every number (frozen; don't tune).
- backend/agents/situation/: Situation agent (state_at.py rebuilds the world at time T from events only; assess.py rules; run.py --at / --replay). Settings: config/agent_settings/situation.yaml.
- backend/agents/communication/: Communication agent (state_at.py, detect.py, fix.py, situation_view.py = what it knows from Situation, run.py --at / --replay / --passenger). Settings: config/agent_settings/communication.yaml.
- backend/contracts/shared_state.py: the shared-state rules in code (SHARED, NEVER_SHARED, visible, project, heartbeat, disagreement). backend/contracts/buffered.py: AgentContext over an in-memory SharedStore for the co-pilot minute loop.
- backend/screens.py: which of the four screens a passenger has (gate screen + call center for all; app/SMS only if on file). Used by seed, simulator, agents and metrics.
- chaos/: random worst-case days (generator.py, sealed truth files), judge.py (Situation), judge_communication.py, calibrate.py, memory_replay.py (fast in-memory replay, about 0.3 s a day, use it for batches), pages.py, run.py.
- reports/: HTML pages opened by double-click (simulator.report, chaos pages).

THE SEED 7 STORY (locked by a test; must never change)
python -m simulator.run --seed 7 --scenario a31_storm gives: thunderstorm ground stop at MCI 11:30–14:00 (first announced to end 13:00, extended once), ramp closed for lightning 12:10–12:45; 17 delayed, 5 cancelled (WN1032, WN1033, WN1035, WN1038, WN1040), 9 diverted, 559 stranded overnight. A31 (WN2031) from Denver holds, diverts to STL at 12:15 and lands at MCI at 14:10. WN1033 (Maya Torres's Baltimore flight) gets reserve pilots R04, then is cancelled for flight attendant duty; Maya (PAX0001) is rebooked overnight on WN3005 at 06:30 on 2026-10-07. Maya has app + SMS on file (a story override in config/seed.yaml, applied after her random draw). In today mode her four screens catch up 17–37 minutes late (app 15:12, sms 15:14, gate 15:20, call center 15:32, stuck on "diverted" from a lost update).

PROTECTIONS EVERY AGENT MUST HAVE (enforced in AgentContext, not in agent code)
1. No answer key: agents never read simulator internals (simulator incidents, seed, scenario, simulator.yaml, metric_runs, chaos truth files, payload fields a real source wouldn't publish such as lag_min or cause_if_late).
2. No future: every read is cut at the agent's time T; state is rebuilt from events up to T, never from end-of-day tables.
3. Freshness: per-feed heartbeats; a stale feed means "cannot assess / cannot check", never a guess.
4. Privacy: passenger names and contacts only for passenger_care and communication (from 4b-3 on), and only for passengers they declare they work on.
Agents never write truth tables (flights, bookings, passengers, crews, aircraft, bags, stations). Truth changes only through an approved action.

SHARED STATE RULES (Phase 4c; config/agents/_shared.yaml shared_state, enforced in backend/contracts/shared_state.py through AgentContext)
SS1. Agents never call each other. They share information only through shared tables and events (no agent imports another; tested).
SS2. Every shared record is append-only and versioned, and carries its producer, its time (assessed_at / as_of) and its sources.
SS3. A reader only sees records with time <= its current time T (no future, not even later in the same minute).
SS4. A reader's contract (shared_reads) lists exactly which producer fields it may read; everything else is refused. Some fields can never be shared (flight_controllability: it decides compensation, only after a human approves; evidence internals).
SS5. Suspicions stay suspicions: suspected causes and estimates reach the reader only under "suspected", with their confidence.
SS6. Freshness is about the producer, not the record. A producer writes a version only when something changes, so an old version is not stale; a producer that stopped RUNNING is. Every run writes an agent_heartbeat event; readers measure staleness from it.
SS7. When two agents disagree, both views are logged (agents_disagree) and a human is asked. Resolving it is the orchestrator's job later.

WORKING RULES
- Every number in a config file, marked "# assumption" (or its source).
- Fix root causes; never special-case a test, a seed or a chaos event name. Never loosen a judge to pass; if a judge is wrong, fix it and list the change with its reason.
- Calibrate only on chaos seeds 1–100; evaluate on fresh, untouched seeds.
- Use the in-memory replay for batches; use the database only for seed 7 and final checks.
- The project owner is not a programmer: final replies are plain English, no jargon, in the required format.

BUILD PHASES
Done: 1 data, 2 rules, 3 simulator, rule catalog + contracts + decision catalog, 4a Situation agent (tested on about 220 chaos days), 4b-1 Communication detection, 4b-2 Communication fixes + co-pilot mode, 4c shared state (Situation -> Communication) + channel data fix, 4b-3 messages + conflict holds that clear.
Next: the remaining agents one at a time (Network Recovery, Passenger Care, Station & Staff), then the remaining agents one at a time, then the orchestrator, API, front end, AI pieces, scale tests, demo.

PHASE 4b-2 IMPLEMENTATION
- backend/agents/communication/fix.py proposes grouped confirmed corrections; critical cases skip settling; stale feeds and open conflicts hold. Policy thresholds are in the Communication settings.
- backend/execution/executor.py is reusable check-and-dispatch plumbing, not the orchestrator. backend/execution/copilot.py connects the minute loop through BufferedContext (an AgentContext storage adapter).
- Only fix_channel_mismatch and resend_on_other_channel were newly enabled. Message actions remain 4b-3. The contact_channel_types API exposes only supported channel type names; raw contacts and names remain forbidden.
- config/channel_causes.yaml is the shared cause vocabulary. Raw simulator events retain legacy labels for byte-for-byte today-event compatibility; reported labels are canonical. Changed itineraries are not evidence of seat-only holds.
- Corrections use normal channel delay/load/jitter/failure draws, on a separate random stream. New correction requests can race pending copies; version checks prevent older copies undoing newer corrections. Already confirmed seats do not repeat reservation holds. Trip and baggage delivery components retain separate delays.
- Run today first, then copilot. Today resets the database; copilot preserves today's records (truth tables AND channel_views, which hold today's screens), writes its own events/recommendations/metrics under the -copilot run id and never updates operational truth tables. A second copilot run is refused until today is rerun. Run-local proposal IDs in execution events correspond to local_proposal_id in saved recommendation options.
- simulator/comparison.py measures both modes; simulator/comparison_page.py renders the paired report. chaos.compare (also chaos.memory_replay --compare) evaluates seed 7 plus 20 fresh recorded seeds, with published chaos overlays, late deliveries and feed outages.
- Recovery includes lost updates. Catch-up median/worst cover completed trip versions only; superseded and unfinished versions are separate counts. Extra correction pushes and total trip updates are both reported.
- Retry policy: a version is not repeated on a channel before max(retry_min, the channel's usual lag as the agent measured it today) unless a failed-delivery receipt arrived since; a resend to another channel happens once per trip version. Maya's id for the comparison comes from config/comparison.yaml (focus_passenger), never hard-coded.
- Verified result (seed 7): passenger-minutes wrong 13,625 -> 6,152; critical 1,594 -> 570; lost updates still wrong 12 -> 0. 20 fresh days (seeds in reports/chaos/comparison-seeds.json): average 51% fewer, worst 13%, no day worse, truth identical on all.
- Baseline before 4b-2: 542 passed; after: 558. Original vs new today-mode seed 7 operational truth and complete event streams have identical SHA-256 fingerprints. Cause accuracy changed from 95.7% to 89.7%; normal wait vs stuck is still inferred. Do not loosen the judge or tune on evaluation seeds.

PHASE 4c IMPLEMENTATION (shared state + channel data fix)
- Screens: gate_screen and call_center for everyone; app/SMS only if in contact_channels (backend/screens.py; seed views, simulator Channels, detection via the passenger_screens projection, fixes, resends, rule check, metrics, pages). A screen a passenger lacks still consumes its two random draws, so every existing screen keeps its exact delays and failures (seed 7 story unchanged). Contacts: app+sms 197, sms 65, app+email 47, email 11. Maya: app+sms (config/seed.yaml story_overrides).
- tests/test_seed.py: new checksum, and undoing exactly the two deliberate changes gives back the old checksum (proof nothing else moved). World truth (flights, aircraft, crews, bookings, bags, trip records, non-channel events) identical to before.
- Contract additions: projections (passenger_screens 4b-1, contact_channel_types 4b-2) and shared_reads (Communication -> situation_assessments, whitelisted fields, conflict_fields key/flight_ids/fields, heartbeat). Situation writes agent_heartbeat each run; its conflicts now carry flight_ids and fields.
- Co-pilot minute loop (backend/execution/copilot.py): Situation first (SituationRunner: exactly its standalone wake schedule), then Communication (detect, read_situation, propose_fixes), then the executor. Situation's 18 seed 7 versions in copilot equal its standalone replay (tested; in the database too). Copilot Situation versions are saved under agent_run_id '<run id>:situation'.
- Communication uses Situation for: conflict holds (fixes held on a flight while Situation reports conflicting data; resume when a later version clears it), Situation unavailable (heartbeat older than situation.unavailable_after_min: logged, fixes continue, escalation after escalate_after_min), cannot_assess (recorded), the latest picture kept for 4b-3 (not used in content). Every read is audited with its version; every fix and hold carries situation_version.
- Codex's 4b-2 local conflict guess (cancel + depart events) and the fake source_conflict_* feed types were removed: conflicts come only from Situation.
- Finding: Situation keeps a "departed while the ramp was closed" conflict open all day (nothing in the data resolves it), so conflict holds last until day end. Held passengers were never worse off than today. Resolution needs a human/orchestrator path (later).
- Today mode is locked byte-for-byte by tests/test_shared_state.py (events + audit hashes).
- Results: seed 7 passenger-minutes wrong 11,744 today vs 6,190 with co-pilot (before the channel fix: 13,625 vs 6,152). Same 20 days: average 48% fewer (was 51%), worst 11%, none worse. 20 extra conflict/outage days (reports/chaos/comparison-extra*.json/html): average 49% fewer, worst 30%, none worse, 115 conflict holds on 8 days.
- Tests: 558 before 4c; 573 after.

PHASE 4b-3 IMPLEMENTATION (messages + conflict-hold fix)
- Situation (assess.py _lifecycle/_settle, settings conflicts): conflicts are opened once, then settled by later evidence (with side + evidence), resolved by a human (human_decision_recorded on the new "decisions" feed; trigger event), or marked unresolved after unresolved_after_min. Conflict entries carry status, sides, opened_at, side, settled_by; Communication may read those (shared_reads conflict_fields). Seed 7 Situation versions and the 100 training-day summary are unchanged (no flight conflicts there).
- Communication holds only the conflicted trip details (settings conflicts.hold_fields; a fix proposal lists held_fields and the rule check allows leaving a detail out only under an open conflict). After ask_after_min it raises A09 (resolve_source_conflict, network_operations_control; A09 also_raised_by communication in decisions.yaml + build script), urgent after urgent_after_min. Never picks a side.
- backend/execution/simulated_human.py + config/simulated_humans.yaml: co-pilot mode only; answers what its role handles after a response time; never sees blocked proposals; conflicts answered from the simulator truth (simulator/run.py _window); events actor "simulated_human:<role>". Situation's A02/A03 etc. are deliberately not answered.
- Messages: backend/agents/communication/messages.py (plan_messages, draft, claims), config/message_templates.yaml, backend/rules/message_check.py (in the rule check). O04 and O14 now use draft_message (needs approval) in decisions.yaml and the build script. Names: first name, app only, working_on scope (BufferedContext enforces like AgentContext). Messages travel through Channels.deliver_messages (own random stream; dropped if the trip facts change before arrival). Settings: messages.* in the Communication settings.
- Metrics: simulator/message_metrics.py (inform time, per passenger, blocked, dropped, withdrawn, approval waits, promises found, next-update promises kept). Pages: Maya's message timeline, staff notes, summary in the comparison section.
- Results: seed 7 wrong-screen passenger-minutes 11,744 today vs 6,180 co-pilot; 828 messages, 0 promises found, 0 blocked, every cancelled or rebooked passenger told (median 19 min), Maya's cancellation approved after 15 min and delivered at 15:14. Conflict days: hold length 315-805 min -> 0-12 min; those passengers' wrong-screen minutes 2,622 today / 2,419 co-pilot before / 1,764 after.
- Message fix (branch fix-messages): messages know when the passenger is in the air (flight_airborne / flight_holding added to the Communication feed; airborne until landed, diverted or cancelled) and then say "When you land in X, please check this app for your connection" (templates: airborne). Cancellation and rebooking notices that only state confirmed facts are automatic (O14 = send_status_update, auto); options, overnight arrangements, corrections and mass batches still need the supervisor. "No change yet" every 60 min. Seed 7: 458 messages (was 828), 1.5 per passenger (was 2.7), Maya's cancellation notice at 14:59 (was 15:14 after approval); wrong screens unchanged (6,180).
