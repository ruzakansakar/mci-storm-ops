"""Build config/decisions.yaml from the compact table below (run once after editing the table).

Run:  python -m scripts.build_decisions
The YAML file is the catalog everyone reads; this script only saves typing. decided_by is worked out from config/rules/autonomy.yaml
(with the never-automatic rule for rungs 1-4), so the two can never disagree. in_story is worked out by playing the a31_storm seed 7 day.
"""
import yaml

from backend.rules import load_rules
from backend.rules.autonomy import autonomy_for
from backend.settings import ROOT

OUT = ROOT / "config" / "decisions.yaml"

FAMILIES = {
    "A": ("detect_and_classify", ["events", "incidents", "flights.status", "flights.est_dep", "stations.ramp_open", "system_health"], ["passengers", "crew", "airport"]),
    "B": ("flights", ["flights", "aircraft", "crews.pilot_duty_left_min", "crews.fa_duty_left_min", "passenger_groups", "later_capacity"], ["passengers", "crew", "aircraft", "cost"]),
    "C": ("aircraft_and_maintenance", ["aircraft", "flights.aircraft_id", "stations"], ["aircraft", "passengers", "cost"]),
    "D": ("crew", ["crews", "flights.crew_id", "flights.reserve_crew_ids", "flights.est_dep"], ["crew", "passengers"]),
    "E": ("ramp_and_station", ["staff", "stations", "flights.est_arr", "passenger_groups"], ["ground staff", "passengers", "airport"]),
    "F": ("passengers_on_board", ["flights.tarmac_since", "flights.status", "stations", "passenger_groups"], ["passengers", "crew", "airport"]),
    "G": ("passengers_in_terminal", ["stations.people_in_terminal", "stations.terminal_capacity", "staff", "passenger_groups"], ["passengers", "ground staff", "airport"]),
    "H": ("passengers_not_yet_at_airport", ["flights", "bookings", "passengers.contact_channels"], ["passengers", "cost"]),
    "I": ("connecting_passengers", ["bookings", "flights", "later_capacity", "bags"], ["passengers"]),
    "J": ("vulnerable_passengers", ["passenger_groups.segment", "passengers.segment", "bookings", "staff"], ["passengers", "ground staff"]),
    "K": ("rebooking", ["bookings", "flights", "later_capacity", "passenger_groups"], ["passengers", "cost"]),
    "L": ("bags", ["bags", "bookings", "flights"], ["passengers", "ground staff", "cost"]),
    "M": ("hotels_and_transport", ["hotels", "transport_options", "hotel_rate_tiers", "passengers.lives_locally", "passengers.overnight"], ["passengers", "partners", "cost"]),
    "N": ("money", ["passengers", "bookings", "flights.controllable", "incidents.controllable"], ["passengers", "cost"]),
    "O": ("communication", ["channel_views", "passengers.contact_channels", "flights", "recommendations"], ["passengers", "crew", "ground staff"]),
    "P": ("partners", ["incidents", "stations", "hotels", "flights"], ["partners", "airport", "cost"]),
    "Q": ("recovery_and_end_of_day", ["flights", "crews", "aircraft.current_station", "passengers.overnight", "incidents"], ["passengers", "crew", "aircraft"]),
    "R": ("legal_and_reporting", ["audit_log", "events", "flights", "incidents"], ["passengers", "crew", "cost"]),
    "S": ("systems_and_data", ["system_health", "recommendations", "events"], ["passengers", "crew", "ground staff"]),
    "T": ("security_and_other_incidents", ["incidents", "stations", "events"], ["passengers", "ground staff", "airport"]),
}

ORC, SIT, NET, STA, CARE, COM = "orchestrator", "situation", "network_recovery", "station_staff", "passenger_care", "communication"
TAR = ["tarmac_domestic_min", "tarmac_international_min", "tarmac_clock_start"]
CREW = ["crew_duty_buffer_min", "pilot_min_rest_hours", "pilot_sleep_opportunity_hours", "pilot_fdp_table_b", "crew_availability"]
CUM = ["pilot_weekly_free_hours", "pilot_fdp_168h_max", "pilot_fdp_672h_max", "pilot_flight_time_672h_max", "pilot_flight_time_365d_max"]
FA = ["fa_duty_max_h", "fa_duty_max_h_by_extra", "fa_rest_after_14h_or_less", "fa_rest_after_over_14h", "fa_weekly_off_h", "fa_beyond_control_exception"]
GS = ["ground_staff_max_shift_min", "ground_staff_break_interval_min", "ground_staff_break_length_min", "ground_staff_min_gap_min", "ground_staff_max_consecutive_days"]
KID = ["child_alone_min_age", "child_alone_max_age", "child_alone_nonstop_or_same_plane", "child_alone_international_allowed", "child_alone_stranded_supervised"]
WC = ["wheelchair_help_boarding_connecting", "wheelchair_unattended_max_min"]
MONEY = ["significant_delay_domestic_min", "significant_delay_international_min", "controllable_delay_min", "uncontrollable_causes", "controllable_causes"]
DIV = ["customs_international_arrivals", "tsa_rescreen_outside_secure_area", "tsa_checkpoint_hours", "southwest_station_hours"]
LIGHT = ["lightning_ramp_radius_miles", "lightning_ramp_blocked_activities"]

