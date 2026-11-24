-- MCI Ground-Stop Co-Pilot: the shared live view (PostgreSQL)
--
-- Southwest's own systems stay the official record. This database is a time-stamped
-- copy the co-pilot uses to compare channels, catch disagreements and send one message.
--
-- Groups
--   What exists         : stations, aircraft, crews, flights, passenger_groups, passengers,
--                         bookings, bags, hotels, staff, later_capacity, route_tiers,
--                         transport_options, hotel_rate_tiers
--   What each screen shows: channel_views
--   What's happening    : events (append-only), incidents, incident_events, system_health
--   What's being decided: recommendations, decisions, actions, audit_log (append-only)
--   Checking & measuring: mismatches, metric_runs, data_issues
--
-- Conventions
--   * Every fact row has source (where it came from), system_of_record (which official
--     system owns it) and as_of (when it was true).
--   * events and audit_log are append-only.
--   * All passenger data is synthetic (generated for the class), never real people.

-- ============================================================ WHAT EXISTS

CREATE TABLE stations (
    code               char(3) PRIMARY KEY,
    name               text NOT NULL,
    region             text,
    is_base            boolean NOT NULL DEFAULT false,   -- big Southwest base (DEN, DAL, MDW, BWI)
    is_diversion_option boolean NOT NULL DEFAULT false,
    gates              int,
    wheelchair_staff   int,
    has_customs        boolean,
    terminal_capacity  int,
    people_in_terminal int,
    ramp_open          boolean NOT NULL DEFAULT true,
    source             text NOT NULL,
    system_of_record   text NOT NULL,
    as_of              timestamptz NOT NULL
);

CREATE TABLE aircraft (
    id                 text PRIMARY KEY,                 -- A31
    type               text NOT NULL,
    seats              int NOT NULL,
    current_station    char(3) REFERENCES stations(code),
    airborne           boolean NOT NULL DEFAULT false,
    fuel_pct           int,
    fuel_minutes_extra int,                              -- set when airborne
    maint_status       text NOT NULL,                    -- Routine | Inspection due | Maintenance critical ...
    maint_due_at       timestamptz,
    needs_inspection   boolean NOT NULL DEFAULT false,
    next_legs          text[],                           -- rotation after MCI
    recovery_priority  text,
    source             text NOT NULL,
    system_of_record   text NOT NULL,
    as_of              timestamptz NOT NULL
);

CREATE TABLE crews (
    id                  text PRIMARY KEY,                -- C31, R01, CX31
    kind                text NOT NULL,                   -- full_crew | pilot_pair | fa_team
    pilots              int NOT NULL,
    flight_attendants   int NOT NULL,
    base                char(3),
    current_station     char(3) REFERENCES stations(code),
    is_reserve          boolean NOT NULL DEFAULT false,
    reserve_status      text,                            -- Available | On Call | Assigned after trigger
    available_from      timestamptz,
    pilot_duty_left_min int,                             -- duty clock
    fa_duty_left_min    int,
    min_rest_hours      numeric(4,1),
    legality_risk       text,                            -- LOW | MEDIUM | HIGH
    hotel_if_cancelled  boolean,
    deadhead_planned    boolean,
    restrictions        text[],
    fatigue_reported    boolean NOT NULL DEFAULT false,
    availability        text NOT NULL DEFAULT 'on_duty'
                        CHECK (availability IN ('on_duty', 'reserve', 'sick', 'fatigued', 'on_leave')),  -- agents only read this
    source              text NOT NULL,
    system_of_record    text NOT NULL,
    as_of               timestamptz NOT NULL
);

CREATE TABLE flights (
    id                text PRIMARY KEY,                  -- WN1031 (departure) / WN2031 (inbound)
    direction         text NOT NULL CHECK (direction IN ('departure', 'arrival')),
    schedule_no       int,                               -- row number in the class schedule
    origin            char(3) NOT NULL REFERENCES stations(code),
    destination       char(3) NOT NULL REFERENCES stations(code),
    sched_dep         timestamptz NOT NULL,
    sched_arr         timestamptz NOT NULL,
    est_dep           timestamptz,
    est_arr           timestamptz,
    edct              timestamptz,                       -- FAA-assigned takeoff time
    aircraft_id       text REFERENCES aircraft(id),
    crew_id           text REFERENCES crews(id),
    reserve_crew_ids  text[],                            -- reserve crews that took over from crew_id (pilots and/or flight attendants)
    next_destination  char(3),                           -- where the plane goes after this leg
    priority          text CHECK (priority IN ('P1','P2','P3','P4')),
    status            text NOT NULL DEFAULT 'scheduled', -- scheduled | boarding | held | delayed | ground_stop | departed | airborne | diverted | arrived | cancelled
    diverted_to       char(3) REFERENCES stations(code),
    tarmac_since      timestamptz,
    cancel_reason     text,
    controllable      boolean,                           -- airline's fault? drives compensation
    pax_total         int NOT NULL,
    source            text NOT NULL,
    system_of_record  text NOT NULL,
    as_of             timestamptz NOT NULL
);

