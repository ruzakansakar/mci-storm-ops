# Live human-in-the-loop API

The world (weather, flights, the storm, passengers, the four screens) is simulated. The agents run on it minute by minute. You
are the human in the loop: every decision that needs a person comes to you, and you approve, reject, wait or hand off.
Everything is saved.

- Code: `backend/api/live.py` (routes), `simulator/live.py` (session and clock), `backend/execution/live_human.py` (you, in front
  of the simulated people), `backend/execution/live_trace.py` (why and trace). Settings: `config/live.yaml`.
- Times are `"HH:MM"` local (America/Chicago). `minute` is minutes after midnight.
- Routes that write:
  - `start`, `control` and `decide`;
  - from the floor: `reports`, flight `notes` (POST), and a Customer's `rebook` and `refund`.

  Every other route only reads.
- All user-facing text says "Customers" (Southwest's word). Headlines, stories, trace labels and the agents' sentences never
  contain code words, rule ids or version numbers.
- Errors: `detail` is always one plain sentence (never a list or an object). Extra facts sit beside it, for example
  `{"detail":"another open question depends on this one","blocked_by":[{"id":14,"title":"..."}]}`. Unknown body fields (for
  example `"to_role"`) are accepted and ignored.
  - `404`: unknown run or question.
  - `409`: a blocked wait, a question already answered, a session that has ended, or results asked for before the end of the day.
  - `422`: invalid input.
- After a server restart, a session is read-only (it cannot be resumed).

## POST /api/live/start

```
body {"seed":7,"scenario":"a31_storm","roles":["station_manager","customer_service_supervisor"]}
->   {"run_id":"a31_storm-s7-live-1"}
```

- `scenario` is `"a31_storm"` or `"random"`.
- `roles` only filter your default view of the queue. You answer for every role.
- The run id is `<scenario>-s<seed>-live-<n>`.
- The world is built exactly as orchestrated mode builds it.
- 05:00 to `live_from` (default 11:00) runs at once, with the simulated people answering.
- If the matching today run is missing, it is made first. It is the minute-by-minute comparison.

## POST /api/live/{run_id}/control

```
body {"action":"pause"|"resume"|"speed","speed_idle":5,"speed_pending":0.1667}
->   state
```

Speeds are simulated minutes per real second:
- `speed_idle` applies while nothing waits for you;
- `speed_pending` applies while something does.

Both are limited to `clock.speed_min`–`clock.speed_max`.

The clock pauses by itself when no client has called `GET state` for `heartbeat_timeout_s` (default 20 s). `resume` starts it
again.

## GET /api/live/{run_id}/state

This call is also the heartbeat.

```
{"run_id":"","status":"running","minute":662,"clock":"11:02","speed_idle":5,"speed_pending":0.1667,
 "roles":[],"pending":3,"urgent_pending":1,"incidents":[{"id":"A10","status":"open","title":""}],
 "world":{"ground_stop":true,"ramp_open":true,"stop_expected_end":"13:00"},
 "truth":{"wrong_now":120,"wrong_minutes_total":3400,"today_wrong_now":260,"today_wrong_minutes_total":5100},
 "focus":{"passenger_id":"PAX0001","name":"Maya Torres","true_status":"",
          "screens":[{"channel":"app","shows":"","wrong":false,"since":"11:00"}]}}
```

- `status`: `running`, `paused`, `finished` or `stopped`.
- `incidents[].id`: the orchestrator's incident number, as text (for example `"1"`).
- `truth`:
  - `wrong_now`: passengers with at least one screen that disagrees with the truth now.
  - `wrong_minutes_total`: passenger-minutes wrong since 05:00. This is the same count every mode reports (simulator/metrics.py).
  - The `today_` fields: today's run at the same minute.
- `focus`:
  - seed 7: Maya Torres (config/comparison.yaml);
  - random day: the tracked passenger with the most trip changes so far, picked once.
  - `screens` lists only the screens they have (backend/screens.py): app and SMS only if on file, gate screen and call center for
    everyone.
  - `since`: when the screen started being right or wrong.

## GET /api/live/{run_id}/queue?roles=station_manager,crew_scheduling

- With no `roles`, you get the roles chosen at start (everything if none were chosen).
- `roles=all` gives everything.

```
{"items":[{"id":12,"group_key":"station_staff:move_gate_agents:662","group_size":4,"agent":"network_recovery",
  "action":"extend_crew_duty","role":"crew_scheduling","role_title":"Crew scheduling","title":"",
  "target":{"flight_id":"WN1031","crew_id":"C31","passenger_ids":[]},"asked":"11:30","deadline":"11:45",
  "minutes_left":13,"urgent":true,"priority_score":91,"safety_legal":true,"passengers_affected":143,
  "passengers_seeing_wrong_info_now":0,"status":"waiting"|"snoozed"|"escalated",
  "snoozed_until":null,"can_wait":false,"blocked_by":[{"id":14,"title":""}],
  "last_option":false,"consequence_if_rejected":"",
  "why":{"plan":"","trigger":[{"t":"11:30","source":"faa_feed","text":""}],
         "inputs":[{"kind":"situation","version":4,"t":"12:15"}],"options_considered":[{"option":"","why_not":""}],
         "rule_check":{"ok":true,"rules":["CREW-01"],"text":""},"why_human":""}}]}
```

- Items are ordered by `priority_score`, highest first. Weights are in config/live.yaml. The order is:
  1. safety or legal (fuel, crew legality, tarmac 3 h, children flying alone, wheelchair passengers);
  2. then the earliest deadline;
  3. then passengers affected.
- `group_key`: similar questions (same agent, action and minute, none depending on another) share one, and can be decided in one
  call.
- `status: "escalated"`: nobody answered by the deadline, so the duty manager (simulated) was asked too. The first answer counts.
- `passengers_seeing_wrong_info_now`: for a passenger message, how many of its passengers see wrong information now, until it is
  approved. It is 0 for questions that do not change what passengers see.
- `can_wait: false`: another open question depends on this one (`blocked_by`).
- `can_hand_off` (true or false) and `hand_off_reason`: false only for human-only decisions, where you are the named person
  and the duty manager can't take it.
- `last_option: true`: you said no once and the agent has no other plan. A second no applies `consequence_if_rejected` through
  today's process.
- `why.options_considered`: only options the agent itself produced. It is often empty.
- **Human-only decisions** (for example D02, extend crew duty) come to you as the named person, in the queue of the role that
  holds them. D02 goes to `crew_scheduling` (config/live.yaml `human_only.roles`).
  - The rule check still runs. Its "human-only: the co-pilot can only show facts" block is expected; any other block keeps the
    question from you.
  - They cannot be handed off (`409`), and the duty manager never answers them, even after the deadline.
  - Your answer is recorded as `human_decision_recorded` (actor `human:<role>`). Nothing in the co-pilot carries it out.
  - The same why, trace and last-option rules apply.

### Added to every queue item (branch live-ui); every field above is kept

```
"headline":"Keep WN1031 to Denver flying today: extend the pilots' duty",
"story":[{"t":"12:15","text":"Thunderstorms and lightning at MCI. FAA ground stop until about 13:00; 75% sure it lifts by 14:30."},
         {"t":"12:16","text":"Network Recovery asked a person to decide; it passed the rule check and went to crew scheduling."}],
"recommendation":"...",
"options":[{"id":"opt1","label":"...","why":"...","recommended":true,"effect":"..."},
           {"id":"opt2","label":"Use reserve R12 instead","crew_id":"R12","why":"legal too; the agent ranked it lower",
            "recommended":false,"effect":"..."}],
"effect":{"customers":158,"connections":16,"flights":1,"text":"Touches 158 Customers, 16 connecting, 1 flight (WN1031)."},
"if_declined":"...",
"bundle":{"id":"incident-1","title":"Ground stop at MCI, 11:30","started":"11:30"},
"section":"urgent"|"routine",
"allowed_changes":{"gate":["G06","G07"]} | {"delay_min":[0,15,30,60]} | {"crew_id":["R12","R09"]} | {"text":"..."} | null
```

- **Plain language.** Everything is built only from recorded data:
  - `headline`, `story`, `recommendation` and `if_declined` come from the proposal, the agent's recommendation, the events
    the agent was given and the shared versions.
  - `options` lists the recommended option first, then only the real alternatives the agent produced. For a crew action these
    are the other legal reserves and crews; for a cancellation, the delay it was compared with. Otherwise there is one option.
- **Human-only questions** (for example extending crew duty): `recommended` is false on every option; the co-pilot only shows
  the facts.
- **`bundle`**: the incident that caused the question. A question with no incident goes to
  `{"id":"normal","title":"Normal operations"}`.
- **`section`**: `urgent` if the question is urgent, safety or legal, or due soon; otherwise `routine`.
- **`group_key`**: agent, action, role and bundle, for questions that don't depend on each other. Hard decisions never share
  one: crew, diversion, cancellation, delay and every rebooking.
- **Automatic actions** never appear in the queue. These are, per `config/autonomy.yaml`:
  - refunds the DOT automatic refund rule requires;
  - hotel rooms within the nightly budget;
  - rides within the contracted limit.

  Each is logged with actor `copilot:auto`. It appears in the history (`"answer":"auto"`), in the day summary
  (`results.decisions.auto`) and in its trace ("Done automatically: <rule>"). A setting that would automate a safety or legal
  item is refused when the file is loaded.
- **The frontline role `counter_gate_agent`** has no approval queue: `?roles=counter_gate_agent` returns `[]`.
- **A Passenger Care item** also has `care`:

  ```
  "care":{"customers":[{"id","name","action":"rebook"|"hotel"|"refund"|"ride",
                        "detail":{"flights":[{"flight_id","dep","arr"}],"hotel":{"name","price","shuttle"},"amount":0,"currency":"USD"},
                        "why":"plain reason"}],
          "message_text":"exact words Customers will get" | null,
          "auto_done":[{"action":"refund","count":0,"total":0}]}
  ```

  It is built from Passenger Care's own proposal: its option (the flight, or the hotel and its cost), the hotel list it was
  given, and its reason.
  - `message_text` is the text of Communication's message backed by this decision. Until Communication has written one (it
    does so after the approval), it is `null`.
  - A refund's `amount` is `null`: the ticket price is not on record.
  - A ride's `amount` is the whole ride, shared when several Customers go together.
  - `auto_done` counts what Passenger Care already did on its own today, by kind, with the total cost.
  - Nothing is rebooked until a person approves.
  - `approve_with_change` can carry `{"customer_id","option_id"}`: that Customer takes another of their options (from
    `GET /customers/{id}` `rebooking`, never the proposed flight) through the same rebooking path (K01, the rule check, the
    reservation system) at the next minute; the other Customers keep the proposed flight. `allowed_changes` lists
    `customer_id` and, per Customer, `option_id` only when such another flight exists.