# id, question, trigger, deadline, proposed_by, action, safety_rung, rules, simulator_event
D = [
    # ---------------------------------------------------------------- A
    ("A01", "Is this a real disruption or normal variation?", "Several flights delayed or a stop announced (ground_stop_started)", "within 5 min of the first signal", SIT, "assess_situation", None, ["missing_or_stale_data_blocks"], "ground_stop_started"),
    ("A02", "What type is it: weather, FAA program, IT outage, airport, security, crew, other?", "An incident is opened", "within 10 min of opening the incident", SIT, "classify_cause", None, ["uncontrollable_causes", "controllable_causes"], "ground_stop_started"),
    ("A03", "Is it within Southwest's control (controllable)?", "The cause is named (A02)", "before any compensation is proposed", SIT, "classify_cause", 8, ["controllable_causes", "uncontrollable_causes", "controllable_delay_min"], "ground_stop_started"),
    ("A04", "Is this a new incident, or part of one already open?", "A new disruption signal while an incident is open", "within 5 min of the signal", ORC, "open_incident", None, [], "ground_stop_started"),
    ("A05", "How long will it last (best / likely / worst), and how sure are we?", "An incident is opened, or the FAA updates the program", "within 15 min of the stop being announced", SIT, "update_expected_end", None, [], "ground_stop_started"),
    ("A06", "The FAA extended or shortened the stop: re-plan now or wait?", "ground_stop_extended", "within 10 min of the change", SIT, "replan_after_program_change", 6, [], "ground_stop_extended"),
    ("A07", "Which flights, crews and passengers are affected, and how badly?", "An incident is opened or the expected end changes", "within 15 min of the stop being announced", SIT, "assess_situation", None, ["crew_duty_buffer_min"], "flight_delayed"),
    ("A08", "Is the data fresh enough to trust? Which systems are stale or down?", "A system's data age passes its limit", "before any recommendation uses that system", SIT, "flag_stale_data", None, ["missing_or_stale_data_blocks"], "missing"),
    ("A09", "Two systems disagree on a fact: which wins, and who resolves it?", "Two sources give different values for the same fact", "before any recommendation uses that fact", SIT, "resolve_source_conflict", None, ["missing_or_stale_data_blocks", "message_matches_truth"], "channel_update_failed"),
    ("A10", "When is the incident over and recovery starting?", "ground_stop_ended", "within 10 min of the stop ending", ORC, "declare_recovery", 6, [], "ground_stop_ended"),
    # ---------------------------------------------------------------- B
    ("B01", "Hold a departure at the gate, or board and wait on the plane?", "A departure is delayed by a stop (flight_delayed)", "before boarding starts; before the 2 h tarmac mark (tarmac_food_water_min)", NET, "hold_departure_at_gate", 1, TAR + ["tarmac_food_water_min"], "flight_delayed"),
    ("B02", "Delay a flight, and to what time?", "Projected departure moves (flight_delay_updated)", "before the scheduled departure", NET, "delay_flight", 4, CREW + ["proposal_conflicts_block"], "flight_delay_updated"),
    ("B03", "Cancel a flight? Which ones (priority, load, crew and aircraft, last flight to a city)?", "Projected delay passes the cancel cutoff, or a crew runs out of duty", "before the crew's duty limit minus 30 min (crew_duty_buffer_min)", NET, "cancel_flight", 4, CREW + ["proposal_conflicts_block", "significant_delay_domestic_min"], "flight_cancelled"),
    ("B04", "Cancel early to thin the schedule, or wait?", "A stop of 60+ min is announced", "within 15 min of the stop being announced", NET, "cancel_flight", 6, ["proposal_conflicts_block"], "flight_cancelled"),
    ("B05", "Combine two lightly loaded flights to the same city?", "Two flights to the same city are both delayed and under half full", "before either boards", NET, "combine_flights", 6, ["proposal_conflicts_block", "no_double_assignment"], "missing"),
    ("B06", "Which flights to protect first (hubs, last of the day, connections, international)?", "More delayed flights than slots after the stop", "within 15 min of the stop being announced", NET, "set_flight_protection_order", 6, [], "flight_delayed"),
    ("B07", "Can we make the assigned takeoff slot (EDCT)? Ask for a new one?", "An EDCT is assigned that the flight can't meet", "before the EDCT window closes (edct_compliance_window_min)", NET, "request_new_edct", 6, ["edct_compliance_window_min"], "missing"),
    ("B08", "Divert an airborne inbound flight, and where?", "A holding flight's spare fuel nears the floor (flight_holding)", "before spare fuel minus the flight to the alternate reaches fuel_buffer_min", NET, "divert_flight", 2, ["fuel_legal_reserve", "fuel_buffer_min", "customs_international_arrivals", "southwest_station_hours"], "flight_diverted"),
    ("B09", "Keep holding in the air, or divert now (fuel)?", "flight_holding", "every minute while holding; before fuel_buffer_min", NET, "divert_flight", 2, ["fuel_legal_reserve", "fuel_buffer_min"], "flight_holding"),
    ("B10", "Return a taxiing plane to the gate?", "A plane that left the gate is held on the ground (tarmac_started)", "before the tarmac limit minus the return time (tarmac_start_return_before_limit_min)", NET, "return_to_gate", 1, TAR + ["tarmac_start_return_before_limit_min"], "tarmac_started"),
    ("B11", "Add an extra flight or a bigger plane during recovery?", "Rebooking demand for a city is larger than the seats left today", "before the last departure to that city", NET, "add_extra_flight", 6, ["no_double_assignment"], "next_day_flight_created"),
    ("B12", "Fly an empty plane to reposition it?", "A plane is in the wrong place for its next flight", "before its next flight's departure minus the flying time", NET, "reposition_aircraft", 6, ["no_double_assignment"], "flight_resumed"),
    ("B13", "Will a late flight land or leave inside the destination's curfew?", "A flight's estimated time moves into a curfew window", "before the flight departs", NET, "flag_curfew_risk", 3, ["airport_curfews"], "missing"),
    ("B14", "In what order do flights restart when the stop lifts?", "ground_stop_ended", "before the stop ends (ready at the end time)", NET, "set_flight_protection_order", 6, [], "ground_stop_ended"),
    # ---------------------------------------------------------------- C
    ("C01", "Lightning strike: inspect before the next flight?", "A plane is reported struck by lightning (aircraft_inspection_needed)", "before the plane's next departure", NET, "inspect_after_lightning_strike", 2, ["lightning_strike_inspection"], "aircraft_inspection_needed"),
    ("C02", "Will the delay push the plane past its maintenance deadline?", "A delay moves a flight's end past aircraft.maint_due_at", "before the flight departs", NET, "flag_maintenance_deadline", 2, [], "flight_delay_updated"),
    ("C03", "Swap aircraft between flights?", "A plane is late, broken or out of position for its next flight", "before the affected departure", NET, "swap_aircraft", 2, ["no_double_assignment", "proposal_conflicts_block", "lightning_strike_inspection"], "missing"),
    ("C04", "A diverted plane is out of position: how does it get back for its next flight?", "flight_diverted", "before its next departure minus the flight back", NET, "recover_diverted_aircraft", 6, ["fuel_legal_reserve"], "flight_resumed"),
    ("C05", "Refuel at the diversion airport?", "A diverted plane lands with low spare fuel", "before it leaves the diversion airport", NET, "refuel_at_diversion", 2, ["fuel_legal_reserve", "fuel_buffer_min"], "flight_diverted"),
    ("C06", "Where should planes spend the night for tomorrow's first flights?", "Evening, with planes out of position", "before the last flight of the day", NET, "reposition_aircraft", 6, [], "missing"),
    ("C07", "A part or mechanic is needed at a station without maintenance", "A plane at a diversion station needs maintenance", "before the plane's next departure", NET, "request_maintenance_support", 2, [], "missing"),
    ("C08", "Release the plane after inspection?", "inspection_completed", "before the plane's next departure", NET, "release_aircraft_after_inspection", 2, ["lightning_strike_inspection"], "inspection_completed"),
    # ---------------------------------------------------------------- D (all proposed by network_recovery)
    ("D01", "Will the pilots go over their maximum duty period (Table B) if delayed?", "A delay is proposed or the duty clock runs low (crew_duty_low)", "before any delay is approved", NET, "flag_crew_legality", 4, CREW, "crew_duty_low"),
    ("D02", "Extend pilot duty up to 2 h before takeoff?", "Pilots will go over Table B and no reserve is free", "before the pilots' duty limit", NET, "extend_crew_duty", 4, ["pilot_fdp_extension_before_takeoff_max_h", "pilot_fdp_extension_once_over_min", "pilot_fdp_extension_cumulative_cap", "pilot_fdp_extension_after_takeoff"], "pilot_duty_extended"),
    ("D03", "Call in a reserve pilot pair or flight attendant team, and which one?", "A crew's duty left drops below what its flight needs", "before the crew's duty limit minus 30 min (crew_duty_buffer_min)", NET, "call_in_reserve_crew", 4, CREW + ["no_double_assignment"], "reserve_crew_assigned"),
    ("D04", "Fly a crew in as passengers (deadhead)?", "No legal crew is at the station that needs one", "before the flight's departure minus the deadhead flight", NET, "deadhead_crew", 4, CREW + CUM, "missing"),
    ("D05", "Is the crew legal for tomorrow (rest)? Delay tomorrow's first flights?", "A crew finishes late tonight", "before tomorrow's first departure minus 10 h (pilot_min_rest_hours)", NET, "flag_crew_for_tomorrow", 4, ["pilot_min_rest_hours", "pilot_sleep_opportunity_hours", "fa_rest_after_14h_or_less", "fa_rest_after_over_14h"], "missing"),
    ("D06", "Crew hotel at MCI or at the diversion station", "A crew can't fly again today, or is stranded at a diversion station", "before the crew's duty ends", NET, "book_crew_hotel", None, ["action_book_crew_hotel"], "flight_diverted"),
    ("D07", "Fatigue report: remove from duty (hard stop; a human confirms)", "A crew member reports fatigue (crews.fatigue_reported)", "immediately: the report stands", NET, "remove_crew_for_fatigue", 4, ["pilot_fatigue_report", "crew_availability"], "missing"),
    ("D08", "Flight attendant duty limit reached, or extra flight attendants needed?", "Flight attendant duty left drops below what the flight needs", "before the flight attendants' duty limit minus 30 min", NET, "call_in_reserve_crew", 4, FA + ["crew_duty_buffer_min"], "crew_out_of_duty"),
    ("D09", "Would a reassignment break the weekly or cumulative limits?", "A crew swap, reserve or deadhead is proposed", "before the reassignment is approved", NET, "flag_crew_legality", 4, CUM + ["pilot_fdp_extension_cumulative_cap"], "missing"),
    ("D10", "A crew stuck at a diversion station is running out of duty", "A diverted flight's crew duty drops below what the flight home needs", "before that crew's duty limit minus 30 min", NET, "call_in_reserve_crew", 4, CREW + ["southwest_station_hours"], "flight_diverted"),
    ("D11", "Swap crews between flights?", "Two flights' crews would each be legal on the other flight but not their own", "before the earlier of the two departures", NET, "swap_crews", 4, CREW + ["no_double_assignment"], "missing"),
    ("D12", "Report a duty extension over 30 min to the FAA within 10 days", "pilot_duty_extended with more than 30 min", "within 10 days of the extension (pilot_fdp_extension_report_days)", NET, "prepare_faa_extension_report", None, ["pilot_fdp_extension_report_days", "pilot_fdp_extension_once_over_min"], "pilot_duty_extended"),
    ("D13", "A crew member is sick, fatigued or on leave: who replaces them?", "crews.availability changes to sick, fatigued or on_leave", "before that crew's next departure", NET, "call_in_reserve_crew", 4, ["crew_availability"] + CREW, "missing"),
    # ---------------------------------------------------------------- E
    ("E01", "Close the ramp for lightning?", "Lightning within the ramp distance (ramp_closed)", "immediately", STA, "ramp_closure", 1, LIGHT, "ramp_closed"),
    ("E02", "Reopen the ramp?", "No lightning within the ramp distance for the set time (ramp_reopened)", "as soon as it is safe", STA, "reopen_ramp", 1, LIGHT, "ramp_reopened"),
    ("E03", "Gates are running out: which gates, remote stands or stairs?", "Free gates fall below the arrivals due in the next hour", "before the next arrival needs a gate", STA, "assign_gates", 3, ["tarmac_domestic_min"], "tarmac_started"),
    ("E04", "Move gate agents to the busiest gates", "Lines at some gates much longer than others", "within 15 min of the lines building", STA, "move_gate_agents", 7, GS, "terminal_crowd_growth"),
    ("E05", "A staff shift is ending, overtime or a break is due", "A shift end or break time is reached during the stop", "before the shift end or the break limit", STA, "call_in_ground_staff", 4, GS, "missing"),
    ("E06", "Call in extra staff?", "Staff needed for the recovery exceeds staff on duty", "within 30 min of the stop being announced", STA, "call_in_ground_staff", 4, GS, "missing"),
    ("E07", "Are there enough wheelchair staff for arrivals and diversions?", "Wheelchair passengers due exceed wheelchair staff free", "before the flights with wheelchair passengers arrive", STA, "request_wheelchair_help", 5, WC, "flight_arrived"),
    ("E08", "After a ramp closure, whose bags are loaded or unloaded first?", "ramp_reopened", "within 10 min of the ramp reopening", STA, "set_bag_priority", 7, [], "ramp_reopened"),
    ("E09", "Fueling or catering stopped: what is the impact on departures?", "ramp_closed", "within 10 min of the ramp closing", STA, "flag_ramp_service_impact", 1, LIGHT, "ramp_closed"),
    ("E10", "Gate conflicts when many delayed flights arrive at once", "More arrivals due in an hour than free gates", "before the first conflicting arrival", STA, "assign_gates", 3, ["tarmac_domestic_min"], "flight_arrived"),
    ("E11", "Ask the airport for extra gates or hardstands?", "Gate plan still short after E03 and E10", "before the first plane that has no gate lands", STA, "request_airport_resources", 3, [], "missing"),
    ("E12", "Staff safety: lightning, heat or cold, very long shifts", "Lightning near the ramp, extreme temperature, or a shift near its limit", "immediately", STA, "stop_work_for_staff_safety", 1, LIGHT + GS, "ramp_closed"),
    # ---------------------------------------------------------------- F
    ("F01", "Tell passengers on board the status at 30 minutes", "tarmac_started", "by 30 min into the tarmac delay (tarmac_status_announcement_min)", COM, "send_status_update", 1, ["tarmac_status_announcement_min", "tarmac_clock_start"], "tarmac_started"),
    ("F02", "Food and water by 2 hours", "tarmac_started, and the delay will pass 2 h", "before the 2 h tarmac mark (tarmac_food_water_min)", STA, "arrange_food_water", 1, ["tarmac_food_water_min"], "tarmac_started"),
    ("F03", "Are the toilets working? Is a service vehicle needed?", "A plane has waited on the tarmac for 60 min", "always: toilets must work from minute one (tarmac_lavatories_medical)", STA, "request_service_vehicle", 1, ["tarmac_lavatories_medical"], "tarmac_started"),
    ("F04", "Medical emergency on board: return to the gate, call EMS?", "Crew reports a medical emergency on a waiting plane", "immediately", STA, "medical_response", 1, ["tarmac_lavatories_medical"], "missing"),
    ("F05", "Offer deplaning when a gate or stairs are available, and announce it", "A gate or stairs become free while a plane waits (tarmac_ended)", "each time deplaning becomes possible (tarmac_notify_each_deplane_chance)", STA, "start_deplaning", 1, ["tarmac_notify_each_deplane_chance", "tarmac_clock_start"], "tarmac_ended"),
    ("F06", "Start returning to the gate before the 3 h / 4 h limit", "A waiting plane nears the limit minus the return time", "before the limit minus 30 min (tarmac_start_return_before_limit_min)", STA, "return_to_gate", 1, TAR + ["tarmac_start_return_before_limit_min"], "tarmac_started"),
    ("F07", "Keep-on-board exception (captain: safety or security, or ATC)", "Deplaning is blocked and the limit is near (tarmac helper says captain_decides)", "before the tarmac limit (tarmac_domestic_min / tarmac_international_min)", STA, "keep_on_board_exception", 1, ["tarmac_exception_captain_safety", "tarmac_exception_atc", "action_keep_on_board_exception", "override_only_by_named_human"], "missing"),
    ("F08", "Getting off at a diversion airport: staff, TSA, customs?", "flight_diverted", "before the diverted plane reaches the gate", STA, "arrange_diversion_deplaning", 3, DIV + ["tarmac_clock_start"], "flight_diverted"),
    ("F09", "Wheelchair and child-alone help getting off", "A plane with wheelchair users or children alone is about to deplane", "before deplaning starts", STA, "request_wheelchair_help", 5, WC + ["child_alone_stranded_supervised"], "flight_arrived"),
    ("F10", "File the DOT tarmac report if the limit was exceeded", "A tarmac delay passed 3 h domestic or 4 h international", "within 30 days of the delay (tarmac_dot_report_days)", ORC, "file_dot_tarmac_report", None, ["tarmac_dot_report_days"], "missing"),
    # ---------------------------------------------------------------- G
    ("G01", "Terminal crowding: more space, ask the airport for help?", "terminal_crowd_growth, terminal above the alert share", "before the terminal is full (terminal_crowding_alert_share)", STA, "request_airport_resources", 1, ["terminal_crowding_alert_share"], "terminal_crowd_growth"),
    ("G02", "Rebooking lines too long: move agents, push self-service in the app?", "Rebooking line wait passes 30 min", "within 15 min of the lines building", STA, "move_gate_agents", 7, GS, "terminal_crowd_growth"),
    ("G03", "Food and water when restaurants close", "Passengers stuck in the terminal after the restaurants' closing time", "before the restaurants close", CARE, "arrange_terminal_care", 5, ["terminal_care_after_min"], "missing"),
    ("G04", "Cots, blankets, overnight in the terminal", "Passengers stranded overnight who can't get a hotel (passenger_stranded_overnight)", "before 22:00", CARE, "arrange_terminal_care", 5, ["terminal_care_after_min", "send_locals_home"], "passenger_stranded_overnight"),
    ("G05", "The security checkpoint is closing: people who leave can't come back in", "TSA checkpoint closing time approaches with passengers in the terminal", "before the checkpoint closes (tsa_checkpoint_hours)", COM, "send_status_update", 3, ["tsa_checkpoint_hours", "tsa_rescreen_outside_secure_area"], "missing"),
    ("G06", "Passengers who want to leave and come back tomorrow", "Passengers rebooked onto a next-day flight (next_day_flight_created)", "before they leave the secure area", CARE, "rebook_passenger", 7, ["send_locals_home"], "next_day_flight_created"),
    ("G07", "Medical or elderly needs in the terminal", "A passenger asks for help, or a long wait passes the care time", "within 15 min of the request", CARE, "arrange_terminal_care", 5, ["terminal_care_after_min"], "missing"),
    ("G08", "Gate announcements: how often?", "A stop is announced", "first one within 15 min; then every status_update_interval_min", COM, "send_status_update", 7, ["status_update_interval_min"], "ground_stop_started"),
    ("G09", "Upset or unruly passengers: call security?", "Staff report an unruly passenger", "immediately", STA, "call_security", 1, [], "missing"),
    ("G10", "Information for people who can't hear, see or read English", "Any message to passengers", "at the same time as the message to everyone else", COM, "send_status_update", 5, ["wheelchair_help_boarding_connecting"], "channel_view_updated"),
    # ---------------------------------------------------------------- H
    ("H01", "Tell passengers not to come yet, or to stay home?", "A stop is announced that will last past their departure", "before they leave for the airport (about 3 h before departure)", COM, "send_status_update", 7, ["status_update_interval_min"], "ground_stop_started"),
    ("H02", "Offer a free same-day change or waived fees?", "A stop expected to last 2+ h", "within 30 min of the stop being announced (travel_waiver_after_min)", CARE, "waive_change_fees", 8, ["travel_waiver_after_min"], "ground_stop_started"),
    ("H03", "Check-in and bag-drop cutoffs during the delay", "Departures delayed by more than the check-in cutoff", "before the original cutoff", STA, "adjust_checkin_cutoffs", 7, [], "flight_delayed"),
    ("H04", "Passengers already driving in: how do we reach them?", "A flight is cancelled or badly delayed less than 2 h before departure", "within 10 min of the change", COM, "send_status_update", 7, [], "flight_cancelled"),
    ("H05", "Offer a refund up front for cancelled flights?", "flight_cancelled", "with the first cancellation notice", CARE, "offer_refund", 8, MONEY + ["refund_timing_days"], "flight_cancelled"),
    ("H06", "Rebook proactively before they arrive?", "flight_cancelled, or a delay that breaks a connection", "before they arrive at the airport", CARE, "rebook_passenger", 4, KID + ["min_connection_min"], "flight_cancelled"),
    # ---------------------------------------------------------------- I
    ("I01", "Who will miss their connection?", "An inbound flight's arrival moves (flight_delay_updated)", "before the connecting flight closes boarding", CARE, "identify_misconnects", None, ["min_connection_min"], "missed_connection"),
    ("I02", "Hold an outbound flight for connecting passengers?", "Connecting passengers will arrive just after the outbound leaves", "before the outbound's scheduled departure", NET, "hold_departure_for_connections", 4, CREW + ["proposal_conflicts_block"], "missing"),
    ("I03", "Rebook at MCI or at the next hub?", "missed_connection", "before the next possible flight closes boarding", CARE, "rebook_passenger", 4, KID + ["min_connection_min"], "missed_connection"),
    ("I04", "Protect the last connection of the day", "The last connection to a city is at risk", "before the last connecting flight closes boarding", NET, "set_flight_protection_order", 6, ["seat_priority_order"], "missed_connection"),
    ("I05", "Make bags follow the new connection", "A passenger with checked bags is rebooked", "before the new flight's bags are loaded", CARE, "reroute_bags", None, ["bag_refund_hours_domestic"], "missed_connection"),
    ("I06", "Passengers ending at MCI vs going onward", "Passengers are rebooked during the stop", "within 15 min of the rebooking", CARE, "rebook_passenger", 7, ["send_locals_home"], "missed_connection"),
    # ---------------------------------------------------------------- J
    ("J01", "Child flying alone, flight cancelled: supervise and call the guardian", "flight_cancelled with a child flying alone booked", "immediately", CARE, "supervise_child_alone", 5, KID, "flight_cancelled"),
    ("J02", "Child rebooking: nonstop or same plane only, never international", "A child flying alone needs a new flight", "before the child is rebooked", CARE, "rebook_passenger", 4, KID, "flight_cancelled"),
    ("J03", "Child stranded overnight: never alone in a hotel; guardian pickup", "A child flying alone is stranded overnight", "before 21:00 (assumption)", CARE, "supervise_child_alone", 5, KID, "passenger_stranded_overnight"),
    ("J04", "A wheelchair user is never left more than 30 minutes unattended", "A wheelchair passenger arrives or is waiting", "within 30 min (wheelchair_unattended_max_min)", STA, "request_wheelchair_help", 5, WC, "flight_arrived"),
    ("J05", "A wheelchair or mobility device is damaged or sent to the wrong place", "A device is reported damaged or missing", "before the passenger leaves the airport", CARE, "report_mobility_device_issue", 5, ["wheelchair_rule_2024_status", "disability_complaint_response_days"], "missing"),
    ("J06", "Elderly or medical passengers needing medication or care", "A passenger reports a need, or the wait passes the care time", "within 15 min of the request", CARE, "arrange_terminal_care", 5, ["terminal_care_after_min"], "missing"),
    ("J07", "Infants: formula and diapers during long waits", "Families with infants wait past the care time", "before the care time (terminal_care_after_min)", CARE, "arrange_terminal_care", 5, ["terminal_care_after_min"], "missing"),
    ("J08", "Service animals: relief areas", "Passengers with service animals wait past the care time", "before the care time", CARE, "arrange_terminal_care", 5, ["terminal_care_after_min"], "missing"),
    ("J09", "Language help", "A passenger needs help in another language", "with the next message", COM, "send_status_update", 5, [], "missing"),
    ("J10", "Oxygen concentrators or batteries running low", "A delay passes 1 h with a concentrator user booked", "before the battery runs out (poc_battery_check)", CARE, "arrange_terminal_care", 1, ["poc_battery_check"], "missing"),
    # ---------------------------------------------------------------- K
    ("K01", "Rebook onto which flight (today or tomorrow)?", "flight_cancelled or missed_connection", "before the chosen flight closes boarding", CARE, "rebook_passenger", 4, KID + ["min_connection_min", "proposal_conflicts_block"], "flight_cancelled"),
    ("K02", "Rebook on another airline?", "No Southwest seat today or tomorrow", "before the other airline's flight closes", CARE, "rebook_on_other_airline", 8, ["southwest_rebooks_on_other_airlines"], "missing"),
    ("K03", "Who gets scarce seats first (priority order, fair and legal)?", "More passengers to rebook than seats left (next_day_flight_created)", "before seats are given out", CARE, "rebook_passenger", 5, ["seat_priority_order", "loyalty_priority_fairness"] + KID[:2], "next_day_flight_created"),
    ("K04", "Hold a seat while the passenger decides", "A passenger is offered a seat", "for up to 10 min (assumption)", CARE, "hold_seat", 7, [], "missing"),
    ("K05", "Keep families and groups together", "A booking with several people is rebooked", "before seats are given out", CARE, "rebook_passenger", 5, ["keep_families_together", "child_alone_stranded_supervised"], "flight_cancelled"),
    ("K06", "Fly to a nearby alternate city?", "No seat to the passenger's city today", "before the alternate flight closes boarding", CARE, "rebook_passenger", 7, [], "missing"),
    ("K07", "The passenger declines rebooking: refund", "A passenger declines the rebooking offered", "pay within 7 business days for cards (refund_timing_days)", CARE, "offer_refund", 8, MONEY + ["refund_timing_days"], "missing"),
    ("K08", "Standby lists", "A flight to a busy city has passengers waiting for a seat", "before the flight closes boarding", CARE, "manage_standby", 7, ["seat_priority_order"], "missing"),
    ("K09", "Tomorrow's seats run out too", "Next-day flights to a city are full", "before the last flight of the day leaves", NET, "add_extra_flight", 6, ["seat_priority_order"], "next_day_flight_created"),
    ("K10", "Should loyalty status change priority? (fairness check)", "Seats are given out by priority", "before seats are given out", CARE, "rebook_passenger", 5, ["loyalty_priority_fairness", "seat_priority_order"], "missing"),
    # ---------------------------------------------------------------- L
    ("L01", "Hold bags, or load them with the plane?", "A flight's departure is uncertain", "before loading starts", STA, "hold_bags", 7, LIGHT, "flight_delayed"),
    ("L02", "Re-tag bags for the new itinerary", "A passenger with bags is rebooked", "before the new flight's bags are loaded", CARE, "reroute_bags", None, ["bag_refund_hours_domestic"], "missed_connection"),
    ("L03", "Bags of cancelled passengers: give back, or keep for the new flight?", "flight_cancelled", "before passengers leave the airport", STA, "hold_bags", 7, ["bag_refund_hours_domestic"], "flight_cancelled"),
    ("L04", "Deliver bags to the hotel or home?", "A passenger leaves without their bag", "before 12 h after arrival (bag_refund_hours_domestic)", CARE, "deliver_bags", 8, ["bag_refund_hours_domestic", "bag_refund_hours_international"], "missing"),
    ("L05", "File a mishandled bag report", "A bag is late or lost", "when the passenger reports it", CARE, "file_mishandled_bag_report", None, ["bag_refund_hours_domestic"], "missing"),
    ("L06", "Bag fee refund (12 h domestic)", "A checked bag is delivered more than 12 h after a domestic arrival", "with the refund timing (refund_timing_days)", CARE, "issue_compensation", 8, ["bag_refund_hours_domestic", "bag_refund_hours_international", "bag_refund_hours_international_long", "long_flight_hours", "bag_fee_usd", "refund_timing_days"], "missing"),
    ("L07", "The bag system disagrees with the booking", "Bag routing differs from the passenger's itinerary (bag_system mismatch)", "before the bag is loaded", COM, "draft_message", None, ["message_matches_truth"], "channel_update_failed"),
    ("L08", "Gate-checked strollers and wheelchairs", "A plane with gate-checked items arrives at a gate or stairs", "at deplaning", STA, "request_wheelchair_help", 5, WC, "flight_arrived"),
    # ---------------------------------------------------------------- M
    ("M01", "How many rooms to hold, and when?", "Passengers are likely to be stranded overnight", "before 19:00 (rooms run out)", CARE, "hold_hotel_rooms", 8, ["hotel_needed_if_overnight", "action_hold_hotel_rooms"], "passenger_stranded_overnight"),
    ("M02", "Which hotel (distance, price, shuttle, preference tier)?", "Rooms are needed (M01)", "before the hotel's last check-in", CARE, "hold_hotel_rooms", 8, ["hotel_needed_if_overnight"], "passenger_stranded_overnight"),
    ("M03", "Hotels running out: next tier, or a farther city?", "Rooms held are fewer than rooms needed", "before the last hotel's last check-in", CARE, "hold_hotel_rooms", 8, ["hotel_needed_if_overnight"], "missing"),
    ("M04", "Local residents: send them home instead, with transport?", "Stranded passengers who live locally (passengers.lives_locally)", "before transport stops for the night", CARE, "arrange_ground_transport", 8, ["send_locals_home"], "passenger_stranded_overnight"),
    ("M05", "How many buses, vans or rideshares?", "Rooms are booked or locals are sent home", "before the first group is ready to leave", CARE, "arrange_ground_transport", 8, ["send_locals_home"], "passenger_stranded_overnight"),
    ("M06", "Will they reach the hotel before its last check-in?", "Rooms are booked late in the evening", "before the hotel's last check-in (hotels.last_checkin)", CARE, "hold_hotel_rooms", 8, [], "missing"),
    ("M07", "Meal vouchers for overnight passengers", "Passengers stranded overnight by a controllable cause", "before restaurants close", CARE, "issue_compensation", 8, ["meal_usd", "controllable_delay_min", "controllable_causes"], "passenger_stranded_overnight"),
    ("M08", "Crew rooms vs passenger rooms", "Crews and passengers both need rooms from the same hotels", "before the rooms are booked", NET, "book_crew_hotel", None, ["action_book_crew_hotel", "action_hold_hotel_rooms"], "missing"),
    ("M09", "Accessible rooms for wheelchair users", "A stranded wheelchair user needs a room", "before rooms are booked", CARE, "hold_hotel_rooms", 5, WC, "missing"),
    ("M10", "Release room holds we don't need", "Fewer passengers need rooms than were held", "before the hotel's cancellation cutoff", CARE, "release_hotel_rooms", None, ["action_hold_hotel_rooms"], "missing"),
    # ---------------------------------------------------------------- N
    ("N01", "Controllable or not: what is owed?", "A passenger's flight is cancelled or 3+ h late", "before any offer is made", CARE, "issue_compensation", 8, MONEY, "flight_cancelled"),
    ("N02", "Refund (law)", "A cancelled or significantly delayed passenger declines the rebooking", "pay within 7 business days for cards, 20 days otherwise (refund_timing_days)", CARE, "offer_refund", 8, ["significant_delay_domestic_min", "significant_delay_international_min", "refund_timing_days"], "flight_cancelled"),
    ("N03", "$75 voucher and a meal (Southwest commitment)", "A controllable delay of 3+ h", "before the passenger leaves the airport", CARE, "issue_compensation", 8, ["voucher_usd", "meal_usd", "controllable_delay_min", "controllable_causes"], "missing"),
    ("N04", "Hotel and transport", "A controllable overnight for a passenger who doesn't live locally", "before the hotel's last check-in", CARE, "issue_compensation", 8, ["hotel_needed_if_overnight", "send_locals_home"], "passenger_stranded_overnight"),
    ("N05", "Goodwill credit (a supervisor approves)", "An uncontrollable delay where nothing is owed", "before the passenger leaves the airport", CARE, "issue_compensation", 8, ["goodwill_usd", "uncontrollable_causes"], "flight_delayed"),
    ("N06", "The passenger paid for their own hotel: reimburse?", "A passenger sends a receipt", "within the refund timing (refund_timing_days)", CARE, "issue_compensation", 8, ["hotel_needed_if_overnight", "refund_timing_days"], "missing"),
    ("N07", "The incident's cost passed a cap: escalate, and to whom?", "The running cost passes incident_cost_cap_usd", "within 15 min of passing the cap", ORC, "escalate_cost", None, ["incident_cost_cap_usd"], "missing"),
    ("N08", "Track the cost of each decision", "Any decision is approved", "when it is approved", ORC, "track_cost", None, ["incident_cost_cap_usd"], "missing"),
    # ---------------------------------------------------------------- O
    ("O01", "First notice: what to say, and when", "ground_stop_started", "within 15 min of the stop being announced", COM, "send_status_update", 7, ["status_update_interval_min", "offer_needs_approved_decision"], "ground_stop_started"),
    ("O02", "Which channel (app, text, email, gate screen, call center, PA)?", "Any message to passengers", "with the message", COM, "send_status_update", 7, [], "channel_view_updated"),
    ("O03", "Keep every channel saying the same thing: detect and fix mismatches", "A screen shows something different from the truth", "within 5 min of the mismatch", COM, "fix_channel_mismatch", None, ["message_matches_truth"], "channel_view_updated"),
    ("O04", "Correct wrong information already sent", "A message turned out wrong", "within 5 min of finding it", COM, "fix_channel_mismatch", None, ["message_matches_truth"], "channel_update_failed"),
    ("O05", "Never promise anything that hasn't been approved", "Any draft that mentions money, a hotel, a seat or a time", "before the message is sent", COM, "send_status_update", 8, ["offer_needs_approved_decision", "message_matches_truth"], "missing"),
    ("O06", "Plain language and translation", "Any message to passengers", "with the message", COM, "send_status_update", 7, [], "missing"),
    ("O07", "How often to update during the stop", "A stop is in effect", "every status_update_interval_min", COM, "send_status_update", 7, ["status_update_interval_min"], "ground_stop_extended"),
    ("O08", "Messages and scripts for staff (gate agents, ramp, call center)", "Any change passengers will ask about", "before the passenger message goes out", COM, "send_staff_briefing", 7, ["message_matches_truth"], "missing"),
    ("O09", "Messages to crews", "A crew's flight is delayed, cancelled or reassigned", "within 10 min of the change", COM, "send_crew_message", 7, ["message_matches_truth"], "reserve_crew_assigned"),
    ("O10", "Public or social media statement", "A large or newsworthy disruption", "as soon as practical", COM, "public_statement", 8, [], "missing"),
    ("O11", "A message failed: resend on another channel", "channel_update_failed", "within 5 min of the failure", COM, "send_status_update", 7, [], "channel_update_failed"),
    ("O12", "A passenger with no contact details", "An affected passenger has no app, text or email", "before they reach the gate", COM, "send_status_update", 7, [], "missing"),
    # ---------------------------------------------------------------- P
    ("P01", "Airport authority: gates, terminal space, overnight", "Gates or terminal space short, or people staying overnight", "before the shortage hits", STA, "contact_partner", 3, ["terminal_crowding_alert_share"], "terminal_crowd_growth"),
    ("P02", "FAA / ATC: slots, stop updates", "A stop is in effect", "every 30 min during the stop", SIT, "contact_partner", None, ["edct_compliance_window_min"], "ground_stop_started"),
    ("P03", "TSA: checkpoint hours, re-screening", "Passengers must be re-screened, or need the checkpoint open late", "before the checkpoint closes", STA, "contact_partner", 3, ["tsa_checkpoint_hours", "tsa_rescreen_outside_secure_area"], "missing"),
    ("P04", "Customs: international arrivals and diversions", "An international flight is delayed or diverted", "before it lands", STA, "contact_partner", 3, ["customs_international_arrivals"], "missing"),
    ("P05", "Hotel vendors", "Rooms are needed (M01)", "before the rooms run out", CARE, "contact_partner", 8, ["hotel_needed_if_overnight"], "passenger_stranded_overnight"),
    ("P06", "Ground transport vendors", "Transport is needed (M05)", "before the first group is ready", CARE, "contact_partner", 8, [], "passenger_stranded_overnight"),
    ("P07", "Other airlines: rebooking agreements", "K02 is raised", "before the other airline's flight", CARE, "contact_partner", 8, ["southwest_rebooks_on_other_airlines"], "missing"),
    ("P08", "The Southwest station at the diversion airport", "flight_diverted", "before the diverted plane lands", STA, "contact_partner", 3, ["southwest_station_hours"] + DIV, "flight_diverted"),
    # ---------------------------------------------------------------- Q
    ("Q01", "When is the stop really over for operations (ramp, crews, gates)?", "ground_stop_ended", "within 10 min of the stop ending", ORC, "declare_recovery", 6, LIGHT, "ground_stop_ended"),
    ("Q02", "Departure order after the restart", "ground_stop_ended", "before the first departure after the restart", NET, "set_flight_protection_order", 6, ["airport_curfews"], "ground_stop_ended"),
    ("Q03", "Destination curfews for the late flights", "A delayed flight would arrive after a curfew starts", "before the flight departs", NET, "flag_curfew_risk", 3, ["airport_curfews"], "missing"),
    ("Q04", "Last flights of the day: protect or cancel?", "The last flight to a city is delayed", "before the crew's duty limit minus 30 min", NET, "set_flight_protection_order", 6, ["crew_duty_buffer_min"], "flight_delayed"),
    ("Q05", "How many passengers are stranded overnight?", "passenger_stranded_overnight", "before 19:00", SIT, "assess_situation", None, [], "passenger_stranded_overnight"),
    ("Q06", "Is tomorrow's first wave legal (crews, aircraft positions)?", "Evening, after the last delayed flight", "before 22:00", NET, "flag_crew_for_tomorrow", 4, ["pilot_min_rest_hours", "pilot_sleep_opportunity_hours"], "missing"),
    ("Q07", "Close the incident; delete temporary data, keep the audit log", "All flights recovered and no proposal pending", "within 1 h of the last recovery flight", ORC, "close_incident", None, [], "missing"),
    ("Q08", "After-action report", "The incident is closed", "within 7 days (assumption)", ORC, "write_after_action_report", None, [], "missing"),
    # ---------------------------------------------------------------- R
    ("R01", "DOT tarmac report within 30 days", "A tarmac delay over the limit", "within 30 days (tarmac_dot_report_days)", ORC, "file_dot_tarmac_report", None, ["tarmac_dot_report_days"], "missing"),
    ("R02", "FAA duty extension report within 10 days", "A pilot duty extension of more than 30 min (pilot_duty_extended)", "within 10 days (pilot_fdp_extension_report_days)", NET, "prepare_faa_extension_report", None, ["pilot_fdp_extension_report_days"], "pilot_duty_extended"),
    ("R03", "Disability complaints", "A written disability complaint", "answer within 30 days (disability_complaint_response_days)", CARE, "handle_disability_complaint", 5, ["disability_complaint_response_days", "wheelchair_unattended_max_min"], "missing"),
    ("R04", "Refund timing (7 business days for card payments)", "A refund is approved", "within 7 business days for cards, 20 days otherwise (refund_timing_days)", CARE, "offer_refund", 8, ["refund_timing_days"], "missing"),
    ("R05", "Record keeping (audit log)", "Any decision, block or override", "when it happens", ORC, "keep_records", None, ["override_only_by_named_human"], "missing"),
    # ---------------------------------------------------------------- S
    ("S01", "A system is down (IT outage): switch to the manual fallback?", "A system's status is down", "within 5 min of the outage", SIT, "switch_to_manual_fallback", None, ["missing_or_stale_data_blocks"], "missing"),
    ("S02", "Data is stale: refuse to recommend", "Data older than an agent's max_data_age_sec", "before any recommendation uses it", ORC, "flag_stale_data", None, ["missing_or_stale_data_blocks"], "missing"),
    ("S03", "Sources conflict: escalate", "Two sources disagree and the rules can't settle it", "before any recommendation uses the fact", ORC, "resolve_source_conflict", None, ["missing_or_stale_data_blocks"], "missing"),
    ("S04", "AI is unavailable: use the templates", "The language model is slow, down or its text fails the fact check", "immediately", COM, "use_message_template", None, ["message_matches_truth"], "missing"),
    ("S05", "Too many recommendations: what do humans see first?", "More pending recommendations than a person can review", "every time the list changes", ORC, "rank_for_humans", None, [], "missing"),
    ("S06", "A human didn't answer before the deadline: escalate to the next role", "A decision passes human_answer_deadline_min without an answer", "at the deadline (human_answer_deadline_min)", ORC, "escalate_to_next_role", None, ["human_answer_deadline_min"], "missing"),
    # ---------------------------------------------------------------- T
    ("T01", "Security threat: evacuate the terminal or a concourse?", "Police or TSA report a threat", "immediately", SIT, "evacuate_terminal", 1, [], "missing"),
    ("T02", "Checkpoint breach: re-screen passengers?", "TSA reports a checkpoint breach", "immediately", STA, "request_rescreening", 3, ["tsa_rescreen_outside_secure_area"], "missing"),
    ("T03", "Power outage at the airport", "The airport reports a power failure", "within 5 min", SIT, "switch_to_manual_fallback", 3, ["missing_or_stale_data_blocks"], "missing"),
    ("T04", "Runway closure", "The airport closes a runway (airport cause)", "within 10 min", SIT, "replan_after_program_change", 6, [], "missing"),
    ("T05", "Two incidents at once: which first?", "A second incident opens while one is open", "within 5 min of the second one", ORC, "set_incident_priority", None, [], "missing"),
    # ---------------------------------------------------------------- extra decisions the catalog needed
    ("A11", "Is a passenger message needed for this change at all?", "Any change to a passenger's flight or status", "within 5 min of the change", COM, "send_status_update", 7, [], "flight_delay_updated"),
    ("B15", "A flight was cancelled: what happens to its aircraft and crew?", "flight_cancelled", "before the aircraft's and crew's next flight", NET, "swap_aircraft", 4, ["no_double_assignment", "crew_duty_buffer_min"], "flight_cancelled"),
    ("D14", "A crew in the class data has rest below the legal minimum: flag before it flies", "crew_illegal_rest at 05:00", "before the crew's first departure", NET, "flag_crew_legality", 4, ["pilot_min_rest_hours", "pilot_sleep_opportunity_hours"], "crew_illegal_rest"),
    ("D15", "A crew is short only for our planning estimate: watch it", "crew_short_for_plan at 05:00", "before the crew's first delay is approved", NET, "flag_crew_legality", 4, ["crew_duty_buffer_min"], "crew_short_for_plan"),
    ("F11", "A tarmac delay is building at a diversion airport: when does the clock start?", "A diverted flight lands (flight_diverted)", "at touchdown (tarmac_clock_start)", STA, "arrange_diversion_deplaning", 1, ["tarmac_clock_start", "tarmac_domestic_min", "customs_international_arrivals"], "flight_diverted"),
    ("F12", "Lightning stops pushback or towing while planes wait", "ramp_closed with planes waiting to push back", "immediately", STA, "flag_ramp_service_impact", 1, LIGHT + ["tarmac_food_water_min"], "ramp_closed"),
    ("G11", "Passengers stranded overnight who live locally: go home and come back?", "passenger_stranded_overnight for someone who lives locally", "before transport stops for the night", CARE, "arrange_ground_transport", 8, ["send_locals_home"], "passenger_stranded_overnight"),
    ("I07", "A passenger will miss a connection and has a child or wheelchair need", "missed_connection for a vulnerable passenger", "before the next flight closes boarding", CARE, "rebook_passenger", 5, KID + WC, "missed_connection"),
    ("J11", "A child flying alone is on a diverted flight", "flight_diverted with a child flying alone on board", "before the plane reaches the gate", STA, "supervise_child_alone", 5, KID, "flight_diverted"),
    ("K11", "Rebook passengers stranded overnight onto tomorrow's flights", "next_day_flight_created", "before 22:00", CARE, "rebook_passenger", 5, ["seat_priority_order"] + KID, "next_day_flight_created"),
    ("M11", "Children flying alone stranded overnight: staff stays with them, no hotel alone", "passenger_stranded_overnight for a child flying alone", "before 21:00", CARE, "supervise_child_alone", 5, KID, "passenger_stranded_overnight"),
    ("O13", "Tell passengers on a diverted flight what happens next", "flight_diverted", "within 15 min of landing at the diversion airport", COM, "send_status_update", 7, ["message_matches_truth"], "flight_diverted"),
    ("O14", "Tell passengers their flight is cancelled and what they are owed", "flight_cancelled", "within 10 min of the cancellation", COM, "send_status_update", 8, ["offer_needs_approved_decision", "message_matches_truth"], "flight_cancelled"),
    ("Q09", "Flights resuming from diversion airports: in what order?", "flight_resumed", "before the first diverted flight leaves", NET, "set_flight_protection_order", 6, ["fuel_legal_reserve"], "flight_resumed"),
    ("S07", "An agent's proposal broke its contract: what now?", "A contract_violation event", "immediately", ORC, "escalate_to_next_role", None, ["missing_or_stale_data_blocks"], "missing"),
    ("S08", "A block from the rule check: show it to the named human", "The rule check blocks a proposal", "before the proposal's own deadline", ORC, "escalate_to_next_role", None, ["override_only_by_named_human", "proposal_conflicts_block", "no_double_assignment"], "missing"),
]