-- Every passenger on every flight, as groups (segments are exclusive and add up to pax_total).
CREATE TABLE passenger_groups (
    flight_id        text NOT NULL REFERENCES flights(id),
    segment          text NOT NULL CHECK (segment IN ('child_alone','wheelchair','international','connecting','standard')),
    count            int NOT NULL CHECK (count >= 0),
    rebooked         int NOT NULL DEFAULT 0,
    refunded         int NOT NULL DEFAULT 0,
    need_hotel       int NOT NULL DEFAULT 0,
    notified_at      timestamptz,
    source           text NOT NULL,
    system_of_record text NOT NULL,
    as_of            timestamptz NOT NULL,
    PRIMARY KEY (flight_id, segment)
);

-- A sample of individual (synthetic) passengers we can follow one by one.
CREATE TABLE passengers (
    id                text PRIMARY KEY,                  -- PAX0001
    full_name         text NOT NULL,
    booking_ref       text NOT NULL UNIQUE,
    segment           text NOT NULL,                     -- same segment names as passenger_groups
    final_destination char(3) NOT NULL REFERENCES stations(code),
    lives_locally     boolean NOT NULL DEFAULT false,    -- Kansas City area: could go home instead of a hotel
    overnight         boolean NOT NULL DEFAULT false,    -- stranded: no seat left today, rebooked on a next-day flight
    loyalty_tier      text,                              -- none | A-List | A-List Preferred
    contact_channels  text[] NOT NULL,                   -- app, sms, email
    is_synthetic      boolean NOT NULL DEFAULT true,
    source            text NOT NULL,
    system_of_record  text NOT NULL,
    as_of             timestamptz NOT NULL
);

-- The true itinerary: which flights each passenger is booked on, in order.
CREATE TABLE bookings (
    passenger_id      text NOT NULL REFERENCES passengers(id),
    seq               int NOT NULL,
    flight_id         text NOT NULL REFERENCES flights(id),
    status            text NOT NULL DEFAULT 'confirmed', -- confirmed | rebooked | cancelled | refunded
    source            text NOT NULL,
    system_of_record  text NOT NULL,
    as_of             timestamptz NOT NULL,
    PRIMARY KEY (passenger_id, seq)
);

CREATE TABLE bags (
    tag               text PRIMARY KEY,
    passenger_id      text NOT NULL REFERENCES passengers(id),
    routing           text[] NOT NULL,                   -- flight ids the bag is tagged to
    current_location  char(3) REFERENCES stations(code),
    status            text NOT NULL DEFAULT 'checked',   -- checked | loaded | in_transit | held | misrouted | delivered
    source            text NOT NULL,
    system_of_record  text NOT NULL,
    as_of             timestamptz NOT NULL
);

CREATE TABLE hotels (
    id                text PRIMARY KEY,
    name              text NOT NULL,
    station           char(3) NOT NULL REFERENCES stations(code),
    rooms_total       int NOT NULL,
    rooms_sold        int NOT NULL,
    rooms_available   int NOT NULL,
    rooms_held        int NOT NULL DEFAULT 0,
    rate_usd          numeric(8,2),
    distance_miles    numeric(5,1),
    shuttle           text,
    bus_capacity      int,
    last_checkin      text,
    use_preference    text,
    source            text NOT NULL,
    system_of_record  text NOT NULL,
    as_of             timestamptz NOT NULL
);

CREATE TABLE staff (
    id                text PRIMARY KEY,
    role              text NOT NULL,                     -- gate_agent | ramp | wheelchair | customer_service | station_manager
    station           char(3) NOT NULL REFERENCES stations(code),
    shift_start       timestamptz NOT NULL,
    shift_end         timestamptz NOT NULL,
    last_break        timestamptz,
    on_duty           boolean NOT NULL DEFAULT true,
    is_synthetic      boolean NOT NULL DEFAULT true,
    source            text NOT NULL,
    system_of_record  text NOT NULL,
    as_of             timestamptz NOT NULL
);

