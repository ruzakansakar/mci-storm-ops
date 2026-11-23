"""The message fact check (Phase 4b-3): fail-closed checks on every passenger message and staff note before it is sent.

Part of the orchestrator's rule check (backend/rules/check.py). The agent runs the same check on its own draft first, but its own
check never counts as final (G09). A message carries its text and the CLAIMS it makes (structured facts); the check verifies
  1. every claim against the truth at send time (the confirmed trip record, flight operations, the Situation agent's picture),
  2. that every flight number, time, date, city, status word and number in the text is backed by one of those claims,
  3. no promise, blame or sensitive detail, no other passenger's information, names only where allowed,
  4. the four parts (what we know, what we don't know yet, what to do now, the next update), SMS length, plain English.
"""
import re
from functools import lru_cache

import yaml

from backend.settings import ROOT

MESSAGE_ACTIONS = {"send_status_update", "draft_message", "send_mass_message", "send_staff_briefing"}
FLIGHT = re.compile(r"\bWN\d{3,4}\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
DATE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s\d{1,2}\b")
NUMBER = re.compile(r"(?<![\w:])\d+(?![\w:])")
PAX = re.compile(r"\bPAX\d+\b")
STATUS_WORDS = ("delayed", "cancelled", "diverted", "on schedule", "continuing tomorrow")


@lru_cache
def templates() -> dict:
    return yaml.safe_load((ROOT / "config" / "message_templates.yaml").read_text(encoding="utf-8"))


@lru_cache
def settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "agent_settings" / "communication.yaml").read_text(encoding="utf-8"))["messages"]


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(phrase.lower())}(?![a-z])", text.lower()) is not None


def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 and not w.endswith("le") else 0)
    return max(1, n)


def reading_ease(text: str) -> float:
    """Flesch reading ease of the words (flight numbers, times and numbers left out). Higher = easier; 60+ is plain English."""
    sentences = max(1, len([s for s in re.split(r"[.!?]+", text) if s.strip()]))
    words = [w for w in re.findall(r"[A-Za-z']+", text)]
    if not words:
        return 100.0
    return 206.835 - 1.015 * len(words) / sentences - 84.6 * sum(syllables(w) for w in words) / len(words)


