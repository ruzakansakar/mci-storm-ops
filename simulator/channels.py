"""The four passenger channels (app, sms, gate screen, call center).

Each channel keeps its own copy of what a passenger should see. Every time the truth changes, each channel
gets a copy of the change LATER (and sometimes never). A channel never shows a correct value early: until
its copy arrives it keeps showing the old one. That gap is the problem the co-pilot fixes in later phases.

This module records nothing in the `mismatches` table (finding mismatches is a later phase). It only exposes
count_disagreements(...) so we can measure how many screens are wrong.
"""
import heapq
from collections import Counter, deque

from simulator.config import CHANNELS
from backend.channel_causes import canonical

MAIN_FIELDS = ("itinerary", "status", "destination")
FIELD_KIND = {"itinerary": "main", "status": "main", "destination": "main", "bag_routing": "bag"}
FIELDS = tuple(FIELD_KIND)


class Channels:
    def __init__(self, cfg: dict, rng, initial_truth: dict, start: int):
        self.cfg, self.rng = cfg, rng
        self.truth = dict(initial_truth)                       # passenger id -> latest truth snapshot
        # what each channel shows right now: (pid, channel) -> snapshot of the truth as the channel last heard it
        self.shown = {(pid, ch): {**snap, "updated_at": start} for pid, snap in initial_truth.items() for ch in CHANNELS}
        self.queue = []                                        # heap of (due, seq, update)
        self.waiting = {}                                      # (pid, channel, kind) -> [updates not applied yet]
        self.failed = {}                                       # (pid, channel, kind) -> update that never arrived
        self.recent = deque()                                  # minutes of recent truth changes (the "load")
        self.seq = 0
        self.last_due = {}
        self.dis = {}                                          # (pid, channel, field) -> cause, for every wrong field now
        self.dis_pax = Counter()                               # pid -> number of wrong fields now
        self.dirty_pairs = set()
        self.events = []                                       # events to log (channel updates and failures)
        self.touched = set()                                   # (pid, channel) whose shown copy changed since the last save
        self.versions = {pid: start for pid in initial_truth}
        self.delivered_versions = {}
        self.fix_rng = None
        self.sent_main = Counter()
        self.flight_displays = {}
        self.call_center_notes = {}

    # ---------------------------------------------------------------- truth changed

    def load(self, t) -> int:
        window = self.cfg["load"]["window_min"]
        while self.recent and self.recent[0] <= t - window:
            self.recent.popleft()
        return len(self.recent)

    def observe(self, t: int, changes: list):
        """changes: [(pid, old_truth, new_truth, was_rebooked)] from the world, in passenger order."""
        for pid, old, new, rebooked in changes:
            self.recent.append(t)
        for pid, old, new, rebooked in changes:
            self.truth[pid] = new
            self.versions[pid] = t
            load = self.load(t)
            for ch in CHANNELS:
                if any(old[f] != new[f] for f in MAIN_FIELDS):
                    seat_hold = rebooked and old["itinerary"] != new["itinerary"]
                    self._enqueue(t, pid, ch, "main", new, load, seat_hold=seat_hold)
                if old["bag_routing"] != new["bag_routing"]:
                    self._enqueue(t, pid, ch, "bag", new, load)
                self.dirty_pairs.add((pid, ch))

    def _enqueue(self, t, pid, ch, kind, snapshot, load, seat_hold=False, fix_id=None):
        c, lab = self.cfg["channels"][ch], self.cfg["labels"]
        rng = self.fix_rng if fix_id is not None else self.rng
        extra = min(self.cfg["load"]["extra_cap_min"], c["per_100_changes_min"] * load / 100)
        lag = c["base_min"] + extra + rng.uniform(0, c["jitter_min"])
        if seat_hold:
            lag += lab["seat_hold_extra_min"]
        if kind == "bag":
            lag += lab["bag_system_extra_min"]
        lag = max(1, round(lag))
        fails = rng.random() < c["fail_chance"]
        key = (pid, ch, kind)
        due = t + lag if fix_id is not None else max(t + lag, self.last_due.get(key, 0))
        if fix_id is None:
            self.last_due[key] = due
        if kind == "bag":
            cause = "bag_system"
        elif seat_hold:
            cause = "seat_hold"
        elif lag > lab["stale_after_min"]:
            cause = "stale_copy"
        else:
            cause = "sync_delay"
        update = {"key": key, "due": due, "queued": t, "lag": due - t, "snapshot": dict(snapshot),
                  "cause": cause, "fails": fails, "version": self.versions[pid], "fix_id": fix_id}
        self.seq += 1
        heapq.heappush(self.queue, (due, self.seq, update))
        self.waiting.setdefault(key, []).append(update)
        if kind == "main":
            self.sent_main[(pid, ch)] += 1
        return update

    def deliver_fix(self, t, proposal):
        """A new delivery request uses the same delay/failure calculation.

        A confirmed correction does not repeat a reservation seat hold. Delivery
        requests can race; revision checks prevent older copies undoing a fix.
        The original random stream and original queued deliveries are untouched.
        """
        pid, ch = proposal["passenger_id"], proposal["channel"]
        snap = dict(proposal["confirmed"])
        snap["itinerary"] = tuple(snap["itinerary"])
        snap["bag_routing"] = tuple(snap["bag_routing"])
        if any(snap[f] != self.truth[pid][f] for f in FIELDS):
            return {"accepted": False, "reason": "authoritative trip changed before dispatch"}
        # Existing system text is reused verbatim; the agent drafts nothing.
        snap["message"] = self.truth[pid]["message"]
        for kind in ("main", "bag"):
            update = self._enqueue(t, pid, ch, kind, snap, self.load(t), fix_id=proposal["id"])
            update["flight_id"] = proposal.get("flight_id")
        return {"accepted": True, "queued_at": t, "endpoint": "flight_display" if ch == "gate_screen" else
                "passenger_call_center_notes" if ch == "call_center" else "push",
                "flight_id": proposal.get("flight_id"), "channel": ch}

    # ---------------------------------------------------------------- copies arrive

    def apply_due(self, t: int):
        """Copies whose time has come arrive on their channel (or are lost)."""
        while self.queue and self.queue[0][0] <= t:
            _, _, u = heapq.heappop(self.queue)
            pid, ch, kind = u["key"]
            self.waiting[u["key"]].remove(u)
            if self.fix_rng is not None and u["version"] < self.delivered_versions.get(u["key"], -1):
                self.dirty_pairs.add((pid, ch))
                continue
            metadata = {}
            if u["fix_id"] is not None:
                from simulator.clock import at
                metadata = {"fix_id": u["fix_id"], "failed_at": at(t).isoformat(), "version_at": at(u["version"]).isoformat()}
            if u["fails"]:
                self.failed[u["key"]] = u
                self.events.append({"t": t, "type": "channel_update_failed", "actor_name": f"channel:{ch}",
                                    "payload": {"passenger_id": pid, "channel": ch, "kind": kind, "queued_at": u["queued"],
                                                "lost_message": u["snapshot"]["message"], **metadata}})
            else:
                self.delivered_versions[u["key"]] = u["version"]
                self.failed.pop(u["key"], None)
                snap, cur = u["snapshot"], self.shown[(pid, ch)]
                if kind == "main":
                    cur.update(itinerary=snap["itinerary"], status=snap["status"], destination=snap["destination"],
                               message=snap["message"])
                else:
                    cur.update(bag_routing=snap["bag_routing"])
                cur["updated_at"] = t
                if u["fix_id"] is not None and kind == "main":
                    if ch == "gate_screen":
                        self.flight_displays.setdefault(u["flight_id"], {})[pid] = dict(cur)
                    elif ch == "call_center":
                        self.call_center_notes[pid] = dict(cur)
                self.touched.add((pid, ch))
                self.events.append({"t": t, "type": "channel_view_updated", "actor_name": f"channel:{ch}",
                                    "payload": {"passenger_id": pid, "channel": ch, "kind": kind, "lag_min": u["lag"],
                                                "cause_if_late": u["cause"], "shown_itinerary": list(snap["itinerary"]),
                                                "shown_status": snap["status"], "shown_destination": snap["destination"],
                                                "shown_bag_routing": list(snap["bag_routing"]), "message": snap["message"], **metadata}})
            self.dirty_pairs.add((pid, ch))

    # ---------------------------------------------------------------- measuring

    def cause_of(self, pid, ch, field) -> str:
        key = (pid, ch, FIELD_KIND[field])
        if key in self.failed:
            return "failed_delivery"
        if self.waiting.get(key):
            update = self.waiting[key][0]
            cause = update["cause"]
            if cause == "seat_hold":
                # A wrong itinerary/status is a delayed copy, not a seat-only difference.
                cause = "stale_copy" if update["lag"] > self.cfg["labels"]["stale_after_min"] else "sync_delay"
            return canonical(cause)
        return "stuck"

    def refresh(self):
        """Recompute which fields are wrong, for passengers/channels that changed since the last call."""
        for pid, ch in self.dirty_pairs:
            shown, truth = self.shown[(pid, ch)], self.truth[pid]
            for f in FIELDS:
                k = (pid, ch, f)
                wrong = shown[f] != truth[f]
                if wrong:
                    if k not in self.dis:
                        self.dis_pax[pid] += 1
                    self.dis[k] = self.cause_of(pid, ch, f)
                elif k in self.dis:
                    del self.dis[k]
                    self.dis_pax[pid] -= 1
                    if not self.dis_pax[pid]:
                        del self.dis_pax[pid]
        self.dirty_pairs.clear()

    def passengers_disagreeing(self) -> int:
        return len(self.dis_pax)

    def causes_now(self) -> Counter:
        return Counter(self.dis.values())

    def drain_events(self):
        out, self.events = self.events, []
        return out

    def snapshot_rows(self, pairs):
        """channel_views rows for the writer."""
        return [(pid, ch, self.shown[(pid, ch)]) for pid, ch in sorted(pairs)]


def count_disagreements(truth: dict, shown: dict, labeller=None) -> list:
    """Compare what the channels show with the truth.

    truth: passenger id -> {itinerary, status, destination, bag_routing, ...}
    shown: (passenger id, channel) -> {itinerary, status, destination, bag_routing, ...}
    labeller: optional function (pid, channel, field) -> cause label (stale_copy, sync_delay, seat_hold,
              message_failed, bag_system)
    Returns one row per wrong field on a channel. The number of passengers affected is
    len({row["passenger_id"] for row in rows}).
    """
    rows = []
    for (pid, ch), view in sorted(shown.items()):
        for f in FIELDS:
            if view[f] != truth[pid][f]:
                rows.append({"passenger_id": pid, "channel": ch, "field": f, "shown": view[f], "truth": truth[pid][f],
                             "cause": labeller(pid, ch, f) if labeller else None})
    return rows
