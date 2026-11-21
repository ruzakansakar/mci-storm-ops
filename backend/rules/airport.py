"""Airport and station checks: curfews, TSA re-screening, Southwest staff hours, wheelchair waiting, lightning, tarmac exceptions.

Same pattern as safety.py: None means the option passes, otherwise a short plain-English reason.
Options use these keys: airport (code), operation ('departure'/'arrival'), local_time ('HH:MM', minutes or a datetime).
"""
from backend.rules.crew import in_window, to_min


def airport_curfew(o, lim):
    """No departures at SAN 23:30-06:30; at DCA only quiet-enough aircraft fly 22:00-06:59. Unlisted airports: no curfew."""
    if "airport" not in o or "operation" not in o:
        return None
    if o.get("local_time") is None:
        return "local time of the flight is unknown, can't check the airport's night rules"
    rule = lim["airports"]["curfews"].get(o["airport"])
    if not rule or rule["kind"] == "none" or not in_window(to_min(o["local_time"]), rule["from"], rule["to"]):
        return None
    if rule["kind"] == "no_departures":
        if o["operation"] == "departure":
            return f"{o['airport']} bans departures from {rule['from']} to {rule['to']}"
    elif rule["kind"] == "noise_limit":
        if o.get("meets_noise_rule") is not True:
            return f"{o['airport']} only allows aircraft that meet its noise rule from {rule['from']} to {rule['to']}"


def _open(hours: dict, minute: int) -> bool:
    return to_min(hours["open"]) <= minute < to_min(hours["close"]) if hours["close"] != "24:00" else True


def tsa_rescreening(o, lim):
    """People who get off outside the secure area must be screened again, so the TSA checkpoint has to be open."""
    if not o.get("deplaned_outside_secure") or not lim["airports"]["tsa_rescreen_if_deplaned_outside_secure"]:
        return None
    hours = lim["airports"]["tsa_checkpoint_hours"].get(o.get("airport"))
    if hours is None or o.get("local_time") is None:
        return "TSA checkpoint hours or the time are unknown, can't plan re-screening"
    if not _open(hours, to_min(o["local_time"])):
        return f"passengers would need TSA re-screening but the checkpoint at {o['airport']} is closed (open {hours['open']}-{hours['close']})"


def station_staffed(o, lim):
    """A plane that needs Southwest people on the ground must land where Southwest has staff at that hour."""
    if not o.get("needs_station_staff"):
        return None
    hours = lim["airports"]["southwest_staffed_hours"].get(o.get("airport"))
    if hours is None or o.get("local_time") is None:
        return "Southwest staff hours at this station or the time are unknown"
    if not _open(hours, to_min(o["local_time"])):
        return f"Southwest has no staff at {o['airport']} at that hour (staffed {hours['open']}-{hours['close']})"


def wheelchair_unattended(o, lim):
    """Nobody who asked for help may sit alone in a wheelchair they can't move for more than 30 minutes (14 CFR 382.103)."""
    pax = o.get("pax", {})
    if "wheelchair_unattended_min" not in pax:
        return None
    waited, limit = pax["wheelchair_unattended_min"], lim["wheelchair"]["unattended_max_min"]
    if waited is None:
        return "how long wheelchair passengers are left alone is unknown"
    if waited > limit and not pax.get("wheelchair_waived"):
        return f"a wheelchair passenger would be left alone {waited} min (limit {limit} unless they waive it)"


def tarmac_exception_needs_human(o, lim):
    """Keeping people on board past the tarmac limit needs a safety or air-traffic exception. Only the captain decides, and it is logged."""
    if not o.get("relies_on_tarmac_exception"):
        return None
    decision = o.get("tarmac_exception") or {}
    if decision.get("decided_by") != "captain" or decision.get("logged") is not True:
        return ("this plan keeps people on board past the tarmac limit; that needs a safety or air-traffic exception "
                "that only the captain decides and that must be logged")


def lightning_strike_inspection(o, lim):
    """A plane struck by lightning is inspected before its next flight."""
    ac = o.get("aircraft")
    if not isinstance(ac, dict) or "struck_by_lightning" not in ac or not lim["lightning"]["strike_needs_inspection"]:
        return None
    if ac["struck_by_lightning"] is None:
        return "whether the aircraft was struck by lightning is unknown"
    if ac["struck_by_lightning"] and ac.get("inspection_done") is not True:
        return "aircraft was struck by lightning and has not been inspected"
