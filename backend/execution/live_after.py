"""What a decision did after the person answered (live human-in-the-loop mode): the rest of its flowchart.

  question -> human -> change(s) the operational systems confirmed -> the Communication messages it caused -> one node per
  destination (a passenger screen: app, sms, gate_screen, call_center; or staff: counter, gate_agents, call_center_agents, crew,
  ramp, hotel_desk, ...) with delivered / failed / waiting and the exact words.

Built ONLY from records that exist: the confirmation events (recovery_action_applied, reserve_crew_assigned, crew_reassigned,
flight_delay_updated, flight_cancelled, gate_assigned, staff_assigned, hotel_room_assigned, care and station decision records,
decision_applied / decision_refused, human_decision_recorded), Communication's own drafts and message log, and the delivery
events (message_delivered, message_dropped, channel_update_failed, staff_note_published, crew_message_delivered,
channel_view_updated). Nothing is invented: a destination appears only when a message or a screen update was really sent.
Before the answer, a preview lists what would change and who would be told, from the proposal alone (empty where unknown).
"""
from backend.execution.live_trace import hhmm
from backend.screens import screens_for

CHANNELS = ("app", "sms", "gate_screen", "call_center")
CHANNEL_NAMES = {"app": "App", "sms": "SMS", "gate_screen": "Gate screen", "call_center": "Call center"}
STATION_REC = ("gate_assigned", "gate_released", "staff_assigned", "staff_released", "staff_called_in", "child_supervisor_assigned",
               "ramp_tasks_paused", "station_decision_recorded")
CARE_REC = ("hotel_room_assigned", "hotel_rooms_held", "hotel_rooms_released", "care_decision_recorded")
FLIGHT = ("flight_delayed", "flight_delay_updated", "flight_cancelled", "reserve_crew_assigned", "crew_reassigned",
          "pilot_duty_extended", "flight_departed")
MSG = ("message_delivered", "message_dropped", "channel_update_failed", "staff_note_published", "crew_message_delivered")


class EventIndex:
    """The run's events, indexed as they arrive (only new events are read each time)."""

    def __init__(self):
        self.cursor, self.by = 0, {}

    def add(self, key, e):
        self.by.setdefault(key, []).append(e)

    def update(self, events):
        for e in events[self.cursor:]:
            p, k = e.get("payload") or {}, e["type"]
            if k == "recovery_action_applied" and p.get("proposal_id") is not None:
                self.add(("proposal", str(p["proposal_id"])), e)
            if k in FLIGHT and p.get("flight_id"):
                self.add(("flight", p["flight_id"]), e)
            if k in ("decision_applied", "decision_refused") and p.get("recommendation") is not None:
                self.add(("rec", p.get("agent"), str(p["recommendation"])), e)
            if k in STATION_REC and p.get("recommendation") is not None:
                self.add(("rec", "station_staff", str(p["recommendation"])), e)
            if k in CARE_REC and p.get("recommendation") is not None:
                self.add(("rec", "passenger_care", str(p["recommendation"])), e)
            if k == "human_decision_recorded" and p.get("recommendation") is not None:
                self.add(("rec", p.get("asked_by"), str(p["recommendation"])), e)
            if k in ("incident_updated", "incident_closed"):
                self.add(("incident",), e)
            if p.get("question_id") is not None and str(e.get("actor", "")).startswith(("human:", "system:live", "copilot:")):
                self.add(("question", p["question_id"]), e)
            if k in MSG:
                mid = p.get("message_id") or (p.get("id") if k == "crew_message_delivered" else None)
                if mid:
                    self.add(("msg", mid), e)
            if k in ("channel_view_updated", "channel_update_failed") and p.get("kind") == "main":
                self.add(("screen", p.get("passenger_id"), p.get("channel")), e)
            if k == "trip_record_updated" and e.get("actor") == "simulator":
                self.add(("trip", p.get("passenger_id")), e)
                self.add(("trips_at", e["t"]), e)
        self.cursor = len(events)

    def get(self, *key):
        return self.by.get(key, [])