CREATE TABLE later_capacity (
    destination       char(3) PRIMARY KEY REFERENCES stations(code),
    scheduled_flights int,
    later_flights     int,
    open_seats_later  int,
    next_later_departure text,
    connection_alternative text,
    recovery_capacity text,
    source            text NOT NULL,
    system_of_record  text NOT NULL,
    as_of             timestamptz NOT NULL
);

CREATE TABLE route_tiers (
    destination       char(3) PRIMARY KEY,
    region            text,
    tier              int,                               -- 1 high frequency, 2 one-two daily, 3 seasonal/international
    source            text NOT NULL
);

CREATE TABLE transport_options (
    name              text PRIMARY KEY,
    capacity          int,
    hourly_rate_usd   numeric(8,2),
    per_trip_rate_usd numeric(8,2),
    source            text NOT NULL
);

CREATE TABLE hotel_rate_tiers (
    tier              text PRIMARY KEY,
    radius_miles      int,
    hotel_count       int,
    rate_usd          numeric(8,2),
    for_travelers     text,
    source            text NOT NULL
);

-- ============================================================ WHAT EACH SCREEN SHOWS

-- What each channel currently shows a passenger. The consistency check compares these
-- with the truth (bookings, flights, bags) to find mismatches.
CREATE TABLE channel_views (
    passenger_id      text NOT NULL REFERENCES passengers(id),
    channel           text NOT NULL CHECK (channel IN ('app','sms','gate_screen','call_center')),
    shown_itinerary   text[] NOT NULL,                   -- flight ids as this channel shows them
    shown_status      text NOT NULL,
    shown_destination char(3),
    shown_bag_routing text[],
    message           text,                              -- last message shown on this channel
    updated_at        timestamptz NOT NULL,
    PRIMARY KEY (passenger_id, channel)
);

-- ============================================================ WHAT'S HAPPENING

