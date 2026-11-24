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
    def __init__(self, conn, run_id: str, seed: int, persist_truth=True):
        self.conn, self.run_id, self.seed = conn, run_id, seed
        self.incident_event_ids = []
        self.persist_truth = persist_truth

    # ------------------------------------------------------------ one batch
    def flush(self, world, channels):
        ev, au = world.drain()
        with self.conn.cursor() as cur:
            if self.persist_truth:
                self._new_flights(cur, world)
            rows = []
            # Event ids come from the database's own counter, reserved for this batch. So the counter is always right after
            # every batch, even if a run crashes halfway, and anyone else inserting events (an agent) never collides.
            ids = []
            if ev:
                cur.execute("SELECT nextval(pg_get_serial_sequence('events', 'id')) FROM generate_series(1, %s)", (len(ev),))
                ids = [r[0] for r in cur.fetchall()]
            for e, eid in zip(ev, ids):
                if e["incident"]:
                    self.incident_event_ids.append(eid)
                rows.append((eid, at(e["t"]), e["type"], e["actor"], e["actor_name"], e["station"],
                             Jsonb(e["payload"]), self.seed, self.run_id, self.published_at(e)))
            if rows:
                cur.executemany("INSERT INTO events (id, occurred_at, type, actor, actor_name, station, payload, seed, run_id, "
                                "published_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
            if au:
                cur.executemany(
                    "INSERT INTO audit_log (at, actor, actor_name, what, entity_type, entity_id, before, after, run_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(at(a["t"]), a["actor"], a["actor_name"], a["what"], a["entity_type"], a["entity_id"],
                      Jsonb(a["before"]), Jsonb(a["after"]), self.run_id) for a in au])
            if self.persist_truth:
                self._state(cur, world)
                self._views(cur, channels)          # channel_views holds TODAY's screens; a co-pilot run keeps them (its own
            else:                                   # screens live in its channel events and metrics, under its own run id)
                channels.touched = set()
        self.conn.commit()

    def published_at(self, e):
        """When the source delivered this event, if late (None = as it happened). Chaos tests deliver some events late."""
        return at(e["published_t"]) if e.get("published_t") is not None else None

    def save_proposals(self, context):
        """Persist buffered, contract-validated proposals in one batch.

        Execution event IDs are run-local proposal IDs; options preserve that
        key so they can be joined to the database's independently assigned ID.
        """
        from backend.execution.copilot import serial
        rows = []
        for (table, local_id), r in context.saved.items():
            if table != "recommendations":
                continue
            options = [{**o, "local_proposal_id": local_id} for o in r["options"]]
            rows.append((r["agent"], r["title"], r["why"], Jsonb(serial(options)), r["autonomy"], r["approver_role"],
                         r["safety_rung"], r["decision_ids"], r["data_as_of"], r["status"], self.run_id, r["created_at"],
                         r.get("rule_check_ok"), r.get("rule_check_notes"), r.get("rule_ids")))
        if rows:
            with self.conn.cursor() as cur:
                cur.executemany("INSERT INTO recommendations (agent,title,why,options,autonomy,approver_role,safety_rung,"
                                "decision_ids,data_as_of,status,run_id,created_at,rule_check_ok,rule_check_notes,rule_ids) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)

    def save_shared(self, store):
        """The shared records agents wrote in a co-pilot run (append-only), under this run. Situation's versions get their own
        agent run id, '<run id>:situation', so they never mix with a standalone replay of the today run."""
        from backend.contracts.shared_state import SHARED
        jsonish = {"flight_controllability", "impact", "conflicts", "stale_sources", "evidence", "delays", "cancels", "crew_actions",
                   "hold_divert", "aircraft_recovery", "restart_order", "seats_by_destination", "warnings", "estimates", "sources",
                   "passengers", "hotels", "costs", "vulnerable", "wheelchair_requests", "rebooking_advice"}
        for table, meta in SHARED.items():
            rows = store.records.get(table) or []
            if not rows:
                continue
            cols = [c for c in rows[0] if c not in ("agent_run_id", "run_id")]
            with self.conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} (agent_run_id, run_id, {', '.join(cols)}) "
                    f"VALUES (%s, %s, {', '.join(['%s'] * len(cols))})",
                    [[f"{self.run_id}:{meta['producer']}", self.run_id] + [Jsonb(r[c]) if c in jsonish else r[c] for c in cols]
                     for r in rows])

    def save_hotel_ledger(self, ledger):
        """The rooms the co-pilot actually held, assigned and released (its own table; never a truth table)."""
        if not ledger:
            return
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO hotel_assignments (run_id, at, kind, hotel_id, rooms, passenger_id, accessible, "
                            "recommendation, approved_by, waited_min) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            [(self.run_id, r["run_at"], r["kind"], r["hotel_id"], r["rooms"], r["passenger_id"], r["accessible"],
                              r["recommendation"], r["approved_by"], r["waited_min"]) for r in ledger])

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
        self.conn.commit()
        return incident_id