def _staff_key(role, s):
    return (s["destinations"]["staff_roles"] or {}).get(role, role)


class After:
    def __init__(self, copilot, channels, settings):
        self.c, self.channels, self.s = copilot, channels, settings
        self.idx = EventIndex()

    # ------------------------------------------------------------ before the answer
    def passengers_on(self, flight, now):
        """Passengers whose latest published trip record (as the agents were given it) includes this flight."""
        last = {}
        for e in self.c.store.events:
            if e["type"] == "trip_record_updated" and e["occurred_at"] <= now:
                last[e["payload"]["passenger_id"]] = e["payload"].get("itinerary") or []
        return sorted(p for p, it in last.items() if flight in it)

    def screens(self, pids):
        types = {p["id"]: p.get("channel_types") or [] for p in self.c.context.rows.get("passengers", [])}
        out = {}
        for pid in pids:
            for ch in screens_for(types.get(pid, [])):
                out[ch] = out.get(ch, 0) + 1
        return [{"key": ch, "label": f"{CHANNEL_NAMES[ch]} ({n} passenger{'s' if n != 1 else ''})"} for ch, n in
                sorted(out.items(), key=lambda kv: CHANNELS.index(kv[0]))]

    def preview(self, q, now):
        p, f = q.proposal or {}, q.target.get("flight_id")
        changes, dest = [], []
        if q.agent == "network_recovery" and q.action == "delay_flight" and p.get("new_departure"):
            changes.append({"label": f"{f} departs at {hhmm(p['new_departure'])}"})
            dest = self.screens(self.passengers_on(f, now))
        elif q.agent == "network_recovery" and q.action == "cancel_flight":
            changes.append({"label": f"{f} is cancelled"})
            dest = self.screens(self.passengers_on(f, now))
        elif q.agent == "network_recovery" and q.action in ("call_in_reserve_crew", "swap_crews", "deadhead_crew"):
            who = "pilots" if p.get("crew_role") == "pilot" else "flight attendants"
            changes.append({"label": f"crew {p.get('crew_id')} flies {f} as its {who}"})
        elif p.get("human_only"):
            changes.append({"label": f"your decision on {', '.join(p.get('decision_ids') or [])}: {q.title}"})
        elif q.agent == "passenger_care" and q.action == "rebook_passenger":
            changes.append({"label": q.title})
            dest = self.screens(q.passenger_ids)
        elif q.agent in ("passenger_care", "station_staff"):
            changes.append({"label": q.title})
            roles = sorted({t.get("role") for t in p.get("tasks") or [] if t.get("role")})
            dest = [{"key": _staff_key(r, self.s), "label": _staff_key(r, self.s).replace("_", " ").capitalize()} for r in roles]
        if p.get("messages"):                               # a message: who it goes to, from the proposal itself
            by = {}
            for m in p["messages"]:
                ch = m.get("channel")
                keys = ["gate_agents", "call_center_agents"] if ch == "staff" else [ch]
                for k in keys:
                    by[k] = by.get(k, 0) + 1
            dest = [{"key": k, "label": f"{CHANNEL_NAMES.get(k, k.replace('_', ' ').capitalize())} ({n})"} for k, n in by.items()]
        return {"changes": changes, "destinations": dest}

    # ------------------------------------------------------------ after the answer
    def changes(self, q, t0):
        """Confirmation events of the change this decision made (plain words)."""
        rid, out = str(q.rec_id), []
        if q.agent == "network_recovery" and not (q.proposal or {}).get("human_only"):
            for e in self.idx.get("proposal", rid):
                p = e["payload"]
                label = {"delay_flight": f"{p.get('flight_id')} delay carried out: new departure {hhmm(p.get('new_departure'))}",
                         "cancel_flight": f"{p.get('flight_id')} cancelled"}.get(p.get("action"),
                                                                                f"crew {p.get('crew_id')} assigned to {p.get('flight_id')} ({p.get('role')})")
                out.append((e, label))
                for g in self.idx.get("flight", p.get("flight_id")):
                    if g["t"] != e["t"]:
                        continue
                    gp = g["payload"]
                    if g["type"] in ("flight_delayed", "flight_delay_updated"):
                        out.append((g, f"{gp['flight_id']} now departs {hhmm(gp.get('new_departure'))}"))
                    elif g["type"] in ("reserve_crew_assigned", "crew_reassigned") and gp.get("crew_id") == p.get("crew_id"):
                        out.append((g, f"crew tools: {gp['crew_id']} on {gp['flight_id']} ({gp.get('role')}, "
                                       f"{gp.get('reserve_duty_left_min')} min of duty left)"))
            for e in self.idx.get("rec", "network_recovery", rid):
                if e["type"] == "decision_refused":
                    out.append((e, f"not carried out: {e['payload'].get('reason')}"))
        elif q.agent == "passenger_care":
            for e in self.idx.get("rec", "passenger_care", rid):
                p = e["payload"]
                out.append((e, {"hotel_room_assigned": f"room at {p.get('hotel_id')} for {p.get('passenger_id')}",
                                "hotel_rooms_held": f"{p.get('rooms')} rooms held at {p.get('hotel_id')}",
                                "hotel_rooms_released": f"{p.get('rooms')} rooms released at {p.get('hotel_id')}",
                                "care_decision_recorded": f"care decision recorded ({p.get('action', '').replace('_', ' ')})",
                                "decision_applied": f"{len((p.get('moved') or p.get('facts', {}).get('passenger_ids') or []))} "
                                                    f"passengers moved to {p.get('facts', {}).get('flight_id')}",
                                "decision_refused": f"not carried out: {p.get('reason')}"}[e["type"]]))
        elif q.agent == "station_staff":
            for e in self.idx.get("rec", "station_staff", rid):
                p = e["payload"]
                label = {"gate_assigned": f"gate {p.get('gate')} for {p.get('flight_id')} {hhmm(p.get('from'))}-{hhmm(p.get('to'))}",
                         "gate_released": f"gate {p.get('gate')} released for {p.get('flight_id')}",
                         "staff_assigned": f"{p.get('staff_id')} ({p.get('role')}) on {p.get('task')} for "
                                           f"{p.get('flight_id') or p.get('passenger_id')} {hhmm(p.get('from'))}-{hhmm(p.get('to'))}",
                         "staff_released": f"{p.get('staff_id')} released",
                         "staff_called_in": f"{p.get('staff_id')} called in {hhmm(p.get('from'))}-{hhmm(p.get('to'))}",
                         "child_supervisor_assigned": f"{p.get('staff_id')} supervises {p.get('passenger_id')}",
                         "ramp_tasks_paused": f"ramp work paused from {hhmm(p.get('from'))}",
                         "station_decision_recorded": f"station decision recorded ({p.get('status')})"}[e["type"]]
                out.append((e, label))
        elif q.agent == "orchestrator":
            for e in self.idx.get("incident"):
                if t0 <= e["t"] <= t0 + 1 and e["payload"].get("approved_by"):
                    out.append((e, f"incident #{e['payload'].get('incident_id')}: " +
                                (e["payload"].get("status") or ("closed" if e["type"] == "incident_closed" else "updated"))))
        for e in self.idx.get("rec", q.agent, rid):              # a data conflict answered (A09)
            if e["type"] == "human_decision_recorded":
                out.append((e, f"conflict answered: {e['payload'].get('answer_side')}"))
        for e in self.idx.get("question", q.id):                 # a human-only decision: your answer is the record
            p = e["payload"]
            if e["type"] == "human_decision_recorded":
                out.append((e, f"Your decision was recorded: {p.get('answer')} (as {str(p.get('decided_by_role')).replace('_', ' ')})"))
            elif e["type"] == "human_decision_applied":
                out.append((e, f"Pilots' duty extended by {p.get('extension_min')} min for {p.get('flight_id')}" if p.get("applied")
                            else f"Not carried out: {p.get('reason')}"))
                if p.get("applied"):
                    for g in self.idx.get("flight", p.get("flight_id")):
                        if g["type"] == "pilot_duty_extended" and g["t"] == e["t"]:
                            out.append((g, f"Crew records: pilots {g['payload'].get('crew_id')} now have "
                                           f"{g['payload'].get('duty_left_before_min', 0) + g['payload'].get('extension_min', 0)} "
                                           f"min of duty left"))
                        elif g["type"] == "flight_departed" and g["t"] >= e["t"]:
                            out.append((g, f"{p.get('flight_id')} departed at {_fmt(g['t'])}"))
                            break
        return [(e, l) for e, l in out if e["t"] >= t0]

    def messages(self, q, changes):
        """The Communication drafts and messages this decision caused: (node label, detail, [(message id, status, text, channel)])."""
        groups = []
        rid = q.rec_id
        if q.agent == "network_recovery":
            latest = {}
            for h in self.c.recovery.history:
                if str(h.get("proposal_id")) == str(rid):
                    latest[h["id"]] = h
            by = {}
            for h in latest.values():
                by.setdefault((h.get("audience"), h.get("plan_version")), []).append(h)
            for (aud, pv), hs in sorted(by.items(), key=lambda kv: str(kv[0])):
                st = sorted({h["status"] for h in hs})
                groups.append((f"recovery message to {len(hs)} {aud}{'s' if len(hs) != 1 else ''} ({', '.join(st)})",
                               {"plan_version": pv, "audience": aud, "statuses": st, "text": hs[0].get("text")},
                               [(h["id"], h["status"], h.get("text"), h.get("channel")) for h in hs if h["status"] != "discarded"]))
        elif q.agent == "communication":
            msgs = (q.proposal or {}).get("messages") or []
            log = {m["id"]: m for m in self.c.messages.log if m.get("id")}
            items = [(m.get("id"), (log.get(m.get("id")) or {}).get("status", "drafted"), m.get("text"), m.get("channel")) for m in msgs]
            groups.append((f"{q.action.replace('_', ' ')}: {len(msgs)} message{'s' if len(msgs) != 1 else ''}",
                           {"recommendation": rid, "text": msgs[0].get("text") if msgs else None}, items))
        elif q.agent == "passenger_care":
            ms = [m for m in self.c.messages.log if str(m.get("backed_by")) == str(rid)]
            if ms:
                groups.append((f"{len(ms)} message{'s' if len(ms) != 1 else ''} backed by this decision",
                               {"recommendation": rid, "text": ms[0].get("text")},
                               [(m["id"], m.get("status"), m.get("text"), m.get("channel")) for m in ms]))
        return groups

    def destinations(self, items):
        """One entry per destination from the messages' delivery events: delivered, failed or waiting, with the words."""
        dest = {}

        def put(key, kind, status, text, by="delivery event"):
            d = dest.setdefault(key, {"key": key, "kind": kind, "delivered": 0, "failed": 0, "waiting": 0, "text": None,
                                      "confirmed_by": []})
            d[status] += 1
            d["text"] = d["text"] or text
            if by not in d["confirmed_by"]:
                d["confirmed_by"].append(by)

        for mid, status, text, ch in items:
            evs = self.idx.get("msg", mid)
            if not evs:
                if status in ("discarded", "withdrawn", "expired"):
                    continue
                if ch == "staff":                            # an internal staff note: Communication records it sent itself
                    st = "delivered" if status == "sent" else "waiting"
                    by = "Communication's own record (an internal note has no separate delivery receipt)"
                    put("gate_agents", "staff", st, text, by)
                    put("call_center_agents", "staff", st, text, by)
                else:
                    put(ch, "staff" if ch == "crew" else "channel", "waiting", text)
                continue
            for e in evs:
                p = e["payload"]
                if e["type"] == "staff_note_published":
                    put("gate_agents", "staff", "delivered", p.get("text"))
                    put("call_center_agents", "staff", "delivered", p.get("text"))
                elif e["type"] == "crew_message_delivered":
                    put("crew", "staff", "delivered", p.get("text"))
                elif e["type"] == "message_delivered":
                    put(p.get("channel"), "channel", "delivered", p.get("text"))
                else:
                    put(p.get("channel"), "channel", "failed", text)
        return dest

    def screen_updates(self, change_events):
        """A flight change reaches passengers' screens through today's systems: one per screen, first copy after the change."""
        dest = {}
        for e in change_events:
            fid = (e["payload"] or {}).get("flight_id") or ((e["payload"] or {}).get("facts") or {}).get("flight_id")
            if not fid or e["type"] not in ("recovery_action_applied", "decision_applied"):
                continue
            pids = {x["payload"]["passenger_id"] for x in self.idx.get("trips_at", e["t"])
                    if fid in (x["payload"].get("itinerary") or [])
                    or any(fid in (y["payload"].get("itinerary") or []) for y in self.idx.get("trip", x["payload"]["passenger_id"])
                           if y["t"] < e["t"])}
            for pid in sorted(pids):
                for ch in self.channels.screens.get(pid, ()):
                    first = next((x for x in self.idx.get("screen", pid, ch) if x["t"] >= e["t"]), None)
                    status = "waiting" if first is None else "failed" if first["type"] == "channel_update_failed" else "delivered"
                    d = dest.setdefault(ch, {"key": ch, "kind": "channel", "delivered": 0, "failed": 0, "waiting": 0, "text": None,
                                             "passengers": 0})
                    d[status] += 1
                    d["passengers"] += 1
                    if status == "delivered" and d["text"] is None:
                        d["text"] = first["payload"].get("message")
        return dest

    def staff_from_changes(self, change_events):
        dest = {}
        for e in change_events:
            p = e["payload"] or {}
            if e["type"] in ("staff_assigned", "staff_called_in"):
                key = _staff_key(p.get("role"), self.s)
                d = dest.setdefault(key, {"key": key, "kind": "staff", "delivered": 0, "failed": 0, "waiting": 0, "text": None})
                d["delivered"] += 1
                d["text"] = d["text"] or f"{p.get('staff_id')}: {p.get('task', 'called in')} {hhmm(p.get('from'))}-{hhmm(p.get('to'))}"
            elif e["type"] == "hotel_room_assigned":
                d = dest.setdefault("hotel_desk", {"key": "hotel_desk", "kind": "staff", "delivered": 0, "failed": 0, "waiting": 0,
                                                   "text": None})
                d["delivered"] += 1
                d["text"] = d["text"] or f"room for {p.get('passenger_id')} at {p.get('hotel_id')}"
        return dest

    def extend(self, trace, q, events):
        """Add the after-the-answer nodes and edges to a trace (in place)."""
        nodes, edges = trace["nodes"], trace["edges"]
        human = next((n for n in nodes if n["kind"] == "human"), None)
        if human is None or (q.answer or {}).get("decision") not in ("approved", "acknowledged"):
            return trace
        self.idx.update(events)
        t0 = _minute(q.answered_at)

        def node(kind, label, t, detail):
            nid = f"n{len(nodes) + 1}"
            nodes.append({"id": nid, "kind": kind, "label": label, "t": t, "detail": detail})
            return nid

        changes = self.changes(q, t0)
        first = None
        for e, label in changes:
            nid = node("change", label, _fmt(e["t"]), {"event": e["type"], **{k: v for k, v in (e["payload"] or {}).items()
                                                                                if not isinstance(v, (dict, list))}})
            edges.append({"from": human["id"], "to": nid, "label": "confirmed", "t": _fmt(e["t"])})
            first = first or nid
        source = first or human["id"]
        groups = self.messages(q, changes)
        for label, detail, items in groups:
            cn = node("communication", label, human["t"], detail)
            edges.append({"from": source, "to": cn, "label": "sent to", "t": human["t"]})
            self._dest_nodes(node, edges, cn, self.destinations(items))
        ev = [e for e, _ in changes]
        if ev:
            self._dest_nodes(node, edges, first, self.screen_updates(ev))
            self._dest_nodes(node, edges, first, self.staff_from_changes(ev))
        return trace

    @staticmethod
    def _dest_nodes(node, edges, parent, dest):
        for key, d in dest.items():
            status = "waiting" if d["waiting"] else "failed" if d["failed"] else "delivered"
            label = (CHANNEL_NAMES.get(key) or key.replace("_", " ").capitalize()) + f": {status}"
            if d["delivered"] + d["failed"] + d["waiting"] > 1:
                label += f" ({d['delivered']} delivered, {d['failed']} failed, {d['waiting']} waiting)"
            nid = node(d["kind"], label, "", {"status": status, "text": d["text"], "key": key,
                                              **{k: d[k] for k in ("delivered", "failed", "waiting")},
                                              **({"confirmed_by": d["confirmed_by"]} if d.get("confirmed_by") else {}),
                                              **({"passengers": d["passengers"]} if "passengers" in d else {})})
            edges.append({"from": parent, "to": nid, "label": "confirmed" if status == "delivered" else "sent to", "t": ""})

    # ------------------------------------------------------------ Communication's versions (for the flow)
    def comm_versions(self, now, since):
        """Each batch of recovery messages drafted or sent, and each passenger or mass message proposal drafted or sent, is one
        Communication version; "used" is the recovery plan and Situation versions it was built on."""
        out = []
        seen = {}
        for h in self.c.recovery.history:
            if h.get("status") not in ("drafted", "sent"):
                continue
            t = h.get("as_of") or h.get("created")
            key = (h.get("proposal_id"), h.get("plan_version"), h["status"], (t or "")[:16])
            if key in seen:
                seen[key]["messages"] += 1
                continue
            seen[key] = {"t": t, "what": f"recovery messages {h['status']} for proposal {h.get('proposal_id')}",
                         "used": [{"kind": "network_recovery", "ref": h.get("plan_version")}], "messages": 1}
            out.append(seen[key])
        recs = {}
        for m in self.c.messages.log:
            rec = m.get("rec")
            for status, t in (("drafted", m.get("created")), ("sent", m.get("sent_at") if m.get("status") == "sent" else None)):
                if t is None:
                    continue
                t = t.isoformat() if hasattr(t, "isoformat") else t
                key = (rec, status)
                if key in recs:
                    recs[key]["messages"] += 1
                    continue
                opt = self.c.message_options.get(rec) or {}
                used = [{"kind": "situation", "ref": opt["situation_version"]}] if opt.get("situation_version") else []
                recs[key] = {"t": t,
                             "what": f"{str(m.get('action') or m.get('type')).replace('_', ' ')} {status} (recommendation {rec})",
                             "used": used, "messages": 1}
                out.append(recs[key])
        out = [v for v in out if v["t"] and v["t"] <= now.isoformat()]
        out.sort(key=lambda v: v["t"])
        for i, v in enumerate(out, 1):                     # numbered over the whole day, so a version keeps its number
            v["v"] = i
        return [v for v in out if _minute_iso(v["t"]) >= since]


def _minute(t):
    from backend.execution.live_trace import minute
    return minute(t)


def _minute_iso(text):
    from datetime import datetime
    return _minute(datetime.fromisoformat(text))


def _fmt(m):
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"