CREATE TABLE events (
    id           bigserial PRIMARY KEY,
    occurred_at  timestamptz NOT NULL,
    type         text NOT NULL,
    actor        text NOT NULL,                          -- simulator | adapter | agent | human | system
    actor_name   text NOT NULL,
    station      char(3) REFERENCES stations(code),
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    seed         bigint,
    run_id       text,
    published_at timestamptz,                            -- when the source delivered it, if late; NULL = as it happened
    recorded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_time_idx ON events (occurred_at);
CREATE INDEX events_type_idx ON events (type);

CREATE TABLE incidents (
    id                bigserial PRIMARY KEY,
    kind              text NOT NULL,                     -- weather | faa_program | ground_stop | it_outage | airport | security | crew | other
    station           char(3) REFERENCES stations(code),
    title             text NOT NULL,
    likely_cause      text,
    confidence        numeric(3,2),
    controllable      boolean,
    status            text NOT NULL DEFAULT 'suspected',  -- suspected | open | monitoring | resolved
    expected_end_min  timestamptz,
    expected_end_max  timestamptz,
    opened_at         timestamptz NOT NULL,
    resolved_at       timestamptz,
    opened_by         text NOT NULL,
    run_id            text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE incident_events (
    incident_id  bigint REFERENCES incidents(id),
    event_id     bigint REFERENCES events(id),
    PRIMARY KEY (incident_id, event_id)
);

CREATE TABLE system_health (
    system          text PRIMARY KEY,
    status          text NOT NULL DEFAULT 'up',          -- up | degraded | down
    last_heartbeat  timestamptz,
    data_age_sec    int,
    note            text
);

-- ============================================================ WHAT'S BEING DECIDED

CREATE TABLE recommendations (
    id               bigserial PRIMARY KEY,
    incident_id      bigint REFERENCES incidents(id),
    agent            text NOT NULL,
    title            text NOT NULL,
    why              text NOT NULL,
    options          jsonb NOT NULL,
    autonomy         text NOT NULL,                      -- auto | needs_approval | human_only
    approver_role    text,
    deadline         timestamptz,
    rank             int,
    safety_rung      int,
    decision_ids     text[],                             -- which decisions in config/decisions.yaml it answers
    supersedes       bigint REFERENCES recommendations(id),  -- the earlier proposal for the same decision that this one replaces
    rule_check_ok    boolean,                            -- the orchestrator's rule check (backend/rules/check.py); NULL = not checked yet
    rule_check_notes text[],
    rule_ids         text[],                             -- the rules that blocked it (ids in config/rules/)
    data_as_of       timestamptz,
    status           text NOT NULL DEFAULT 'pending',    -- pending | approved | rejected | expired | done_automatically | blocked_by_rule_check
    run_id           text,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE decisions (
    id                 bigserial PRIMARY KEY,
    recommendation_id  bigint NOT NULL REFERENCES recommendations(id),
    decided_by         text NOT NULL,
    role               text NOT NULL,
    verdict            text NOT NULL,                    -- approved | rejected | changed
    chosen_option      text,
    reason             text,
    decided_at         timestamptz NOT NULL
);

CREATE TABLE actions (
    id             bigserial PRIMARY KEY,
    decision_id    bigint REFERENCES decisions(id),
    target_system  text NOT NULL,
    request        jsonb NOT NULL,
    result         text,
    executed_at    timestamptz NOT NULL
);

CREATE TABLE audit_log (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL,
    actor        text NOT NULL,
    actor_name   text NOT NULL,
    what         text NOT NULL,
    entity_type  text NOT NULL,
    entity_id    text NOT NULL,
    before       jsonb,
    after        jsonb,
    run_id       text
);
CREATE INDEX audit_entity_idx ON audit_log (entity_type, entity_id);

-- events and audit_log are append-only: the database refuses any UPDATE or DELETE on them.
CREATE FUNCTION refuse_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: rows are never updated or deleted', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION refuse_change();

-- ============================================================ SHARED STATE BETWEEN AGENTS

-- What the Situation agent concluded, one row per version. Append-only: a new version is a new row.
-- The next agents (network_recovery, station_staff, passenger_care, communication) read this table.
CREATE TABLE situation_assessments (
    id                     bigserial PRIMARY KEY,
    agent_run_id           text NOT NULL,                -- one replay of the agent (never mixes with another)
    run_id                 text,                         -- the simulator run it read from
    version                int NOT NULL,
    assessed_at            timestamptz NOT NULL,         -- simulated time T the assessment is "as of"
    status                 text NOT NULL,                -- normal | abnormal | cannot_assess: <source> stale since HH:MM
    type                   text,                         -- weather | faa_program | it_outage | airport | security | crew | other | unknown
    likely_cause           text,
    confidence             numeric(3,2),
    incident_controllable  boolean,
    flight_controllability jsonb NOT NULL DEFAULT '[]'::jsonb,
    expected_end_earliest  timestamptz,
    expected_end_likely    timestamptz,
    expected_end_latest    timestamptz,
    end_confidence         numeric(3,2),
    impact                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    conflicts              jsonb NOT NULL DEFAULT '[]'::jsonb,
    stale_sources          jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence               jsonb NOT NULL DEFAULT '[]'::jsonb,
    summary                text NOT NULL,
    data_as_of             timestamptz,
    decision_ids           text[] NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_run_id, version)
);
CREATE TRIGGER situation_assessments_append_only BEFORE UPDATE OR DELETE ON situation_assessments
    FOR EACH ROW EXECUTE FUNCTION refuse_change();

-- The Network Recovery agent's plan (shared state, Phase 4d). Append-only and versioned, like situation_assessments. ADVISORY:
-- nothing in it is applied; it is what the agent proposes, for people (and later Passenger Care) to read.
CREATE TABLE recovery_plans (
    id                     bigserial PRIMARY KEY,
    agent_run_id           text NOT NULL,
    run_id                 text,
    version                int NOT NULL,
    planned_at             timestamptz NOT NULL,         -- simulated time T the plan is "as of"
    situation_version      int,                          -- the Situation version it was built on
    delays                 jsonb NOT NULL DEFAULT '[]'::jsonb,
    cancels                jsonb NOT NULL DEFAULT '[]'::jsonb,
    crew_actions           jsonb NOT NULL DEFAULT '[]'::jsonb,
    approval_status        jsonb NOT NULL DEFAULT '[]'::jsonb,
    station_plan_version   int,                                  -- Phase 4f: the Station and Staff plan version it used
    hold_divert            jsonb NOT NULL DEFAULT '[]'::jsonb,
    aircraft_recovery      jsonb NOT NULL DEFAULT '[]'::jsonb,
    restart_order          jsonb NOT NULL DEFAULT '[]'::jsonb,
    seats_by_destination   jsonb NOT NULL DEFAULT '{}'::jsonb,
    warnings               jsonb NOT NULL DEFAULT '[]'::jsonb,
    estimates              jsonb NOT NULL DEFAULT '{}'::jsonb,   -- labelled estimates (the plan is not applied)
    sources                jsonb NOT NULL DEFAULT '[]'::jsonb,
    data_as_of             timestamptz,
    decision_ids           text[] NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_run_id, version)
);
CREATE TRIGGER recovery_plans_append_only BEFORE UPDATE OR DELETE ON recovery_plans
    FOR EACH ROW EXECUTE FUNCTION refuse_change();

-- The Passenger Care agent's plan (shared state, Phase 4e): who needs care and what (hotel, transport, compensation status,
-- vulnerable-passenger plans, rebooking advice), with approval status and cost. Append-only and versioned.
CREATE TABLE care_plans (
    id                     bigserial PRIMARY KEY,
    agent_run_id           text NOT NULL,
    run_id                 text,
    version                int NOT NULL,
    planned_at             timestamptz NOT NULL,
    situation_version      int,
    recovery_plan_version  int,
    passengers             jsonb NOT NULL DEFAULT '[]'::jsonb,
    hotels                 jsonb NOT NULL DEFAULT '[]'::jsonb,
    costs                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    vulnerable             jsonb NOT NULL DEFAULT '[]'::jsonb,
    wheelchair_requests    jsonb NOT NULL DEFAULT '[]'::jsonb,   -- for Station & Staff (next agent)
    rebooking_advice       jsonb NOT NULL DEFAULT '[]'::jsonb,   -- advisory: bookings are not changed
    sources                jsonb NOT NULL DEFAULT '[]'::jsonb,
    data_as_of             timestamptz,
    decision_ids           text[] NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_run_id, version)
);
CREATE TRIGGER care_plans_append_only BEFORE UPDATE OR DELETE ON care_plans
    FOR EACH ROW EXECUTE FUNCTION refuse_change();