## GET /api/live/{run_id}/questions/{id}/trace

The flowchart of one decision: from the outside world to the question, and after you answer, on to what it did. It is saved
when asked and when answered; at the end of the day the continued flowchart is saved once more.

```
{"question_id":12,"snapshot":"asked"|"answered",
 "nodes":[{"id":"n1","kind":"feed"|"situation"|"network_recovery"|"passenger_care"|"station_staff"|"communication"|"orchestrator"
                            |"rule_check"|"question"|"human"|"change"|"channel"|"staff",
           "label":"","t":"11:30","detail":{}}],
 "edges":[{"from":"n1","to":"n2","label":"used"|"sent to"|"answered"|"confirmed","t":"11:30"}],
 "preview":{"changes":[{"label":""}],"destinations":[{"key":"","label":""}]}}
```

Before the answer, the nodes run from the world to the question:
- the events that started it (`feed`);
- the shared versions it was built on (`situation`, `network_recovery`, ...);
- the proposing agent, then `rule_check`, then `orchestrator`, then `question`.

Edges: `used` from each input to the next agent; `sent to` from agent to rule check, rule check to orchestrator, and orchestrator
to question.

**Before the answer, `preview`** says what would change and who would be told. It is built from the proposal only, and left
empty where unknown:
- **Changes:**
  - a delay: `"WN1034 departs at 13:43"`;
  - a cancellation: `"WN1034 is cancelled"`;
  - a crew action: `"crew R07 flies WN1030 as its flight attendants"`;
  - care, station and human-only questions: their title.
