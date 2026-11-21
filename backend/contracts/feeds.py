"""Simulated source heartbeats during a replay.

Each simulated source (faa_feed, weather_feed, flight_ops, station_ops) sends a heartbeat every simulated minute, derived from
the replay clock. A quiet period with no events is therefore NOT stale data. Tests can "stop" a source at a time to simulate a
feed outage: its heartbeat freezes there, and AgentContext hides that feed's events after the stop.
"""
from datetime import datetime, timedelta


class Feeds:
    def __init__(self, stopped: dict[str, datetime] | None = None, every_sec: int = 60):
        self.stopped = dict(stopped or {})
        self.every = timedelta(seconds=every_sec)

    def stop(self, feed: str, at: datetime) -> None:
        self.stopped[feed] = at

    def last_heartbeat(self, feed: str, now: datetime) -> datetime:
        stop = self.stopped.get(feed)
        beat = now if stop is None or stop > now else stop
        # heartbeats come on the minute
        return beat - timedelta(seconds=beat.second, microseconds=beat.microsecond)

    def is_up(self, feed: str, now: datetime) -> bool:
        stop = self.stopped.get(feed)
        return stop is None or stop > now