-- Phase 4f: the Station & Staff agent's plan (shared state, append-only, versioned; only station_staff writes it).
CREATE TABLE station_plans (
    id                     bigserial PRIMARY KEY,
    agent_run_id           text NOT NULL,
    run_id                 text,
    version                int NOT NULL,
    planned_at             timestamptz NOT NULL,
    situation_version      int,
    recovery_plan_version  int,
    care_plan_version      int,
    ramp_status            jsonb NOT NULL DEFAULT '{}'::jsonb,
    terminal               jsonb NOT NULL DEFAULT '{}'::jsonb,
    gates                  jsonb NOT NULL DEFAULT '[]'::jsonb,
    gate_shortages         jsonb NOT NULL DEFAULT '[]'::jsonb,
    staff_shortages        jsonb NOT NULL DEFAULT '[]'::jsonb,
    staff_coverage         jsonb NOT NULL DEFAULT '[]'::jsonb,
    tarmac                 jsonb NOT NULL DEFAULT '[]'::jsonb,
    wheelchair_coverage    jsonb NOT NULL DEFAULT '[]'::jsonb,
    child_coverage         jsonb NOT NULL DEFAULT '[]'::jsonb,
    warnings               jsonb NOT NULL DEFAULT '[]'::jsonb,
    breaks                 jsonb NOT NULL DEFAULT '[]'::jsonb,   -- planned breaks per staff member (never later than the rule)
    sources                jsonb NOT NULL DEFAULT '[]'::jsonb,
    data_as_of             timestamptz,
    decision_ids           text[] NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_run_id, version)
);
CREATE TRIGGER station_plans_append_only BEFORE UPDATE OR DELETE ON station_plans
    FOR EACH ROW EXECUTE FUNCTION refuse_change();

-- Phase 5a: the orchestrator's log, one row a minute (append-only): which agents ran, the shared versions each read and wrote,
-- proposals, blocks, conflicts, the queue size and the open incidents. Plus what each incident's work is (tags).
CREATE TABLE orchestrator_log (
    id      bigserial PRIMARY KEY,
    run_id  text NOT NULL,
    at      timestamptz NOT NULL,
    row     jsonb NOT NULL
);
CREATE TRIGGER orchestrator_log_append_only BEFORE UPDATE OR DELETE ON orchestrator_log
    FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TABLE incident_items (
    id           bigserial PRIMARY KEY,
    run_id       text NOT NULL,
    incident_id  bigint REFERENCES incidents(id),
    kind         text NOT NULL,                 -- recommendation | queue_item | situation_assessments | recovery_plans | ... | message
    ref          text NOT NULL,
    at           timestamptz NOT NULL
);