- **Destinations:**
  - a delay or cancellation: the flight's passengers' screens, from the trip records the agents were given;
  - a rebooking: its passengers' screens;
  - a station task: the staff roles it names;
  - a message proposal: the channels it lists (a staff note goes to `gate_agents` and `call_center_agents`).

**After the answer**, there is a `human` node (edge `answered`), then:
- `change`: each change the decision made. Each one is a real confirmation event, and `detail.event` names it:
  - `recovery_action_applied`;
  - `reserve_crew_assigned` / `crew_reassigned`;
  - a flight's new time (`flight_delayed` / `flight_delay_updated`) and `flight_cancelled`;
  - `gate_assigned`, `staff_assigned`, `staff_called_in`, `hotel_room_assigned`;
  - the care and station decision records;
  - `decision_applied` / `decision_refused` (a refusal is shown as "not carried out: <reason>");
  - `human_decision_recorded`.
- `communication`: the Communication messages it caused:
  - recovery messages drafted for the proposal (one node per audience, with its statuses);
  - the messages of an approved message proposal;
  - the messages backed by an approved care decision (hotel or ride).
- **destinations**: one node per destination, either `channel` (`app`, `sms`, `gate_screen`, `call_center`) or `staff`
  (`counter`, `gate_agents`, `call_center_agents`, `crew`, `ramp`, `hotel_desk`, `wheelchair_agents`).
  - `detail.status`:
    - `"waiting"` if any message has not arrived yet (or is held for approval);
    - otherwise `"failed"` if any failed or was dropped;
    - otherwise `"delivered"`.
  - Counts are in `detail.delivered`, `detail.failed` and `detail.waiting`.
  - `detail.text`: the exact words sent.
  - `detail.key`: the destination key.
  - `detail.confirmed_by`: which record says so. It comes from `message_delivered`, `message_dropped`, `channel_update_failed`,
    `crew_message_delivered`, `staff_note_published` or `channel_view_updated`. An internal staff note has no delivery receipt;
    Communication records it sent itself, and the node says so.
  - Screen updates (`channel_view_updated`) appear only when the change actually changed passengers' trip records.

