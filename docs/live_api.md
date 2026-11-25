# Live human-in-the-loop API

The world (weather, flights, the storm, passengers, the four screens) is simulated. The agents run on it minute by minute. You
are the human in the loop: every decision that needs a person comes to you, and you approve, reject, wait or hand off.
Everything is saved.

- Code: `backend/api/live.py` (routes), `simulator/live.py` (session and clock), `backend/execution/live_human.py` (you, in front
  of the simulated people), `backend/execution/live_trace.py` (why and trace). Settings: `config/live.yaml`.
- Times are `"HH:MM"` local (America/Chicago). `minute` is minutes after midnight.
- Only three routes write: `start`, `control` and `decide`. All other routes only read.
- Errors:
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
- `last_option: true`: you said no once and the agent has no other plan. A second no applies `consequence_if_rejected` through
  today's process.
- `why.options_considered`: only options the agent itself produced. It is often empty.

## GET /api/live/{run_id}/questions/{id}/trace

The latest saved snapshot of the trace (taken when asked, and again when answered). It is a graph from the outside world to the
question.

```
{"question_id":12,"snapshot":"asked"|"answered",
 "nodes":[{"id":"n1","kind":"feed"|"situation"|"network_recovery"|"passenger_care"|"station_staff"|"communication"|"orchestrator"|"rule_check"|"question"|"human",
           "label":"","t":"11:30","detail":{}}],
 "edges":[{"from":"n1","to":"n2","label":"used","t":"11:30"}]}
```

Edge labels:
- `used`: a feed or version the next agent used;
- `sent to`: agent → rule check → orchestrator → question;
- `answered`: question → human.

## POST /api/live/{run_id}/decide

```
body {"question_ids":[12],"action":"approve"|"reject"|"wait"|"hand_off","reason":"","minutes":10}
->   {"ok":true,"results":[{"id":12,"status":"approved","error":null}]}
```

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
- Communication writes no shared table, so its `versions` list is empty.
- `queue` counts the questions waiting for you.

## GET /api/live/{run_id}/history

```
{"decisions":[{"id":12,"title":"","role":"","action":"","answer":"approved","reason":"","asked":"","answered":"","waited_min":4}]}
```

`answer` is one of `approved`, `rejected`, `waited`, `handed_off` or `escalated` (answered by the duty manager after the
deadline). `waited_min` is the minutes from asked to answered; for a question you only put off, it is the minutes you waited.

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