-- CO-PILOT ONLY (not truth tables, no agent writes them): gates and staff the co-pilot actually assigned, applied by the executor
-- after the rule check (and, where the rules ask for it, the station manager's approval). Flights, bookings and crews are never
-- changed; the staff roster itself is never edited.
CREATE TABLE gate_assignments (
    id             bigserial PRIMARY KEY,
    run_id         text NOT NULL,
    at             timestamptz NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('assign', 'release')),
    flight_id      text NOT NULL,
    gate           text NOT NULL,
    remote_stand   boolean NOT NULL DEFAULT false,
    from_at        timestamptz,
    to_at          timestamptz,
    recommendation int,
    approved_by    text,
    waited_min     numeric(6,1)
);
CREATE TABLE staff_assignments (
    id             bigserial PRIMARY KEY,
    run_id         text NOT NULL,
    at             timestamptz NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('assign', 'release', 'call_in', 'pause')),
    staff_id       text NOT NULL,
    role           text NOT NULL,
    task           text NOT NULL,
    flight_id      text,
    passenger_id   text,
    from_at        timestamptz,
    to_at          timestamptz,
    recommendation int,
    approved_by    text,
    waited_min     numeric(6,1)
);

-- CO-PILOT ONLY (not a truth table, no agent writes it): hotel rooms the co-pilot actually held and assigned, applied by the
-- executor after the rule check (and, for an assignment, a person's approval). Flights and bookings are never changed.
CREATE TABLE hotel_assignments (
    id            bigserial PRIMARY KEY,
    run_id        text NOT NULL,
    at            timestamptz NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('hold', 'assign', 'release')),
    hotel_id      text NOT NULL,
    rooms         int NOT NULL,
    passenger_id  text,
    accessible    boolean NOT NULL DEFAULT false,
    recommendation int,
    approved_by   text,
    waited_min    numeric(6,1)
);

-- ============================================================ CHECKING & MEASURING

-- Written by the communication agent only (its own rows; it may update their status, never another agent's or run's).
CREATE TABLE mismatches (
    id             bigserial PRIMARY KEY,
    passenger_id   text NOT NULL REFERENCES passengers(id),
    channel        text NOT NULL,
    field          text NOT NULL,                        -- itinerary | status | destination | bag_routing
    shown_value    text,
    true_value     text,
    cause          text,                                 -- what the agent suspects: waiting_for_update | stuck | failed_delivery | bag_system | seat_hold | unknown
    cause_confidence numeric(3,2),
    severity       text,                                 -- critical | high | medium | low
    flight_id      text,                                 -- the flight it is about (for the roll-up by flight)
    status         text NOT NULL DEFAULT 'open',         -- open | cleared
    since          timestamptz,                          -- when it started being wrong (the truth changed and the channel didn't follow)
    detected_at    timestamptz NOT NULL,
    cleared_at     timestamptz,                          -- when the channel matched the truth again
    evidence       jsonb,                                -- event ids and times behind it
    sources        jsonb,                                -- each source with its as_of time (G01)
    decision_ids   text[],                               -- G08
    fixed_at       timestamptz,
    fix_applied    text,
    reached_passenger boolean NOT NULL DEFAULT false,
    agent_run_id   text,                                 -- the agent's replay (filled in by AgentContext)
    run_id         text                                  -- the run it read (filled in by AgentContext; the agent never sees it)
);

CREATE TABLE metric_runs (
    id             bigserial PRIMARY KEY,
    run_id         text NOT NULL,
    seed           bigint NOT NULL,
    mode           text NOT NULL CHECK (mode IN ('today','copilot')),
    scenario       text NOT NULL,
    metrics        jsonb NOT NULL,
    finished_at    timestamptz NOT NULL DEFAULT now()
);

-- Conflicts found in the class files. Flagged, never silently fixed.
CREATE TABLE data_issues (
    id          bigserial PRIMARY KEY,
    category    text NOT NULL,
    item        text NOT NULL,
    issue       text NOT NULL,
    resolution  text NOT NULL,                           -- what the loader did about it
    severity    text NOT NULL DEFAULT 'info'             -- info | warning
);