Edges: `confirmed` from the human to each confirmed change and to each delivered destination; `sent to` from the change (or
the human) to Communication, and to a destination that is still waiting or failed.

## POST /api/live/{run_id}/decide

```
body {"question_ids":[12],"action":"approve"|"approve_option"|"approve_with_change"|"reject"|"wait"|"hand_off",
      "option_id":"opt2","change":{"gate":"G07"},"reason":"","minutes":10}
->   {"ok":true,"results":[{"id":12,"status":"approved","error":null,"done":"Approved with your change: WN2034 goes to gate G07"}]}
```

- **`approve_option`** carries out that option instead of the recommended one:
  - another legal crew is assigned;
  - "delay instead" declines the cancellation for good, and Network Recovery plans a delay;
  - "do not extend" is your final no.
- **`approve_with_change`** accepts only what `allowed_changes` lists (`config/live.yaml`):
  - `gate`: a gate free for the plane's whole stay;
  - `delay_min`: minutes added to the proposed time;
  - `crew_id`: another legal reserve or crew the agent found;
  - `text`: new wording for a message with one wording for everyone;
  - `extension_min`: for an approved duty extension.

  The changed proposal goes through the rule check again. A change that breaks a rule is a `409` with the reason in plain
  words (for example "This change breaks a rule: rung 4 (legal): crew would end with 16 min of duty left (need a 30 min
  buffer)"). Changed wording keeps the message's facts and checks, so a time or flight not backed by a fact is refused.
- **Human-only decisions take effect.** Approving a pilot duty extension (D02) is carried out by the world at the next minute,
  as an approved action is in orchestrated mode:
  - the pilots' duty is extended by the approved minutes (up to the legal 2 hours, once per crew);
  - the flight can depart;
  - the trace shows the changes ("Pilots' duty extended by 120 min for WN1031", "WN1031 departed at 14:45").

  A rejection keeps today's behaviour.

