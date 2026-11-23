"""Independent observations of channel outcomes; never an input to an agent."""
from collections import Counter
import hashlib
import json
from statistics import median

FIELDS = ("itinerary", "status", "destination", "bag_routing")
# what the channels did with co-pilot messages: deliveries, not world truth
DELIVERY_EVENTS = {"message_delivered", "message_dropped", "staff_note_published"}


def snapshot(row):
    return {k: list(row[k]) if isinstance(row[k], tuple) else row[k] for k in FIELDS}


class ComparisonRecorder:
    def __init__(self, settings, focus=None):
        self.settings = settings
        self.focus = focus                    # the story passenger's id (config/comparison.yaml focus_passenger)
        self.previous = {}
        self.pending = {}
        self.latencies = []
        self.superseded = 0
        self.critical_minutes = 0
        self.last_wrong = None
        self.first_agree = None
        self.maya = []
        self.maya_versions = []
        self.times = []
        self.counts = []
        self.pax_minutes = Counter()             # passenger -> minutes with at least one wrong screen
        self.cancellations = {}                  # passenger -> minutes their trip was cancelled or rebooked (for message timing)

    def tick(self, t, world, channels):
        critical = set()
        for pid, ch, field in channels.dis:
            if field == "bag_routing":
                continue
            truth, shown = world.truth[pid], channels.shown[(pid, ch)]
            cancelled = truth["status"] == "cancelled" or any(world.flights[f]["status"] == "cancelled"
                                                                           for f in shown["itinerary"] if f in world.flights)
            vulnerable = world.passengers[pid]["segment"] in self.settings["severity"]["vulnerable_segments"]
            near = any(world.flights[f]["status"] not in ("departed", "arrived", "cancelled")
                       and world.flights[f]["est_dep"] - t <= self.settings["severity"]["vulnerable_within_min"]
                       for f in tuple(truth["itinerary"]) + tuple(shown["itinerary"]) if f in world.flights)
            if (cancelled and shown["status"] != "cancelled") or (vulnerable and near):
                critical.add(pid)
        self.critical_minutes += len(critical)
        if channels.dis:
            self.last_wrong = t
        elif t >= world.sc.end and self.first_agree is None:
            self.first_agree = t
        self.times.append(t)
        self.counts.append(channels.passengers_disagreeing())
        self.pax_minutes.update(channels.dis_pax.keys())
        for pid, row in world.truth.items():
            snap = snapshot(row)
            old = self.previous.get(pid)
            if old is not None and old != snap and (snap["status"] in ("cancelled", "overnight") and old["status"] != snap["status"]
                                                    or [f for f in snap["itinerary"] if f not in old["itinerary"]]):
                self.cancellations.setdefault(pid, []).append(t)
            if old is not None and old != snap:
                if pid in self.pending:
                    self.superseded += 1
                self.pending[pid] = (t, snap)
                if pid == self.focus:
                    self.maya_versions.append({"changed_at": t, "truth": snap, "catch_up": {}})
            self.previous[pid] = snap
            if pid in self.pending and all(snapshot(channels.shown[(pid, ch)]) == snap for ch in channels.screens[pid]):
                self.latencies.append(t - self.pending.pop(pid)[0])
        if self.focus in world.truth:
            self.maya.append({"t": t, "truth": snapshot(world.truth[self.focus]),
                              "channels": {ch: snapshot(channels.shown[(self.focus, ch)]) for ch in channels.screens[self.focus]}})
            if self.maya_versions:
                version = self.maya_versions[-1]
                for ch in channels.screens[self.focus]:
                    if ch not in version["catch_up"] and all(channels.shown[(self.focus, ch)][f] == world.truth[self.focus][f]
                                                            for f in FIELDS[:3]):
                        version["catch_up"][ch] = t

    def finish(self, world, channels, stop_end, copilot=None):
        proposals = copilot.proposals if copilot else []
        applied = [p for p, r in zip(proposals, copilot.executions) if r["status"] == "applied"] if copilot else []
        per_pax = Counter(p["passenger_id"] for p in applied)
        per_ch = Counter(p["channel"] for p in applied)
        total_pax, total_ch = Counter(), Counter()
        for (pid, ch), count in channels.sent_main.items():
            total_pax[pid] += count
            total_ch[ch] += count
        failures = [e for e in world.events if e["type"] == "channel_update_failed" and e["payload"].get("fix_id") is not None]
        resent = {e["payload"]["fix_id"] for e in failures if any(p["action"] == "resend_on_other_channel" and p["passenger_id"] == e["payload"]["passenger_id"]
                         and p["original_channel"] == e["payload"]["channel"]
                         and p["truth_as_of"] == e["payload"]["version_at"]
                         and p["data_as_of"] >= e["payload"]["failed_at"] for p in applied)}
        retried = {e["payload"]["fix_id"] for e in failures
                   if any(p["passenger_id"] == e["payload"]["passenger_id"] and p["original_channel"] == e["payload"]["channel"]
                          and p["truth_as_of"] == e["payload"]["version_at"] and p["data_as_of"] >= e["payload"]["failed_at"]
                          for p in applied)}
        held = [e for e in world.events if e["type"] == "channel_fix_held"]
        holds = Counter(e["payload"].get("kind") or e["payload"]["reason"] for e in held)
        by_flight = {}
        for e in held:
            if e["payload"].get("kind") == "situation_conflict":
                for fid in e["payload"]["flight_ids"]:
                    f = by_flight.setdefault(fid, {"flight_id": fid, "holds": 0, "passengers": set(), "first": e["t"], "last": e["t"]})
                    f["holds"] += 1
                    f["passengers"].add(e["payload"]["passenger_id"])
                    f["last"] = e["t"]
        conflict_holds = [{**f, "passengers": sorted(f["passengers"])} for f in by_flight.values()]
        link = Counter(e["type"] for e in world.events if e["type"] in ("situation_unavailable", "situation_available_again",
                                                                        "situation_cannot_assess", "agents_disagree",
                                                                        "conflict_hold_cleared"))
        extra = {}
        if copilot:
            from simulator.message_metrics import conflict_metrics, message_metrics
            extra = {**message_metrics(world, copilot, self.focus, self.cancellations, channels.screens),
                     "conflict_detail": conflict_metrics(copilot)}
        return {**extra, "holds_by_reason": dict(holds), "conflict_holds": conflict_holds, "situation_link_events": dict(link),
                "copilot_situation": copilot.situation_summary() if copilot else None,
                "pax_minutes_wrong": dict(self.pax_minutes),
                "critical_passenger_minutes_wrong": self.critical_minutes,
                "all_screens_agree_after_stop_min": None if self.first_agree is None else self.first_agree - stop_end,
                "all_screens_stay_correct_after_stop_min": None if channels.dis else max(0, (self.last_wrong or stop_end - 1) + 1 - stop_end),
                "lost_updates_wrong_end": len({(p, ch, "bag" if f == "bag_routing" else "main") for p, ch, f in channels.dis
                                                if (p, ch, "bag" if f == "bag_routing" else "main") in channels.failed}),
                "catch_up_median_min": median(self.latencies) if self.latencies else None,
                "catch_up_worst_min": max(self.latencies, default=None),
                "completed_versions": len(self.latencies), "superseded_versions": self.superseded,
                "versions_still_wrong_end": len(self.pending),
                "pushes_per_passenger_avg": round(len(applied) / len(world.passengers), 3),
                "pushes_per_passenger_max": max(per_pax.values(), default=0),
                "pushes_per_channel": {ch: per_ch[ch] for ch in self.settings["channels"]},
                "all_pushes_per_passenger_avg": round(sum(total_pax.values()) / len(world.passengers), 3),
                "all_pushes_per_passenger_max": max(total_pax.values(), default=0),
                "all_pushes_per_channel": {ch: total_ch[ch] for ch in self.settings["channels"]},
                "fixes_failed": len({e["payload"]["fix_id"] for e in failures}), "failed_fixes_resent": len(resent),
                "failed_fixes_retried": len(retried),
                "failed_delivery_components": len(failures),
                "maya_versions": self.maya_versions, "maya_minutes": self.maya,
                "comparison_series": {"t": self.times, "pax_disagreeing": self.counts}}


def focus_passenger(data):
    import yaml
    from backend.settings import ROOT
    name = yaml.safe_load((ROOT / "config" / "comparison.yaml").read_text(encoding="utf-8"))["focus_passenger"]
    return next((p["id"] for p in data["passengers"] if p["full_name"] == name), None)


def truth_fingerprint(result):
    w = result.world
    state = {name: getattr(w, name) for name in ("flights", "aircraft", "crews", "stations", "passengers", "bookings", "bags")}
    state["groups"] = sorted((list(k), v) for k, v in w.groups.items())
    state["events"] = [e for e in w.events if e["actor"] == "simulator" and not e["type"].startswith("channel_")
                       and e["type"] not in DELIVERY_EVENTS]
    state["audit"] = [a for a in w.audit_rows if a["actor"] == "simulator"]
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
