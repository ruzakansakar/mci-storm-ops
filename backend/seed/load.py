"""Write the built rows into PostgreSQL, in an order that respects the foreign keys."""
from psycopg.types.json import Jsonb  # noqa: F401  (kept for later phases)

from backend.seed.build import AS_OF, SOR

# table name -> (key in the built data, which official system owns it, or None for no tracking columns)
ORDER = [
    ("stations", "stations", "stations"),
    ("aircraft", "aircraft", "aircraft"),
    ("crews", "crews", "crews"),
    ("flights", "flights", "flights"),
    ("passenger_groups", "passenger_groups", "passengers"),
    ("passengers", "passengers", "passengers"),
    ("bookings", "bookings", "passengers"),
    ("bags", "bags", "bags"),
    ("channel_views", "channel_views", None),
    ("hotels", "hotels", "hotels"),
    ("staff", "staff", "staff"),
    ("later_capacity", "later_capacity", "capacity"),
    ("route_tiers", "route_tiers", None),
    ("transport_options", "transport", None),
    ("hotel_rate_tiers", "hotel_tiers", None),
    ("data_issues", "data_issues", None),
]

RENAME = {
    "transport_options": {"hourly": "hourly_rate_usd", "per_trip": "per_trip_rate_usd"},
    "hotel_rate_tiers": {"radius": "radius_miles", "count": "hotel_count", "rate": "rate_usd", "for": "for_travelers"},
}
EXTRA = {"transport_options": {"source": "class_transport"}, "hotel_rate_tiers": {"source": "class_transport"}}


def load(conn, data: dict) -> dict:
    counts = {}
    with conn.cursor() as cur:
        for table, key, sor in ORDER:
            rows = []
            for r in data[key]:
                r = {RENAME.get(table, {}).get(k, k): v for k, v in r.items()}
                r = {**EXTRA.get(table, {}), **r}
                if sor:
                    r.setdefault("system_of_record", SOR[sor])
                    r.setdefault("as_of", AS_OF)
                rows.append(r)
            if rows:
                cols = list(rows[0])
                sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
                cur.executemany(sql, [[r[c] for c in cols] for r in rows])
            counts[table] = len(rows)
            if table == "crews":       # every crew starts on duty; reserve crews start as reserve (sick / fatigued / on_leave come later)
                cur.execute("UPDATE crews SET availability = 'reserve' WHERE is_reserve")
        systems = sorted(set(SOR.values()))
        cur.executemany(
            "INSERT INTO system_health (system, status, last_heartbeat, data_age_sec, note) "
            "VALUES (%s, 'up', %s, 0, 'simulated stand-in for the official system')",
            [(s, AS_OF) for s in systems],
        )
    return counts