- `reject` needs a `reason`. `wait` needs `minutes` of 10 or 15, and is refused (`409`, `blocked_by` in the answer) while another
  open question depends on this one.
- A snoozed question still escalates at its deadline.
- `hand_off` gives the question to the duty manager, who answers in their usual 3–10 minutes (2–5 if urgent).
- With several ids, each is decided on its own:
  - `ok` is false if any item failed (see `results[].error`);
  - if every item failed, the first failure's status code is returned.
- Every answer is an event with actor `human:<role>`:
  - `human_approved`, `human_rejected`, `human_waited` or `human_handed_off`;
  - then the agent's own records (for example `network_approved` or `decision_rejected`).

## GET /api/live/{run_id}/flow?since=660

```
{"agents":[{"key":"situation","name":"Situation","last_run":"11:02","versions":[{"v":4,"t":"12:15","used":[{"kind":"feed","ref":"faa_feed"}]}],"waiting_on_human":0}],
 "orchestrator":{"open_incidents":1,"queue":3},
 "edges":[{"from":"situation:4","to":"network_recovery:102","t":"12:16"}]}
```

- `versions`: the shared versions each agent wrote since `since`, each with the upstream versions it recorded using.
- Communication's versions: each batch of recovery messages it drafts or sends, and each passenger or mass message proposal it
  drafts or sends, is one version.
  - It is numbered over the whole day, so a version keeps its number.
  - Each version has `what` and `messages` (how many), and `used` points to the recovery-plan version
    (`{"kind":"network_recovery"}`) or the Situation version (`{"kind":"situation"}`) it was built on. Each `used` is also an
    edge to `communication:<v>`.
- `queue` counts the questions waiting for you.

## GET /api/live/{run_id}/history

```
{"decisions":[{"id":12,"title":"","role":"","action":"","answer":"approved","reason":"","asked":"","answered":"","waited_min":4}]}
```

`answer` is one of `approved`, `rejected`, `waited`, `handed_off` or `escalated` (answered by the duty manager after the
deadline). `waited_min` is the minutes from asked to answered; for a question you only put off, it is the minutes you waited.

## Reports from the floor and flight notes

```
POST /api/live/{run_id}/reports {"kind":"gate_unavailable"|"jet_bridge_slow"|"long_queue"|"special_assistance_waiting"|
                                 "equipment_issue"|"other","flight_id":null,"gate":null,"text":"","role":""}
->   {"ok":true,"report":{...}}
GET|POST /api/live/{run_id}/flights/{flight_id}/notes {"text","role"} -> [{"t","role","text"}]
```

A report is a `floor_report` event in shared state, read through the agents' contracts:
- **Station & Staff:** stops using a gate reported unavailable. It treats a long queue as a staffing gap at that place (30 min,
  short by one: a gate agent at a gate, else customer service).
- **Situation:** lists every report as evidence.
- **The other kinds** are notes only.

Flight notes are notes only; no agent reads them.

## Flight and Customer views

These need the session to be running in this server (`409` otherwise). Customer routes are for the frontline role only
(`?role=counter_gate_agent`, the default; `403` for other roles). They return names and booking references only, never a
contact detail.

```
GET /api/live/{run_id}/flights/{flight_id}
 -> {"id","from","to","sched_dep","est_dep","sched_arr","est_arr","status","gate","crew":{"id","legal_until"},
     "customers":0,"connections":0,"script":{"text","updated"},"notes":[...],
     "on_screens":{"app":"","sms":"","gate_screen":"","call_center":""}}
GET /api/live/{run_id}/customers?q=<name or booking ref>  -> [{"id","name","booking_ref","flight_id","status"}]
GET /api/live/{run_id}/customers/{id}
 -> {"id","name","trip":[{"flight_id","from","to","dep","arr","status"}],"true_status","screens":[...],
     "needs":["wheelchair"|"unaccompanied_minor"],
     "rebooking":[{"option_id","flights":[...],"arrives","seats_left","why","recommended"}],"hotel":null|{...},"refund_due":false}
POST /api/live/{run_id}/customers/{id}/rebook {"option_id"}  and  POST .../refund {}   -> {"ok":true,"trip":[...]}
```

