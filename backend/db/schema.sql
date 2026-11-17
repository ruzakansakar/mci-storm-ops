-- MCI ground-stop co-pilot: shared live picture (PostgreSQL)
-- One source of truth that every agent and every dashboard view reads.
--
-- Three groups:
--   1. What exists        : stations, aircraft, crews, flights, passenger_groups, hotels
--   2. What's happening   : events (append-only), incidents, incident_events, system_health
--   3. What's being decided: recommendations, decisions, actions, audit_log (append-only)
--
-- Conventions
--   * Every state row has updated_at and source (which system or agent last wrote it),
--     so anyone can see how fresh a fact is.
--   * events and audit_log are append-only: never UPDATE or DELETE them.
--     The state tables can be rebuilt from events (replay any moment).
--   * Times are stored as timestamptz in simulated time.

CREATE TYPE priority_level   AS ENUM ('P1', 'P2', 'P3', 'P4');
CREATE TYPE flight_status    AS ENUM ('scheduled', 'boarding', 'held', 'delayed', 'ground_stop', 'departed', 'diverted', 'arrived', 'cancelled');
CREATE TYPE incident_kind    AS ENUM ('weather', 'faa_program', 'ground_stop', 'it_outage', 'airport', 'security', 'crew', 'other');
CREATE TYPE incident_status  AS ENUM ('suspected', 'open', 'monitoring', 'resolved');
CREATE TYPE autonomy_level   AS ENUM ('auto', 'needs_approval', 'human_only');
CREATE TYPE rec_status       AS ENUM ('pending', 'approved', 'rejected', 'expired', 'done_automatically', 'blocked_by_guardrail');
CREATE TYPE system_status    AS ENUM ('up', 'degraded', 'down');
CREATE TYPE actor_kind       AS ENUM ('simulator', 'adapter', 'agent', 'human', 'system');

-- ---------------------------------------------------------------- 1. WHAT EXISTS

CREATE TABLE stations (
    code              char(3) PRIMARY KEY,              -- MCI, DEN, ICT ...
    name              text NOT NULL,
    is_base           boolean NOT NULL DEFAULT false,   -- big Southwest base (DEN, DAL, MDW, BWI)
    gates             int,
    wheelchair_staff  int,                              -- attendants on shift
    has_customs       boolean NOT NULL DEFAULT false,
    terminal_capacity int,                              -- safe number of people inside
    people_in_terminal int,                             -- live estimate
    ramp_open         boolean NOT NULL DEFAULT true,    -- false during lightning closures
    updated_at        timestamptz NOT NULL DEFAULT now(),
    source            text NOT NULL
);

CREATE TABLE aircraft (
    id                text PRIMARY KEY,                 -- A31
    type              text NOT NULL,                    -- 737-800, 737 MAX 8 ...
    seats             int NOT NULL,
    current_station   char(3) REFERENCES stations(code),
    airborne          boolean NOT NULL DEFAULT false,
    fuel_minutes_extra int,                             -- holding/diversion flexibility when airborne
    maint_status      text NOT NULL DEFAULT 'routine',  -- routine | inspection_due | critical | grounded
    maint_due_at      timestamptz,
    needs_inspection  boolean NOT NULL DEFAULT false,   -- e.g. after hail or a lightning strike
    updated_at        timestamptz NOT NULL DEFAULT now(),
    source            text NOT NULL
);

CREATE TABLE crews (
    id                 text PRIMARY KEY,                -- C31, R01 (reserve)
    base               char(3) REFERENCES stations(code),
    current_station    char(3) REFERENCES stations(code),
    pilots             int NOT NULL DEFAULT 2,
    flight_attendants  int NOT NULL DEFAULT 4,
    is_reserve         boolean NOT NULL DEFAULT false,
    available_from     timestamptz,                     -- reserves: when they can report
    pilot_duty_left_min int,                            -- the duty clock: minutes of legal duty left
    fa_duty_left_min   int,
    duty_started_at    timestamptz,
    rest_until         timestamptz,                     -- can't be assigned before this
    restrictions       text[],                          -- 'international', 'no_late_night' ...
    fatigue_reported   boolean NOT NULL DEFAULT false,  -- if true: remove from duty, no override
    updated_at         timestamptz NOT NULL DEFAULT now(),
    source             text NOT NULL
);