# The money and safety rules that a decision uses only through the rule check, listed here so every enforced rule is covered.
EXTRA_RULES = {
    "B03": ["unlisted_action_is_human_only", "never_auto_rungs"],
    "C01": ["needs_inspection_check_note"],
    "F07": ["tarmac_lavatories_medical"],
}


# Who owns (and so approves) each decision that needs approval. Human-only decisions keep the role in autonomy.yaml.
OWNER_BY_FAMILY = {"B": "network_operations_control", "C": "maintenance_control", "D": "crew_scheduling", "E": "station_manager",
                   "F": "station_manager", "G": "station_manager", "K": "customer_service_supervisor", "M": "customer_service_supervisor",
                   "N": "customer_service_supervisor", "J": "customer_service_supervisor", "O": "customer_service_supervisor"}
OWNER = {
    # ramp work at MCI belongs to the ramp supervisor
    "E08": "ramp_supervisor", "E09": "ramp_supervisor", "F12": "ramp_supervisor",
    # deplaning help in the terminal / at the gate
    "F09": "station_manager", "J04": "station_manager", "J08": "station_manager",
    # restart order and last flights
    "Q02": "network_operations_control", "Q04": "network_operations_control", "Q06": "crew_scheduling",
    # the DOT tarmac report
    "F10": "regulatory_compliance",
    # partners: the role that owns each relationship
    "P01": "station_manager", "P02": "network_operations_control", "P03": "station_manager", "P04": "station_manager",
    "P05": "customer_service_supervisor", "P06": "customer_service_supervisor", "P07": "customer_service_supervisor",
    "P08": "network_operations_control",
    # the rest of A, H, I, L, Q, R, S, T: by subject
    "A01": "network_operations_control", "A02": "network_operations_control", "A03": "customer_service_supervisor",
    "A04": "network_operations_control", "A05": "network_operations_control", "A06": "network_operations_control",
    "A07": "network_operations_control", "A08": "network_operations_control", "A09": "network_operations_control",
    "A10": "network_operations_control", "A11": "customer_service_supervisor",
    "H01": "customer_service_supervisor", "H02": "customer_service_supervisor", "H03": "station_manager",
    "H04": "customer_service_supervisor", "H05": "customer_service_supervisor", "H06": "customer_service_supervisor",
    "I01": "customer_service_supervisor", "I02": "network_operations_control", "I03": "customer_service_supervisor",
    "I04": "network_operations_control", "I05": "ramp_supervisor", "I06": "customer_service_supervisor", "I07": "customer_service_supervisor",
    "L01": "ramp_supervisor", "L02": "ramp_supervisor", "L03": "ramp_supervisor", "L04": "customer_service_supervisor",
    "L05": "customer_service_supervisor", "L06": "customer_service_supervisor", "L07": "customer_service_supervisor",
    "L08": "ramp_supervisor",
    "Q01": "station_manager", "Q03": "network_operations_control", "Q05": "customer_service_supervisor", "Q07": "station_manager",
    "Q08": "station_manager", "Q09": "network_operations_control",
    "R01": "regulatory_compliance", "R02": "crew_scheduling", "R03": "customer_service_supervisor", "R04": "customer_service_supervisor",
    "R05": "regulatory_compliance",
    "S01": "station_manager", "S02": "network_operations_control", "S03": "network_operations_control",
    "S04": "customer_service_supervisor", "S05": "network_operations_control", "S06": "network_operations_control",
    "S07": "network_operations_control", "S08": "network_operations_control",
    "T02": "station_manager", "T03": "station_manager", "T04": "network_operations_control", "T05": "station_manager",
}