The Customer view also has:
- **`messages`**: `[{"t","channel","text"}]`, the exact words of every message delivered to them.
- **`refunds`**: `[{"amount","status":"issued"|"due","t"}]`. A refund is `issued` once Passenger Care's refund decision was
  carried out (with its time), and `due` when one is owed but not issued yet. `amount` is `null` because the ticket price is
  not on record.
- **`hotel`**: `{"name","price","currency","shuttle","status"}`, or `null`. It is the room Passenger Care planned or assigned:
  the price is its option's cost, and the status is "assigned" once the hotel confirmed it, otherwise Passenger Care's own
  status.

The flight view also has:
- **`truth`**: the flight's real status now, in one plain sentence, for example "WN1033 to Baltimore is waiting for the ground
  stop to lift, now expected to leave at 15:14 (129 min late)."
- **`one_story`**: `[{"place","says","matches","since"}]` for `app`, `sms`, `gate_screen`, `call_center`, `counter`,
  `gate_agents` and `crew`.
  - A screen: the exact text most of the flight's tracked Customers see on it. `matches` is whether those screens agree with
    the truth; `since` is when that text arrived.
  - `gate_agents`: the latest staff note sent about the flight.
  - `crew`: the latest crew message delivered about the flight. For both, `matches` is false if the flight changed after it
    was sent.
  - `counter`: nothing is ever sent to the counter, so it is always `null`.
  - Every field is `null` for a place nothing was sent to.

## Who sees out-of-date information

```
GET /api/live/{run_id}/out_of_date
 -> {"total":120,"ten_min_ago":95,"flights":[{"flight_id","label","customers","channels":[{"channel","since"}],"truth"}]}
```

- `total` is the same count as `state.truth.wrong_now`: Customers with at least one screen that disagrees with the truth.
- `flights` groups them by the flight they are on (each Customer is counted once), sorted by Customers. Each lists its wrong
  screens with the minute they went wrong, and the flight's `truth` sentence.
- `ten_min_ago` is `wrong_now` from the minute row ten minutes earlier.

## A Customer's legs, rebooking on request, a flight's Customers

The Customer view's `trip` legs also have:
- `sched_dep`, `sched_arr`, `est_dep`, `est_arr`;
- `gate` (for a leg at MCI, from the station plan);
- `status`;
- `connection`: `{"to_flight","minutes","at_risk"}`, or `null` on the last leg. `at_risk` means shorter than the rules'
  minimum connection time.

The view also has `international`: true if the Customer is in the international group or a station on the trip is in an
international region, false if every station has a recorded region, `null` if one is not recorded.

```
GET /api/live/{run_id}/customers/{id}/rebooking?reason=customer_request
 -> {"customer_id","reason":"customer_request","options":[{"option_id":"c1","rank":1,"label":"Best option","recommended":true,
     "flights":[{"flight_id","dep","arr","from","to"}],"arrives":"19:45","seats_left":3,"why":"Arrives in Dallas at 19:45 today, nonstop, 3 seats left"}]}
```

- The options cover any flight out of MCI not yet flown, even if it is on time.
- They are every other flight to the final destination with seats left: today's, and tomorrow's already on the schedule.
  A tomorrow option's `why` says whether a hotel night is needed or the Customer can go home.
- Ranked by the earliest arrival, then fewer connections, then the same day. Labels: "Best option", "Next best",
  "Third choice", then "Option 4" and so on.
- `POST /customers/{id}/rebook {"option_id":"c1"}` accepts these ids as well as the disruption options (`r1`...).

A counter rebooking changes the Customer's trip record. Communication then sends them the new trip on its own, through the
normal shared-state path, in exact words: "Hi Amelia. Your trip has changed. You are now booked on WN1048 to St. Louis...".
That message appears in `/sent` (with `customer_id`) and in the Customer's `messages`, and their screens update.

```
GET  /api/live/{run_id}/flights/{id}/customers
 -> {"flight":{"id","from","to","sched_dep","est_dep","status","gate","truth"},
     "customers":[{"id","name","status","needs","international","connection","best_option":{...as above...}|null,"rebooked":false}]}
POST /api/live/{run_id}/flights/{id}/rebook {"assignments":[{"customer_id","option_id"}]}
 -> {"flight_id","results":[{"customer_id","ok":true,"error":null,"trip":[...]}]}
```

