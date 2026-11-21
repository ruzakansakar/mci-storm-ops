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

-- ============================================================ CHECKING & MEASURING

CREATE TABLE mismatches (
    id             bigserial PRIMARY KEY,
    passenger_id   text NOT NULL REFERENCES passengers(id),
    channel        text NOT NULL,
    field          text NOT NULL,                        -- itinerary | status | destination | bag_routing
    shown_value    text,
    true_value     text,
    cause          text,                                 -- stale_copy | sync_delay | seat_hold | message_failed | bag_system
    detected_at    timestamptz NOT NULL,
    fixed_at       timestamptz,
    fix_applied    text,
    reached_passenger boolean NOT NULL DEFAULT false,
    run_id         text
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
