"""Saves the run to PostgreSQL in batches.

The world and the channels live in memory during the run. Every so many simulated minutes (and at the end)
everything that changed is written with a few executemany calls, so a whole day needs only a handful of
round-trips to the (cloud) database.

events and audit_log are append-only: rows are only ever inserted here, never updated or deleted.
"""
from psycopg.types.json import Jsonb

from simulator.clock import at


def _t(v):
    return at(v) if isinstance(v, int) else v


class Writer:
    def __init__(self, conn, run_id: str, seed: int):
        self.conn, self.run_id, self.seed = conn, run_id, seed
        self.next_event_id = 1
        self.incident_event_ids = []

    # ------------------------------------------------------------ one batch
    def flush(self, world, channels):
        ev, au = world.drain()
        with self.conn.cursor() as cur:
            self._new_flights(cur, world)
            rows = []
            for e in ev:
                eid = self.next_event_id
                self.next_event_id += 1
                if e["incident"]:
                    self.incident_event_ids.append(eid)
                rows.append((eid, at(e["t"]), e["type"], e["actor"], e["actor_name"], e["station"],
                             Jsonb(e["payload"]), self.seed, self.run_id))
            if rows:
                cur.executemany("INSERT INTO events (id, occurred_at, type, actor, actor_name, station, payload, seed, run_id) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
            if au:
                cur.executemany(
                    "INSERT INTO audit_log (at, actor, actor_name, what, entity_type, entity_id, before, after, run_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(at(a["t"]), a["actor"], a["actor_name"], a["what"], a["entity_type"], a["entity_id"],
                      Jsonb(a["before"]), Jsonb(a["after"]), self.run_id) for a in au])
            self._state(cur, world)
            self._views(cur, channels)
        self.conn.commit()

    def _new_flights(self, cur, world):
        """Next-day flights the simulator had to create (INSERT only, before anything refers to them)."""
        ids, world.new_flights = world.new_flights, []
        if not ids:
            return
        from simulator.world import SEG_ORDER
        cur.executemany(
            "INSERT INTO flights (id, direction, schedule_no, origin, destination, sched_dep, sched_arr, est_dep, est_arr, "
            "priority, status, pax_total, source, system_of_record, as_of) VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,'scheduled',0,"
            "'simulator','Amadeus Altea (simulated)',%s)",
            [(f["id"], f["direction"], f["origin"], f["destination"], at(f["sched_dep"]), at(f["sched_arr"]),
              at(f["est_dep"]), at(f["est_arr"]), f["priority"], at(world.t))
             for f in (world.flights[i] for i in ids)])
        cur.executemany(
            "INSERT INTO passenger_groups (flight_id, segment, count, rebooked, refunded, need_hotel, source, system_of_record, as_of) "
            "VALUES (%s,%s,0,0,0,0,'simulator','Amadeus Altea (simulated)',%s)",
            [(i, seg, at(world.t)) for i in ids for seg in SEG_ORDER])

    def _take(self, world, table):
        keys, world.dirty[table] = sorted(world.dirty[table]), set()
        return keys

    def _state(self, cur, world):
        k = self._take(world, "flights")
        if k:
            cur.executemany(
                "UPDATE flights SET status=%s, est_dep=%s, est_arr=%s, edct=%s, tarmac_since=%s, cancel_reason=%s, "
                "controllable=%s, diverted_to=%s, pax_total=%s, reserve_crew_ids=%s, as_of=%s WHERE id=%s",
                [(f["status"], _t(f["est_dep"]), _t(f["est_arr"]), _t(f["edct"]), _t(f["tarmac_since"]), f["cancel_reason"],
                  f["controllable"], f["diverted_to"], f["pax_total"], f.get("reserve_crew_ids"), _t(f["as_of"]), f["id"])
                 for f in (world.flights[i] for i in k)])
        k = self._take(world, "aircraft")
        if k:
            cur.executemany(
                "UPDATE aircraft SET airborne=%s, current_station=%s, fuel_minutes_extra=%s, needs_inspection=%s, as_of=%s WHERE id=%s",
                [(a["airborne"], a["current_station"], a["fuel_minutes_extra"], a["needs_inspection"], _t(a["as_of"]), a["id"])
                 for a in (world.aircraft[i] for i in k)])
        k = self._take(world, "crews")
        if k:
            cur.executemany("UPDATE crews SET pilot_duty_left_min=%s, as_of=%s WHERE id=%s",
                            [(c["pilot_duty_left_min"], _t(c["as_of"]), c["id"]) for c in (world.crews[i] for i in k)])
        k = self._take(world, "stations")
        if k:
            cur.executemany("UPDATE stations SET ramp_open=%s, people_in_terminal=%s, as_of=%s WHERE code=%s",
                            [(s["ramp_open"], s["people_in_terminal"], _t(s["as_of"]), s["code"])
                             for s in (world.stations[i] for i in k)])
        k = self._take(world, "bags")
        if k:
            cur.executemany("UPDATE bags SET status=%s, routing=%s, current_location=%s, as_of=%s WHERE tag=%s",
                            [(b["status"], b["routing"], b["current_location"], _t(b["as_of"]), b["tag"])
                             for b in (world.bags[i] for i in k)])
        k = self._take(world, "bookings")
        if k:
            rows = []
            for pid, seq in k:
                b = next(x for x in world.bookings[pid] if x["seq"] == seq)
                rows.append((b["flight_id"], b["status"], _t(b["as_of"]), pid, seq))
            cur.executemany("UPDATE bookings SET flight_id=%s, status=%s, as_of=%s WHERE passenger_id=%s AND seq=%s", rows)
        k = self._take(world, "passengers")
        if k:
            cur.executemany("UPDATE passengers SET overnight=%s, as_of=%s WHERE id=%s",
                            [(p["overnight"], _t(world.t), p["id"]) for p in (world.passengers[i] for i in k)])
        k = self._take(world, "passenger_groups")
        if k:
            cur.executemany("UPDATE passenger_groups SET count=%s, rebooked=%s, as_of=%s WHERE flight_id=%s AND segment=%s",
                            [(g["count"], g["rebooked"], _t(g["as_of"]), fid, seg)
                             for fid, seg in k for g in [world.groups[(fid, seg)]]])

    def _views(self, cur, channels):
        pairs, channels.touched = channels.touched, set()
        if not pairs:
            return
        cur.executemany(
            "INSERT INTO channel_views (passenger_id, channel, shown_itinerary, shown_status, shown_destination, "
            "shown_bag_routing, message, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (passenger_id, channel) DO UPDATE SET shown_itinerary=EXCLUDED.shown_itinerary, "
            "shown_status=EXCLUDED.shown_status, shown_destination=EXCLUDED.shown_destination, "
            "shown_bag_routing=EXCLUDED.shown_bag_routing, message=EXCLUDED.message, updated_at=EXCLUDED.updated_at",
            [(pid, ch, list(v["itinerary"]), v["status"], v["destination"], list(v["bag_routing"]), v["message"], at(v["updated_at"]))
             for pid, ch, v in channels.snapshot_rows(pairs)])

    # ------------------------------------------------------------ end of run
    def finish(self, scenario, metrics: dict, mode: str, until: int):
        inc, ended = scenario.incident, until >= scenario.end
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incidents (kind, station, title, likely_cause, controllable, status, expected_end_min, "
                "expected_end_max, opened_at, resolved_at, opened_by, run_id) "
                "VALUES (%s,'MCI',%s,%s,%s,%s,%s,%s,%s,%s,'simulator',%s) RETURNING id",
                (inc["kind"], inc["title"], inc["likely_cause"], inc["controllable"], "resolved" if ended else "open",
                 at(scenario.announced_end), at(scenario.end), at(scenario.start), at(scenario.end) if ended else None,
                 self.run_id))
            incident_id = cur.fetchone()[0]
            if self.incident_event_ids:
                cur.executemany("INSERT INTO incident_events (incident_id, event_id) VALUES (%s,%s)",
                                [(incident_id, e) for e in self.incident_event_ids])
            cur.execute("INSERT INTO metric_runs (run_id, seed, mode, scenario, metrics) VALUES (%s,%s,%s,%s,%s)",
                        (self.run_id, self.seed, mode, scenario.name, Jsonb(metrics)))
            cur.execute("SELECT setval(pg_get_serial_sequence('events', 'id'), %s)", (max(1, self.next_event_id - 1),))
        self.conn.commit()
        return incident_id