def owner_for(did: str) -> str | None:
    return OWNER.get(did) or OWNER_BY_FAMILY.get(did[0])


def build():
    rules = load_rules()
    known = {e["id"] for e in rules["entries"]}
    added = [e["id"] for e in rules["entries"] if e["source"] == "assumption" and e["id"] in {
        "incident_cost_cap_usd", "human_answer_deadline_min", "seat_priority_order", "loyalty_priority_fairness", "keep_families_together",
        "status_update_interval_min", "terminal_crowding_alert_share", "send_locals_home", "terminal_care_after_min", "poc_battery_check",
        "travel_waiver_after_min", "crew_availability", "missing_or_stale_data_blocks", "proposal_conflicts_block", "message_matches_truth",
        "offer_needs_approved_decision", "no_double_assignment", "override_only_by_named_human", "unlisted_action_is_human_only"}]
    story = story_events()
    out = []
    for did, q, trig, dl, agent, action, rung, rule_ids, ev in D:
        fam = did[0]
        family, data, affects = FAMILIES[fam]
        ids = list(dict.fromkeys(rule_ids + [r for r in EXTRA_RULES.get(did, []) if r in known] + [f"action_{action}"]))
        ids = [r for r in ids if r in known]
        level = autonomy_for(action, rules, safety_rung=rung, owner=owner_for(did))
        decided = {"level": level["level"]}
        if level["level"] != "auto":
            decided["role"] = level["approver"]
        ladder = [f"rung_{n}_{name}" for n, name in [(1, "life"), (2, "aircraft"), (3, "infrastructure"), (4, "legal"), (5, "vulnerable"),
                                                         (6, "network"), (7, "convenience"), (8, "cost")] if n == rung]
        out.append({
            "id": did, "family": family, "question": q, "trigger": trig, "deadline": dl, "proposed_by": agent, "action": action,
            "decided_by": decided, "checked_by": ["orchestrator rule check"] + ladder + ids,
            "safety_rung": rung if rung is not None else "none", "rules": ids, "data_needed": data, "affects": affects,
            "in_story": ev != "missing" and ev in story, "simulator_event": ev, "status": "defined",
            **({"also_raised_by": ALSO_RAISED_BY[did]} if did in ALSO_RAISED_BY else {}),
        })
    enforced = [e["id"] for e in rules["entries"] if e["enforced"]]
    used = {r for d in out for r in d["rules"]} | {c for d in out for c in d["checked_by"]}
    unused = [r for r in enforced if r not in used]
    return out, added, unused