def _claim_problems(c: dict, pid: str | None, world: dict) -> list[str]:
    """Is this claim true in the world at send time?"""
    k, out = c.get("kind"), []
    flights, trips, pic = world.get("flights", {}), world.get("confirmed_trips", {}), world.get("situation") or {}
    f = flights.get(c.get("flight")) if c.get("flight") else None
    if c.get("flight") and f is None:
        return [f"talks about {c['flight']}, which flight operations doesn't know"]
    if k == "route":
        if f["destination"] != c["to"] or f["origin"] != c["from"]:
            out.append(f"says {c['flight']} goes {c['from']}-{c['to']} but it goes {f['origin']}-{f['destination']}")
    elif k == "flight_status":
        if f["status"] != c["status"]:
            out.append(f"says {c['flight']} is {c['status']} but flight operations says {f['status']}")
    elif k == "departure":
        if f["dep_time"] != c["time"] or f["dep_date"] != c["date"]:
            out.append(f"says {c['flight']} leaves {c['time']} {c['date']} but it is {f['dep_time']} {f['dep_date']}")
    elif k == "waiting_at_diversion":
        if not f.get("waiting_at_diversion"):
            out.append(f"says {c['flight']} is waiting at its diversion airport, but flight operations does not")
    elif k == "airborne":
        if not f.get("airborne"):
            out.append(f"says the passenger is in the air on {c['flight']}, but flight operations does not")
    elif k == "diverted_to":
        if f.get("diverted_to") != c["to"]:
            out.append(f"says {c['flight']} diverted to {c['to']} but flight operations says {f.get('diverted_to')}")
    elif k == "booked_on":
        trip = trips.get(pid)
        if trip is None or list(trip["itinerary"]) != list(c["itinerary"]):
            out.append(f"says the passenger is booked on {c['itinerary']} but the trip record says "
                       f"{None if trip is None else list(trip['itinerary'])}")
    elif k == "trip_status":
        trip = trips.get(pid)
        if trip is None or trip["status"] != c["status"]:
            out.append(f"says the trip is {c['status']} but the trip record says {None if trip is None else trip['status']}")
    elif k == "cause":
        sus = (pic.get("suspected") or {})
        conf = (sus.get("type") or {}).get("confidence")
        is_weather = (sus.get("type") or {}).get("value") == "weather" and conf is not None and conf >= settings()["cause_confidence_min"]
        if (c["word"] == templates()["cause"]["weather"]) != is_weather:
            out.append(f"names the cause '{c['word']}' but Situation's picture supports "
                       f"'{templates()['cause']['weather' if is_weather else 'other']}'")
    elif k == "end":
        sus = pic.get("suspected") or {}
        conf = (sus.get("expected_end_likely") or {}).get("confidence")
        low = conf is None or conf < settings()["end_confidence_low"]
        if c["phrase"] == "ended":
            if (pic.get("facts") or {}).get("status") != "recovery":
                out.append("says flights are moving again, but Situation does not report recovery")
        elif c["phrase"] == "unknown_passed":
            want = (sus.get("expected_end_likely") or {}).get("value")
            if want is None or world.get("now") is None or _dt(want) > world["now"]:
                out.append("says there is no reliable estimate, but Situation's estimate has not passed")
        elif c["phrase"] == "unknown":
            if not low:
                out.append("says there is no reliable estimate, but Situation has one")
        else:
            want = sus.get("expected_end_likely" if c["phrase"] == "around" else "expected_end_earliest", {}).get("value")
            if low or want is None or _hhmm(want) != c["time"]:
                out.append(f"gives an end time {c['time']} that Situation's picture doesn't support")
    elif k == "counts":
        have = world.get("counts", {})
        for key in ("cancelled", "delayed", "diverted"):
            if have.get(key) != c.get(key):
                out.append(f"says {c.get(key)} {key} but flight operations shows {have.get(key)}")
    elif k == "wrong_shown":
        if world.get("was_shown_wrong", {}).get((pid, c["channel"])) != c["status"]:
            out.append(f"corrects a {c['channel']} that did not show '{c['status']}'")
    elif k in ("name", "next_update", "city"):
        pass                                             # checked below (names) or our own commitment (next update)
    else:
        out.append(f"unknown claim '{k}'")
    return out


def _dt(v):
    from datetime import datetime
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def _hhmm(v) -> str:
    from datetime import datetime
    from backend.settings import TZ
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return v.astimezone(TZ).strftime("%H:%M")


