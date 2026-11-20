"""The safety ladder: remove unsafe options, then rank what's left.

An option is a plain dict describing one possible move (hold, divert, delay, cancel, swap crew...).
Each check reads only the keys it needs. If a key is missing, that check doesn't apply to the option.
If a key is present but its value is None (we don't know), the check FAILS: unknown counts as unsafe.
"""
from datetime import datetime

UNKNOWN = "unknown"


def _need(option, *path):
    """Fetch a nested value. Returns (applies, value)."""
    cur = option
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return False, None
        cur = cur[p]
    return True, cur


# Each check returns None when the option passes, or a short plain-English reason when it fails.

def tarmac_limit(o, lim):
    applies, minutes = _need(o, "tarmac_min")
    if not applies:
        return None
    if minutes is None:
        return "time on the tarmac is unknown"
    limit = lim["tarmac_international_min"] if o.get("international") else lim["tarmac_domestic_min"]
    if minutes >= limit:
        return f"passengers would sit on the tarmac {minutes} min (limit {limit})"


def tarmac_food_water(o, lim):
    """Law (14 CFR 259.4): food and water by 2 hours. A plan with 2+ hours on the tarmac needs a food-and-water plan."""
    applies, minutes = _need(o, "tarmac_min")
    if not applies or minutes is None:          # unknown time is already stopped by tarmac_limit
        return None
    if minutes >= lim["tarmac_food_water_min"] and not o.get("food_water_plan"):
        return (f"passengers would sit on the tarmac {minutes} min and there is no food-and-water plan "
                f"(required by {lim['tarmac_food_water_min']} min)")


def ramp_closed_for_lightning(o, lim):
    if not o.get("needs_ramp"):
        return None
    applies, open_ = _need(o, "ramp_open")
    if applies and open_ is not True:
        return "needs ramp workers outside while the ramp is closed"


def maintenance_due(o, lim):
    applies, due = _need(o, "aircraft", "maint_due_at")
    if not applies or due is None:
        return None
    end = o.get("flight_end")
    if end is None:
        return "flight end time is unknown, can't confirm maintenance is not due"
    if isinstance(due, datetime) and due <= end:
        return f"aircraft maintenance is due at {due:%H:%M}, before the plan ends"


def needs_inspection(o, lim):
    applies, flag = _need(o, "aircraft", "needs_inspection")
    if applies and flag is not False:
        return "aircraft needs an inspection first" if flag else "aircraft inspection status is unknown"


def fuel_reserve(o, lim):
    applies, extra = _need(o, "aircraft", "fuel_minutes_extra")
    if not applies:
        return None
    if extra is None:
        return "fuel on board is unknown"
    # hold_minutes: planned holding. minutes_to_alternate: extra flying beyond the planned destination.
    # The legal reserve is already on board and is not part of fuel_minutes_extra.
    left = extra - o.get("hold_minutes", 0) - o.get("minutes_to_alternate", 0)
    if left < lim["fuel_buffer_min"]:
        return f"only {left} min of spare holding fuel would be left (need {lim['fuel_buffer_min']})"


def station_has_gate(o, lim):
    applies, gates = _need(o, "station", "gates_free")
    if applies and (gates is None or gates < 1):
        return "no free gate at the station" if gates is not None else "free gates at the station are unknown"


def customs_for_international(o, lim):
    if not o.get("international"):
        return None
    applies, customs = _need(o, "station", "has_customs")
    if applies and customs is not True:
        return "international flight sent to a station without customs"


def crew_duty_left(o, lim):
    applies, left = _need(o, "crew", "duty_left_min")
    if not applies:
        return None
    if left is None:
        return "crew duty time left is unknown"
    planned = o.get("planned_duty_min", 0)
    if left - planned < lim["crew_duty_buffer_min"]:
        return f"crew would end with {left - planned} min of duty left (need a {lim['crew_duty_buffer_min']} min buffer)"


