"""Phase 1 checks: is the shared live view loaded correctly from the class files?"""
import hashlib
import json

from backend.seed.build import build
from tests.conftest import one

TRACKED = ["stations", "aircraft", "crews", "flights", "passenger_groups", "passengers",
           "bookings", "bags", "hotels", "staff", "later_capacity"]


def test_all_67_departures_loaded(db):
    assert one(db, "SELECT count(*) FROM flights WHERE direction='departure' AND origin='MCI'") == 67


def test_inbound_flights_for_aircraft_not_overnighting(db):
    assert one(db, "SELECT count(*) FROM flights WHERE direction='arrival' AND destination='MCI'") == 56


def test_a31_story_numbers_match_the_slides(db):
    db.execute("SELECT origin, pax_total, crew_id FROM flights WHERE id='WN2031'")
    assert db.fetchone() == ("DEN", 158, "CX31")
    assert one(db, "SELECT count FROM passenger_groups WHERE flight_id='WN2031' AND segment='connecting'") == 42
    assert one(db, "SELECT count FROM passenger_groups WHERE flight_id='WN2031' AND segment='wheelchair'") == 3
    assert one(db, "SELECT pilot_duty_left_min FROM crews WHERE id='CX31'") == 260


def test_passenger_groups_add_up_to_each_flight(db):
    assert one(db, """SELECT count(*) FROM (SELECT f.id FROM flights f JOIN passenger_groups g ON g.flight_id=f.id
                      GROUP BY f.id, f.pax_total HAVING sum(g.count) <> f.pax_total) x""") == 0


def test_every_passenger_has_bookings_on_real_flights(db):
    assert one(db, "SELECT count(*) FROM passengers p WHERE NOT EXISTS (SELECT 1 FROM bookings b WHERE b.passenger_id=p.id)") == 0
    assert one(db, "SELECT count(*) FROM bookings b LEFT JOIN flights f ON f.id=b.flight_id WHERE f.id IS NULL") == 0


def test_connections_are_at_least_45_minutes(db):
    assert one(db, """SELECT count(*) FROM bookings b1
                      JOIN bookings b2 ON b2.passenger_id=b1.passenger_id AND b2.seq=b1.seq+1
                      JOIN flights f1 ON f1.id=b1.flight_id JOIN flights f2 ON f2.id=b2.flight_id
                      WHERE f2.sched_dep - f1.sched_arr < interval '45 minutes'""") == 0


def test_bags_follow_their_passenger(db):
    assert one(db, """SELECT count(*) FROM bags g WHERE g.routing <>
                      (SELECT array_agg(flight_id ORDER BY seq) FROM bookings b WHERE b.passenger_id=g.passenger_id)""") == 0


def test_each_passenger_has_only_his_screens_all_matching_at_the_start(db):
    # gate screen and call center for everyone; app and SMS only if they are among the passenger's contact channels (Phase 4c)
    assert one(db, """SELECT count(*) FROM passengers p WHERE
                      (SELECT array_agg(v.channel ORDER BY v.channel) FROM channel_views v WHERE v.passenger_id=p.id) <>
                      (SELECT array_agg(c ORDER BY c) FROM unnest(ARRAY['gate_screen','call_center'] ||
                              ARRAY(SELECT x FROM unnest(p.contact_channels) x WHERE x IN ('app','sms'))) c)""") == 0
    assert one(db, "SELECT count(*) FROM channel_views v JOIN passengers p ON p.id=v.passenger_id "
                   "WHERE v.channel IN ('app','sms') AND NOT v.channel = ANY(p.contact_channels)") == 0
    assert one(db, """SELECT count(*) FROM channel_views v WHERE v.shown_itinerary <>
                      (SELECT array_agg(flight_id ORDER BY seq) FROM bookings b WHERE b.passenger_id=v.passenger_id)""") == 0


def test_every_fact_says_where_it_came_from_and_when(db):
    for t in TRACKED:
        assert one(db, f"SELECT count(*) FROM {t} WHERE source IS NULL OR system_of_record IS NULL OR as_of IS NULL") == 0, t


def test_maya_lands_on_a31_and_connects_to_baltimore(db):
    db.execute("""SELECT array_agg(b.flight_id ORDER BY b.seq), max(p.final_destination) FROM passengers p
                  JOIN bookings b ON b.passenger_id=p.id WHERE p.full_name='Maya Torres'""")
    itinerary, final = db.fetchone()
    assert itinerary[0] == "WN2031" and final == "BWI"


def test_conflicts_in_the_class_files_are_recorded(db):
    cats = {r[0] for r in (db.execute("SELECT DISTINCT category FROM data_issues") or db).fetchall()}
    for expected in ["crew_rest", "child_alone_international", "missing_priority", "tier_typo", "inbound_flights"]:
        assert expected in cats


def test_same_seed_gives_identical_data():
    assert build() == build()


def test_seed_data_is_exactly_what_it_was_before_the_numbers_moved_into_config_seed_yaml():
    """Checksum taken from build() before the hard-coded numbers moved to config/seed.yaml (SEED=42, SAMPLE_PASSENGERS=300)."""
    from backend.settings import SAMPLE_PASSENGERS, SEED
    assert (SEED, SAMPLE_PASSENGERS) == (42, 300), "the checksum is only valid for the default SEED and SAMPLE_PASSENGERS"
    data = build()
    issues = data.pop("data_issues")
    blob = json.dumps(data, sort_keys=True, default=str).encode()
    # Phase 4c changed the data on purpose, in exactly two ways: (1) app/SMS screens only for passengers who have them on file,
    # (2) the story override gives Maya Torres app + SMS (config/seed.yaml story_overrides). New checksum:
    assert hashlib.sha256(blob).hexdigest() == "8df52897fda3545d729ebeea00b82d55fb22f45d2d48b560e3a17050bfeb453c"
    # ...and undoing exactly those two changes gives back the data, byte for byte, from before (steps 1-3): nothing else moved.
    old = json.loads(json.dumps(data, default=str))
    maya = next(p for p in old["passengers"] if p["full_name"] == "Maya Torres")
    maya["contact_channels"] = ["email"]                       # her random draw, before the story override
    by_pid, order = {}, []
    for v in old["channel_views"]:
        if v["passenger_id"] not in by_pid:
            order.append(v["passenger_id"])
        by_pid.setdefault(v["passenger_id"], {})[v["channel"]] = v
    old["channel_views"] = [by_pid[pid].get(ch) or {**by_pid[pid]["gate_screen"], "channel": ch}
                            for pid in order for ch in ("app", "sms", "gate_screen", "call_center")]
    blob = json.dumps(old, sort_keys=True, default=str).encode()
    assert hashlib.sha256(blob).hexdigest() == "7f6e1cd978a43c1c80a868f132a2ed06b51ecc42512de3b6428de5728e456a04"
    # data_issues: same rows, only the crew-rest wording changed in step 3
    assert len(issues) == 266 and sum(i["category"] == "crew_rest" for i in issues) == 35
    assert all("legal minimum rest" in i["issue"] and "crew_illegal_rest" in i["resolution"] for i in issues if i["category"] == "crew_rest")