- The list covers the flight's Customers now, plus those already moved off it (`rebooked: true`).
- Each assignment goes through the same path and rule check as a single rebooking, and is its own result: one failure doesn't
  stop the others.
- These routes are for the frontline role (`counter_gate_agent`).

## GET /api/live/{run_id}/sent?since=<minute>

```
[{"t":"11:31","minute":691,"to":"Nora Okafor" | "143 Customers on WN1033" | "Gate agents and call center" | "Crew R07",
  "channel":"sms"|"app"|"staff_note"|"crew"|null,"kind":"message"|"refund"|"hotel"|"rebook","text":"exact words","count":1,
  "customer_id":"PAX0073"|null}]
```

Newest last. It covers delivered messages, staff notes and crew messages, plus refund, hotel and rebooking confirmations.
- **A mass send** (one recommendation, one channel, one minute) is one row with its `count`. Its `text` is the first message's
  exact words, since personalised messages differ by name.
- **Confirmations of one kind in the same minute** are one row with their count. They have no message words or channel on
  record, so their `text` is a plain confirmation and `channel` is `null`.
- **There is no email row:** nothing in the simulation sends email.
- **`customer_id`** is set on every row that went to exactly one Customer, and `null` for grouped rows, staff notes and crew
  messages.

- **`script`**: the newest message the co-pilot actually sent about the flight (a staff note first).
- **`on_screens`**: what most of the flight's tracked Customers see on each screen.
- **`rebooking`**: offered when the Customer's trip was cancelled or moved to tomorrow, or their flight out of MCI is at least
  60 min late. The options are other flights today to their destination with seats left in the reservation system. Passenger
  Care's own advice is recommended; otherwise the earliest arrival.
- **`rebook`**: goes through Passenger Care's rebooking path (decision K01), the rule check and the reservation system. The
  change reaches every screen through the normal truth feed.
- **`refund`**: approves Passenger Care's own refund offer for the Customer through its path, and is rule-checked again.

## Station, clocks, handover

```
GET /api/live/{run_id}/station
 -> {"ramp":{"open","reason","since"},"gates":[{"gate","status":"open"|"closed"|"in_use","flight_id","staffed","note"}],
     "staffing_gaps":[{"role","from","to","short_by"}]}
GET /api/live/{run_id}/clocks
 -> {"tarmac":[{"flight_id","minutes","limit":180}],"crew":[{"crew_id","flight_id","legal_until","minutes_left"}],
     "refunds":[{"customer_id","flight_id","due_by","status":"auto_issued"|"due"}],
     "special_assistance":[{"customer_id","need","flight_id","waiting_min"}]}
GET /api/live/{run_id}/handover?since=HH:MM
 -> {"since","decisions":0,"open":[...],"watch":["plain sentences"],"auto_done":0,"text":"plain summary"}
```

`/api/day/{run_id}` also works for a live run still in progress; it shows the events saved so far, written every 5 simulated
minutes.

## GET /api/live/sessions

```
[{"run_id":"","started_at":"","status":"","roles":[],"minute":0}]
```

## GET /api/live/{run_id}/results

Available once the session has `finished`. It compares this session with today and with the stored orchestrated run (if one
exists). Each block has the same fields:

```
{"you":{"wrong_screen_passenger_minutes":0,"critical_passenger_minutes":0,"lost_updates_still_wrong":0,"cancelled":0,
        "stranded_overnight":0,"focus_final_trip":""},
 "today":{...},"orchestrated":{...}|null,
 "decisions":{"approved":0,"rejected":0,"waited":0,"handed_off":0,"escalated":0,"median_answer_min":0}}
```

## Saved tables

| Table | One row per |
|---|---|
| `live_sessions` | session: seed, scenario, roles, status, sim_minute, speeds, start/end, results |
| `live_questions` | question: target, asked/deadline minutes, status, group, depends_on, priority, why, answer |
| `live_decision_traces` | trace snapshot, at ask and at answer |
| `live_minutes` | minute: wrong-screen numbers (live and today) and the focus passenger's screens |