def message_problems(m: dict, world: dict) -> list[str]:
    """Everything wrong with one message (empty = it may be sent)."""
    T, S = templates(), settings()
    text, pid, channel, claims = m.get("text") or "", m.get("passenger_id"), m.get("channel"), m.get("claims") or []
    staff = m.get("type") == "staff_note"
    out = []
    for c in claims:
        out += _claim_problems(c, pid, world)
    # every fact in the text must come from a claim
    cities = world.get("cities", {})
    allowed = {"flights": set(), "times": set(), "dates": set(), "cities": set(), "numbers": set(), "statuses": set()}
    for c in claims:
        for key in ("flight",):
            if c.get(key):
                allowed["flights"].add(c[key])
        for fid in c.get("itinerary") or []:
            allowed["flights"].add(fid)
        for key in ("time",):
            if c.get(key):
                allowed["times"].add(c[key])
        if c.get("date"):
            allowed["dates"].add(c["date"])
        for key in ("from", "to"):
            if c.get(key):
                allowed["cities"].add(cities.get(c[key], c[key]))
        for key in ("cancelled", "delayed", "diverted"):
            if c.get("kind") == "counts" and c.get(key) is not None:
                allowed["numbers"].add(str(c[key]))
                allowed["statuses"].add(key)             # "3 cancelled, 7 delayed, 2 diverted": the counts back these words
        if c.get("status"):
            allowed["statuses"].add(T["status_words"].get(c["status"], c["status"]))
        if c.get("kind") == "end" and c.get("phrase") == "ended":
            allowed["statuses"].add("delayed")          # "flights are moving again, but some are still delayed" (recovery)
    for fid in FLIGHT.findall(text):
        if fid not in allowed["flights"]:
            out.append(f"mentions {fid} without a confirmed fact behind it")
    for tm in TIME.findall(text):
        if tm not in allowed["times"]:
            out.append(f"mentions the time {tm} without a confirmed fact behind it")
    for d in DATE.findall(text):
        if d not in allowed["dates"]:
            out.append(f"mentions the date {d} without a confirmed fact behind it")
    for name in sorted(set(cities.values())):
        if _has(text, name) and name not in allowed["cities"]:
            out.append(f"mentions {name} without a confirmed fact behind it")
    for n in NUMBER.findall(FLIGHT.sub("", TIME.sub("", DATE.sub("", text)))):
        if n not in allowed["numbers"]:
            out.append(f"mentions the number {n} without a confirmed fact behind it")
    for w in STATUS_WORDS:
        if _has(text, w) and w not in allowed["statuses"] and not (w == "delayed" and "delayed" in allowed["statuses"]):
            out.append(f"says '{w}' without a confirmed fact behind it")
    # promises, blame, sensitive details
    approved = set(world.get("approved", ()))
    check_text = text.replace(T["staff_allowed_sentence"], "") if staff else text
    for group in ("promises", "blame", "sensitive"):
        for w in T["forbidden"][group]:
            if _has(check_text, w) and not (group == "promises" and m.get("backed_by") in approved and m.get("backed_by")):
                out.append(f"contains '{w}' ({'a promise nobody approved' if group == 'promises' else group})")
    if PAX.search(text):
        out.append("contains a passenger id")
    # names: only the recipient's first name, only in the app, only for passengers the agent says it is working on
    names = [c for c in claims if c.get("kind") == "name"]
    for c in names:
        if channel != "app":
            out.append("uses a name outside the app")
        elif pid not in world.get("working_on", ()):
            out.append("uses a name for a passenger the agent did not declare it is working on")
        elif world.get("first_names", {}).get(pid) != c.get("value"):
            out.append("uses a name that is not the passenger's")
    without_places = text
    for city in allowed["cities"]:                       # e.g. "San Diego" in one message while a "Diego" is also messaged
        without_places = without_places.replace(city, " ")
    for other in world.get("other_names", ()):
        if not other:
            continue
        if re.search(rf"\b{re.escape(other)}\b", without_places) and not any(c.get("value") == other for c in names):
            out.append(f"contains another person's name ({other})")
    # the four parts, length, plain English
    parts = m.get("parts") or {}
    for part in ("know", "dont_know", "do_now", "next"):
        if not (parts.get(part) or "").strip() or parts[part].strip() not in text:
            out.append(f"is missing the '{part}' part")
    if channel == "sms" and len(text) > S["sms_max_chars"]:
        out.append(f"is {len(text)} characters; an SMS may have {S['sms_max_chars']}")
    if reading_ease(text) < S["readability_min"]:
        out.append(f"is not plain English (reading ease {reading_ease(text):.0f}, needs {S['readability_min']})")
    if any(c.get("kind") == "end" and c.get("phrase") == "around" for c in claims) and "this may change" not in text:
        out.append("gives an expected end without saying it may change")
    facts, trip = m.get("trip_facts"), world.get("confirmed_trips", {}).get(pid)
    if facts and pid is not None and (trip is None or list(trip["itinerary"]) != list(facts["itinerary"])
                                      or trip["status"] != facts["status"]):
        out.append("the trip changed after this message was written")
    return out


def reasons(proposal: dict, world: dict) -> list[str]:
    """For the rule check: every message of a message proposal (a mass message has many)."""
    msgs = proposal.get("messages")
    if msgs is None:
        if proposal.get("action") in MESSAGE_ACTIONS and "message" not in proposal:
            return ["a message action with no message to check"]
        return []                                    # an older-style single "message" is checked by check.py itself
    if not msgs:
        return ["a message proposal has no message"]
    out = []
    for m in msgs:
        out += [f"{m.get('passenger_id') or 'staff'} ({m.get('channel')}): {p}" for p in message_problems(m, world)]
    return out