CREATE TABLE flights (
    id               text PRIMARY KEY,                  -- WN1031
    origin           char(3) NOT NULL REFERENCES stations(code),
    destination      char(3) NOT NULL REFERENCES stations(code),
    sched_dep        timestamptz NOT NULL,
    sched_arr        timestamptz,
    est_dep          timestamptz,
    edct             timestamptz,                       -- FAA-assigned takeoff time in a delay program
    aircraft_id      text REFERENCES aircraft(id),
    crew_id          text REFERENCES crews(id),
    next_flight_id   text REFERENCES flights(id),       -- the plane's next leg (rotation)
    priority         priority_level,
    status           flight_status NOT NULL DEFAULT 'scheduled',
    diverted_to      char(3) REFERENCES stations(code),
    tarmac_since     timestamptz,                       -- starts the 3-hour tarmac clock
    cancel_reason    text,
    controllable     boolean,                           -- was it the airline's fault? drives hotels/vouchers
    updated_at       timestamptz NOT NULL DEFAULT now(),
    source           text NOT NULL
);

-- Passengers are stored as groups per flight, not individuals (no personal data in the shared picture).
CREATE TABLE passenger_groups (
    id               bigserial PRIMARY KEY,
    flight_id        text NOT NULL REFERENCES flights(id),
    segment          text NOT NULL,       -- standard | connecting | child_alone | wheelchair | medical
                                          -- | international | family | loyalty | local | group
    count            int NOT NULL,
    final_destination char(3),
    rebooked         int NOT NULL DEFAULT 0,
    refunded         int NOT NULL DEFAULT 0,
    need_hotel       int NOT NULL DEFAULT 0,
    notified_at      timestamptz,         -- when this group was last told what's happening
    updated_at       timestamptz NOT NULL DEFAULT now(),
    source           text NOT NULL,
    UNIQUE (flight_id, segment)
);

CREATE TABLE hotels (
    id               text PRIMARY KEY,
    station          char(3) NOT NULL REFERENCES stations(code),
    name             text NOT NULL,
    rooms_total      int NOT NULL,
    rooms_available  int NOT NULL,
    rooms_held       int NOT NULL DEFAULT 0,            -- held by us, not yet assigned
    rate_usd         numeric(8,2),
    distance_miles   numeric(5,1),
    shuttle          text,                              -- yes | limited | no
    last_checkin     time,
    preferred_for    text,                              -- crew | passenger | mixed
    updated_at       timestamptz NOT NULL DEFAULT now(),
    source           text NOT NULL
);

-- ---------------------------------------------------------------- 2. WHAT'S HAPPENING

