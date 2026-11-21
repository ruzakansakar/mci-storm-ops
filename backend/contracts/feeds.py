"""Simulated source heartbeats during a replay.

Each simulated source (faa_feed, weather_feed, flight_ops, station_ops, ...) sends a heartbeat every simulated minute, derived from
the replay clock. A quiet period with no events is therefore NOT stale data. A source can be silent for a while (an outage window):
its heartbeat freezes at the start of the window, and AgentContext holds back that feed's events from then until the feed is back
(they are delivered late, when it returns). A window with no end means the source never comes back.
"""
from datetime import datetime, timedelta


class Feeds:
    def __init__(self, stopped: dict[str, datetime] | None = None, outages: dict[str, list] | None = None):
        self.outages: dict[str, list] = {f: [tuple(w) for w in ws] for f, ws in (outages or {}).items()}
        for f, at in (stopped or {}).items():
            self.outages.setdefault(f, []).append((at, None))

    def stop(self, feed: str, at: datetime, until: datetime | None = None) -> None:
        self.outages.setdefault(feed, []).append((at, until))

    @property
    def stopped(self) -> dict[str, datetime]:
        return {f: min(w[0] for w in ws) for f, ws in self.outages.items() if ws}

    def window(self, feed: str, now: datetime):
        """The outage window this feed is in at `now`, or None if it is up."""
        for start, end in self.outages.get(feed, []):
            if start <= now and (end is None or now < end):
                return start, end
        return None

    def last_heartbeat(self, feed: str, now: datetime) -> datetime:
        w = self.window(feed, now)
        beat = w[0] if w else now
        return beat - timedelta(seconds=beat.second, microseconds=beat.microsecond)     # heartbeats come on the minute

    def is_up(self, feed: str, now: datetime) -> bool:
        return self.window(feed, now) is None

    def change_times(self) -> list[datetime]:
        out = []
        for ws in self.outages.values():
            for start, end in ws:
                out.append(start)
                if end is not None:
                    out.append(end)
        return out
