"""The simulated clock: minutes after midnight on SIM_DATE (America/Chicago), one tick per minute."""
from datetime import datetime, time, timedelta

from backend.settings import SIM_DATE, TZ

MIDNIGHT = datetime.combine(SIM_DATE, time(0, 0), TZ)


def at(minute: int) -> datetime:
    """The real time of a simulated minute (minutes after midnight)."""
    return MIDNIGHT + timedelta(minutes=int(minute))


def minute_of(moment: datetime) -> int:
    return int((moment - MIDNIGHT).total_seconds() // 60)


def hhmm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def fmt(minute: int) -> str:
    """12:35 style label (a trailing +1 means after midnight)."""
    day, rest = divmod(int(minute), 1440)
    return f"{rest // 60:02d}:{rest % 60:02d}" + (f"+{day}" if day else "")


class SimClock:
    """Walks the day one minute at a time."""

    def __init__(self, start: int, end: int):
        self.start, self.end, self.now = start, end, start

    def ticks(self):
        for self.now in range(self.start, self.end + 1):
            yield self.now