-- Everything that occurred, in order. Never updated or deleted.
CREATE TABLE events (
    id           bigserial PRIMARY KEY,
    occurred_at  timestamptz NOT NULL,                  -- simulated time
    type         text NOT NULL,                         -- forecast_updated, ground_stop_issued, system_down ...
    actor        actor_kind NOT NULL,
    actor_name   text NOT NULL,                         -- 'simulator', 'situation_agent', 'COO' ...
    station      char(3) REFERENCES stations(code),
    payload      jsonb NOT NULL,                        -- validated by a Pydantic model per type
    seed         bigint,                                -- simulator seed, so a run can be replayed
    recorded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_time_idx ON events (occurred_at);
CREATE INDEX events_type_idx ON events (type);

-- A disruption the Situation agent has detected, with its likely cause.
CREATE TABLE incidents (
    id                 bigserial PRIMARY KEY,
    kind               incident_kind NOT NULL,
    station            char(3) REFERENCES stations(code),
    title              text NOT NULL,                   -- 'FAA ground stop at MCI'
    likely_cause       text,                            -- 'thunderstorms per FAA advisory'
    confidence         numeric(3,2),                    -- 0-1
    controllable       boolean,                         -- airline's fault? (IT outage yes, weather no)
    status             incident_status NOT NULL DEFAULT 'suspected',
    expected_end_min   timestamptz,
    expected_end_max   timestamptz,
    opened_at          timestamptz NOT NULL,
    resolved_at        timestamptz,
    opened_by          text NOT NULL,                   -- usually 'situation_agent'
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Which events are evidence for which incident.
CREATE TABLE incident_events (
    incident_id  bigint REFERENCES incidents(id),
    event_id     bigint REFERENCES events(id),
    PRIMARY KEY (incident_id, event_id)
);

-- Which connected systems are working right now.
CREATE TABLE system_health (
    system          text PRIMARY KEY,                   -- amadeus, crew_optimization, dispatch, faa_feed ...
    status          system_status NOT NULL DEFAULT 'up',
    last_heartbeat  timestamptz,
    data_age_sec    int,                                -- how stale our copy is
    note            text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 3. WHAT'S BEING DECIDED

-- Options an agent proposes. The Guardrail checks every one before anyone sees it.
CREATE TABLE recommendations (
    id               bigserial PRIMARY KEY,
    incident_id      bigint REFERENCES incidents(id),
    agent            text NOT NULL,                     -- network_recovery, passenger_care ...
    title            text NOT NULL,                     -- 'A31: hold, divert ICT, or DAL?'
    why              text NOT NULL,
    options          jsonb NOT NULL,                    -- [{label, upside, downside, cost_usd, pax_affected, recommended}]
    autonomy         autonomy_level NOT NULL,           -- auto | needs_approval | human_only
    approver_role    text,                              -- 'captain+dispatcher', 'COO', 'station_manager' ...
    deadline         timestamptz,                       -- decide by
    rank             int,                               -- set by the orchestrator: 1 = most urgent
    guardrail_ok     boolean,
    guardrail_notes  text[],                            -- rules checked / reasons blocked
    data_as_of       timestamptz,                       -- the freshest data the options were based on
    status           rec_status NOT NULL DEFAULT 'pending',
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Who approved what, and when.
CREATE TABLE decisions (
    id                 bigserial PRIMARY KEY,
    recommendation_id  bigint NOT NULL REFERENCES recommendations(id),
    decided_by         text NOT NULL,                   -- a person, or 'auto' for low-risk actions
    role               text NOT NULL,
    verdict            text NOT NULL,                   -- approved | rejected | changed
    chosen_option      text,
    reason             text,
    decided_at         timestamptz NOT NULL
);

-- What was actually sent to an existing system after a decision (through an adapter).
CREATE TABLE actions (
    id             bigserial PRIMARY KEY,
    decision_id    bigint NOT NULL REFERENCES decisions(id),
    target_system  text NOT NULL,                       -- amadeus, crew_optimization, messaging ...
    request        jsonb NOT NULL,
    result         text,                                -- sent | confirmed | failed
    executed_at    timestamptz NOT NULL
);

-- Every change by anyone: agents, humans, adapters. Never updated or deleted.
CREATE TABLE audit_log (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL,
    actor        actor_kind NOT NULL,
    actor_name   text NOT NULL,
    what         text NOT NULL,                         -- 'flight status changed', 'recommendation created' ...
    entity_type  text NOT NULL,
    entity_id    text NOT NULL,
    before       jsonb,
    after        jsonb,
    trace_id     text                                   -- links to OpenTelemetry traces
);
CREATE INDEX audit_entity_idx ON audit_log (entity_type, entity_id);