# A decision another agent may also raise (Phase 4b-3): Communication asks A09 when a conflict holds passenger corrections.
ALSO_RAISED_BY = {"A09": ["communication"]}


def story_events() -> set[str]:
    from backend.seed.build import build as seed
    from simulator.run import simulate
    return {e["type"] for e in simulate(7, "a31_storm", data=seed()).world.events}


def main():
    out, added, unused = build()
    if unused:
        raise SystemExit(f"enforced rules no decision uses: {unused}")
    doc = {
        "version": 1,
        "about": ("Every decision the co-pilot may raise during a ground stop. proposed_by = the agent that raises it; decided_by = who decides "
                  "(from config/rules/autonomy.yaml, with rungs 1-4 never automatic); checked_by = the orchestrator's rule check plus the rules it runs. "
                  "Built by scripts/build_decisions.py; edit the table there and rerun."),
        "decisions": out,
        "gaps": {"rules_added_as_assumptions": added,
                 "decisions_the_simulator_cannot_trigger": [d["id"] for d in out if d["simulator_event"] == "missing"]},
    }
    OUT.write_text("# The decision catalog. Generated by `python -m scripts.build_decisions`.\n"
                   + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=140), encoding="utf-8")
    print(f"wrote {OUT} with {len(out)} decisions")


if __name__ == "__main__":
    main()
