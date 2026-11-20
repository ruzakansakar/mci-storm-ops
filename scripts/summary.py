"""Print a plain-English check of what's in the database.

Run:  python -m scripts.summary
"""
from backend.database import connect

CHECKS = [
    ("Departures from MCI (the class schedule)", "SELECT count(*) FROM flights WHERE direction='departure'", 67),
    ("Inbound flights into MCI (generated)", "SELECT count(*) FROM flights WHERE direction='arrival'", 56),
    ("Aircraft", "SELECT count(*) FROM aircraft", 67),
    ("Flights whose passenger groups don't add up",
     "SELECT count(*) FROM (SELECT f.id FROM flights f JOIN passenger_groups g ON g.flight_id=f.id "
     "GROUP BY f.id, f.pax_total HAVING sum(g.count) <> f.pax_total) x", 0),
    ("Passengers with no booking",
     "SELECT count(*) FROM passengers p WHERE NOT EXISTS (SELECT 1 FROM bookings b WHERE b.passenger_id=p.id)", 0),
    ("Connections shorter than 45 minutes",
     "SELECT count(*) FROM bookings b1 JOIN bookings b2 ON b2.passenger_id=b1.passenger_id AND b2.seq=b1.seq+1 "
     "JOIN flights f1 ON f1.id=b1.flight_id JOIN flights f2 ON f2.id=b2.flight_id "
     "WHERE f2.sched_dep - f1.sched_arr < interval '45 minutes'", 0),
    ("Bags whose routing doesn't match the passenger's bookings",
     "SELECT count(*) FROM bags g WHERE g.routing <> (SELECT array_agg(flight_id ORDER BY seq) FROM bookings b WHERE b.passenger_id=g.passenger_id)", 0),
    ("Screens that disagree with the truth right now",
     "SELECT count(*) FROM channel_views v WHERE v.shown_itinerary <> (SELECT array_agg(flight_id ORDER BY seq) FROM bookings b WHERE b.passenger_id=v.passenger_id)", 0),
]


def main():
    with connect() as conn, conn.cursor() as cur:
        print("\nHEALTH CHECKS")
        ok_all = True
        for label, sql, expected in CHECKS:
            cur.execute(sql)
            n = cur.fetchone()[0]
            ok = n == expected
            ok_all &= ok
            print(f"  [{'ok' if ok else 'XX'}] {label}: {n}" + ("" if ok else f" (expected {expected})"))

        print("\nTHE STORY FLIGHT: A31 from Denver")
        cur.execute("SELECT f.id, f.origin, f.destination, to_char(f.sched_arr AT TIME ZONE 'America/Chicago','HH24:MI'), "
                    "f.pax_total, c.pilot_duty_left_min FROM flights f LEFT JOIN crews c ON c.id=f.crew_id WHERE f.id='WN2031'")
        fid, o, d, arr, pax, duty = cur.fetchone()
        cur.execute("SELECT segment, count FROM passenger_groups WHERE flight_id='WN2031' ORDER BY count DESC")
        segs = ", ".join(f"{s} {n}" for s, n in cur.fetchall() if n)
        print(f"  {fid} {o} -> {d}, lands {arr}, {pax} passengers ({segs})")
        print(f"  Crew duty left: {duty // 60} h {duty % 60} min")
        cur.execute("SELECT p.id, p.full_name, array_agg(b.flight_id || ' to ' || f.destination ORDER BY b.seq) "
                    "FROM passengers p JOIN bookings b ON b.passenger_id=p.id JOIN flights f ON f.id=b.flight_id "
                    "WHERE p.full_name='Maya Torres' GROUP BY p.id, p.full_name")
        pid, name, itin = cur.fetchone()
        print(f"  Example passenger: {name} ({pid}): {' then '.join(itin)}")

        print("\nROWS PER TABLE")
        cur.execute("SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname")
        for t, _ in cur.fetchall():
            cur.execute(f"SELECT count(*) FROM {t}")
            print(f"  {t:<18} {cur.fetchone()[0]:>5}")

        print("\nDATA ISSUES FOUND IN THE CLASS FILES (flagged, not silently fixed)")
        cur.execute("SELECT category, severity, count(*), min(issue), min(resolution) FROM data_issues "
                    "GROUP BY category, severity ORDER BY severity DESC, count(*) DESC")
        for cat, sev, n, example, res in cur.fetchall():
            print(f"  {sev:<7} {cat} x{n}\n          e.g. {example}\n          -> {res}")

        print("\n" + ("All checks passed." if ok_all else "Some checks FAILED - see XX above."))


if __name__ == "__main__":
    main()
