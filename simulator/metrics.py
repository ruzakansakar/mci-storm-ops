"""Numbers for one run: what the day cost, and how wrong the passenger screens were."""
from collections import Counter


class Recorder:
    """Samples the day minute by minute (cheap numbers) and a coarser time series for the charts."""

    def __init__(self, cfg: dict, sc, start: int):
        self.every = cfg["metrics"]["series_every_min"]
        self.sc, self.start = sc, start
        self.minutes = 0
        self.sum_pax = self.peak_pax = self.sum_pax_incident = self.incident_minutes = 0
        self.cause_minutes = Counter()
        self.last_fixable_t = None                     # last minute with a wrong screen that was NOT a lost message
        self.first_agree_t = None                      # first minute after the stop when no such wrong screen was left
        self.fixable_now = 0
        self.failed_now = 0
        self.peak_people = 0
        self.series = {"t": [], "flights": [], "pax_disagreeing": [], "fields_disagreeing": [], "people_in_terminal": []}

    def tick(self, t, world, channels):
        n = channels.passengers_disagreeing()
        causes = channels.causes_now()
        self.minutes += 1
        self.sum_pax += n
        self.peak_pax = max(self.peak_pax, n)
        self.cause_minutes.update(causes)
        self.failed_now = causes.get("message_failed", 0)
        self.fixable_now = sum(causes.values()) - self.failed_now
        if self.fixable_now:
            self.last_fixable_t = t
        elif t >= self.sc.end and self.first_agree_t is None:
            self.first_agree_t = t
        if self.sc.start <= t <= self.sc.end:
            self.incident_minutes += 1
            self.sum_pax_incident += n
        people = world.mci["people_in_terminal"] or 0
        self.peak_people = max(self.peak_people, people)
        if (t - self.start) % self.every == 0:
            s = self.series
            s["t"].append(t)
            s["flights"].append(world.status_counts())
            s["pax_disagreeing"].append(n)
            s["fields_disagreeing"].append(sum(causes.values()))
            s["people_in_terminal"].append(people)

    def finish(self, world, channels, until: int) -> dict:
        ph = world.ph
        delayed, cancelled, diverted, delays = [], [], [], []
        for f in world.flights.values():
            if f["status"] == "cancelled":
                cancelled.append(f)
                continue
            if f["diverted_to"]:
                diverted.append(f)
            late = ((f["actual_dep"] if f["actual_dep"] is not None else f["est_dep"]) - f["sched_dep"]
                    if f["direction"] == "departure" else
                    (f["actual_arr"] if f["actual_arr"] is not None else f["est_arr"]) - f["sched_arr"])
            if late >= ph["disrupted_delay_min"]:
                delayed.append(f)
                delays.append(late)
        disrupted = {f["id"] for f in delayed + cancelled + diverted}
        pax = sum(world.flights[i]["_orig_pax"] for i in disrupted)
        seg = lambda name: sum(world.orig_groups[(i, name)] for i in disrupted)           # noqa: E731
        rebooked = sum(g["rebooked"] for g in world.groups.values())
        stranded = sum(1 for bks in world.bookings.values() for b in bks if b["status"] == "cancelled")
        agree_min = None if self.first_agree_t is None else self.first_agree_t - self.sc.end
        last_min = None
        if self.last_fixable_t is not None and self.fixable_now == 0:
            last_min = max(0, self.last_fixable_t - self.sc.end + 1)
        return {
            "flights_total": len(world.flights),
            "flights_delayed": len(delayed),
            "flights_cancelled": len(cancelled),
            "flights_diverted": len(diverted),
            "avg_delay_min": round(sum(delays) / len(delays), 1) if delays else 0,
            "max_delay_min": max(delays) if delays else 0,
            "passengers_disrupted": pax,
            "passengers_rebooked": rebooked,
            "tracked_passengers": len(world.passengers),
            "tracked_passengers_stranded": stranded,
            "peak_passengers_disagreeing": self.peak_pax,
            "avg_passengers_disagreeing": round(self.sum_pax / max(1, self.minutes), 2),
            "avg_passengers_disagreeing_during_stop": round(self.sum_pax_incident / max(1, self.incident_minutes), 2),
            "passenger_minutes_of_disagreement": self.sum_pax,
            "minutes_until_all_channels_agree_after_stop": agree_min,
            "minutes_until_last_late_copy_arrives": last_min,
            "lost_messages_still_wrong_at_end": self.failed_now,
            "max_tarmac_min": world.open_tarmac_max(),
            "wheelchair_passengers_disrupted": seg("wheelchair"),
            "child_alone_passengers_disrupted": seg("child_alone"),
            "peak_people_in_terminal": self.peak_people,
            "stop_start": self.sc.start,
            "stop_end": self.sc.end,
            "wrong_field_minutes_by_cause": dict(sorted(self.cause_minutes.items())),
            "timeseries": self.series,
        }