def crew_rest(o, lim):
    applies, rest = _need(o, "crew", "rest_hours")
    if applies and (rest is None or rest < lim["pilot_min_rest_hours"]):
        return f"crew rest {rest} h is below the {lim['pilot_min_rest_hours']} h legal minimum" if rest is not None \
            else "crew rest is unknown"


def child_alone_international(o, lim):
    rule = lim["child_alone"]
    if not rule["international_allowed"] and o.get("international") and o.get("pax", {}).get("child_alone", 0) > 0:
        return "children flying alone can't be booked on international flights"


def child_alone_no_connection(o, lim):
    """Southwest: a child flying alone goes nonstop or on the same plane. Never rebook onto a connection."""
    rule = lim["child_alone"]
    if not rule["nonstop_or_same_plane_only"] or not o.get("pax", {}).get("child_alone", 0):
        return None
    applies, changes = _need(o, "plane_changes")
    if applies and (changes is None or changes > 0):
        return "children flying alone can't be put on a connection (nonstop or same plane only)" \
            if changes is not None else "plane changes for children flying alone are unknown"


def child_alone_age(o, lim):
    """Southwest: unaccompanied minor service is for ages 5-11."""
    rule = lim["child_alone"]
    ages = o.get("pax", {}).get("child_alone_ages")
    if ages is None:
        return None
    if any(a is None or not rule["min_age"] <= a <= rule["max_age"] for a in ages):
        return f"children flying alone must be {rule['min_age']}-{rule['max_age']} years old"


def wheelchair_help_available(o, lim):
    need = o.get("pax", {}).get("wheelchair", 0)
    if not need:
        return None
    applies, staff = _need(o, "station", "wheelchair_staff")
    if applies and (staff is None or staff < 1):
        return f"{need} wheelchair passengers but no wheelchair staff at the station"


def child_alone_has_plan(o, lim):
    if o.get("pax", {}).get("child_alone", 0) and not o.get("child_alone_plan"):
        return "children flying alone have no named adult or supervised plan"


CHECKS = {f.__name__: f for f in [
    tarmac_limit, tarmac_food_water, ramp_closed_for_lightning, maintenance_due, needs_inspection, fuel_reserve,
    station_has_gate, customs_for_international, crew_duty_left, crew_rest, child_alone_international,
    child_alone_no_connection, child_alone_age, wheelchair_help_available, child_alone_has_plan,
]}


def limits_for(rules: dict) -> dict:
    """Everything the checks read: the numbers in `limits` plus the `child_alone` section."""
    return {**rules["limits"], "child_alone": rules["child_alone"]}


def screen(options: list[dict], rules: dict) -> tuple[list[dict], list[dict]]:
    """Run every option down the ladder. Returns (kept, removed).

    Removed options say which rung stopped them and why, so a human can see what was ruled out.
    """
    lim = limits_for(rules)
    kept, removed = [], []
    for o in options:
        stopped = None
        for rung in rules["safety_ladder"]:
            if rung["kind"] != "filter":
                continue
            reasons = [r for name in rung["checks"] if (r := CHECKS[name](o, lim))]
            if reasons:
                stopped = {"option": o["id"], "rung": rung["rung"], "rung_name": rung["name"], "reasons": reasons}
                break
        (removed if stopped else kept).append(stopped or o)
    return kept, removed


def rank(options: list[dict], rules: dict) -> list[dict]:
    """Order safe options: best for the network first, then fewest people disrupted, then cheapest."""
    keys = [r["score"] for r in rules["safety_ladder"] if r["kind"] == "rank"]

    def sort_key(o):
        out = []
        for k in keys:
            v = o.get(k, 0)
            out.append(-v if k == "network_score" else v)
        return out
    return sorted(options, key=sort_key)


def decide(options: list[dict], rules: dict) -> dict:
    kept, removed = screen(options, rules)
    return {"ranked": rank(kept, rules), "removed": removed}
