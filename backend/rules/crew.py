"""Crew hour checks: pilots (14 CFR part 117), flight attendants (14 CFR 121.467) and ground staff (our assumptions).

Each check follows the same pattern as safety.py: it returns None when the option passes, or a short plain-English reason.
If an input is present but None (we don't know), the check FAILS: unknown counts as unsafe.
"""
from datetime import datetime


def to_min(x) -> int:
    """Minutes after midnight from 'HH:MM', a datetime, or a number of minutes."""
    if isinstance(x, datetime):
        return x.hour * 60 + x.minute
    if isinstance(x, str):
        h, m = x.split(":")
        return int(h) * 60 + int(m)
    return int(x)


def in_window(minute: int, start, end) -> bool:
    """Is a time of day inside a window that may cross midnight (e.g. 23:30 to 06:30)?"""
    s, e = to_min(start), to_min(end)
    return s <= minute <= e if s <= e else (minute >= s or minute <= e)


# ------------------------------------------------------------------ pilots

def fdp_limit_hours(table: dict, start, segments: int, acclimated: bool = True) -> float:
    """Table B: the longest scheduled duty period (hours) for a duty period that starts at `start` with `segments` flights."""
    m = to_min(start)
    for row in table["rows"]:
        if to_min(row["from"]) <= m <= to_min(row["to"]):
            hours = row["hours"][min(max(int(segments), 1), len(row["hours"])) - 1]
            return hours - (0 if acclimated else table["not_acclimated_reduction_min"] / 60)
    raise ValueError(f"no Table B row for start time {start}")


def pilot_fdp_limit(o, lim):
    """Table B duty period limit, plus the rules for extending it (14 CFR 117.13, 117.19)."""
    c = o.get("crew")
    if not isinstance(c, dict) or "planned_fdp_h" not in c:
        return None
    p = lim["pilot"]
    if c["planned_fdp_h"] is None or c.get("fdp_start") is None or c.get("segments") is None:
        return "pilot duty period start, flights or length is unknown"
    limit = fdp_limit_hours(p["fdp_table_b"], c["fdp_start"], c["segments"], c.get("acclimated", True))
    over = c["planned_fdp_h"] - limit
    if over <= 0:
        return None
    if c.get("airborne"):                                        # after takeoff: only as far as needed to land safely
        return None if c.get("needed_to_land_safely") else \
            f"duty period would be {over:.1f} h over the limit; after takeoff it can only be extended as needed to land safely"
    if over > p["fdp_extension_before_takeoff_max_h"]:
        return f"duty period would be {over:.1f} h over the {limit:g} h limit; an extension can't be more than {p['fdp_extension_before_takeoff_max_h']} h"
    if over * 60 > p["fdp_extension_once_over_min"] and c.get("extension_used_since_rest"):
        return "an extension of more than 30 min was already used since the last full rest; it can be used only once"
    used = c.get("fdp_hours_168h")
    if used is not None and used + c["planned_fdp_h"] > p["fdp_hours_168h_max"]:
        return "an extension would push the crew over the 60 hours in 168 hours duty limit"
    if not c.get("extension_approved"):
        return (f"duty period would be {over * 60:.0f} min over the {limit:g} h limit; only the captain and the airline can "
                f"extend it (human only)")


def pilot_cumulative_limits(o, lim):
    """Weekly and longer limits on duty and flight time, and the 30 hours free in 168 (14 CFR 117.23, 117.25(b))."""
    c = o.get("crew")
    if not isinstance(c, dict):
        return None
    p = lim["pilot"]
    specs = [("fdp_hours_168h", "fdp_hours_168h_max", "planned_fdp_h", "duty hours in 168 h"),
             ("fdp_hours_672h", "fdp_hours_672h_max", "planned_fdp_h", "duty hours in 672 h"),
             ("flight_hours_672h", "flight_hours_672h_max", "planned_flight_h", "flight hours in 672 h"),
             ("flight_hours_365d", "flight_hours_365d_max", "planned_flight_h", "flight hours in 365 days")]
    for used_key, max_key, planned_key, label in specs:
        if used_key not in c:
            continue
        if c[used_key] is None:
            return f"{label} so far is unknown"
        total = c[used_key] + (c.get(planned_key) or 0)
        if total > p[max_key]:
            return f"crew would reach {total:g} {label} (limit {p[max_key]})"
    if "longest_free_stretch_168h" in c:
        free = c["longest_free_stretch_168h"]
        if free is None or free < p["free_hours_in_168h"]:
            return f"crew has not had {p['free_hours_in_168h']} h in a row off duty in the last 168 h" if free is not None \
                else "time off duty in the last 168 h is unknown"


def crew_available(o, lim):
    """Only crews marked on_duty or reserve can be assigned (sick, fatigued and on-leave crews never are)."""
    c = o.get("crew")
    if not isinstance(c, dict) or "availability" not in c:
        return None
    if c["availability"] not in lim["limits_crew"]["availability_usable"]:
        return f"crew is marked '{c['availability']}' and can't be assigned" if c["availability"] else "crew availability is unknown"


def pilot_not_fatigued(o, lim):
    """A pilot who reported being too fatigued cannot be scheduled or kept on duty (14 CFR 117.5)."""
    c = o.get("crew")
    if isinstance(c, dict) and "fatigue_reported" in c and c["fatigue_reported"] is not False:
        return "a pilot reported being too fatigued" if c["fatigue_reported"] else "fatigue status is unknown"


# ------------------------------------------------------------------ flight attendants

def fa_duty_limit_hours(fa_rules: dict, extra_fas: int, outside_48: bool = False) -> int:
    by_extra = fa_rules["duty_max_h_by_extra_fas"]
    extra = min(max(int(extra_fas), 0), 3)
    if extra == 0:
        return fa_rules["duty_max_h"]
    if extra == 3 and not outside_48:          # 18-20 h needs a flight outside the 48 contiguous states
        extra = 2
    return by_extra[extra]


def fa_duty_limit(o, lim):
    """Flight attendant duty period limit (14 CFR 121.467(b)(1), (4)-(6)), with the beyond-our-control exception (b)(14)."""
    f = o.get("fa")
    if not isinstance(f, dict) or "duty_h" not in f:
        return None
    if f["duty_h"] is None:
        return "flight attendant duty length is unknown"
    limit = fa_duty_limit_hours(lim["fa"], f.get("extra_fas", 0), f.get("outside_48", False))
    if f["duty_h"] <= limit:
        return None
    sched = f.get("scheduled_duty_h")
    if f.get("beyond_carrier_control") and sched is not None and sched <= limit and lim["fa"]["beyond_control_exception"]:
        return None                                              # scheduled inside the limit; weather etc. pushed it over
    return f"flight attendants would be on duty {f['duty_h']:g} h (limit {limit} h with {f.get('extra_fas', 0)} extra)"


def fa_rest(o, lim):
    """Rest after a flight attendant duty period, and 24 hours off in 7 days (14 CFR 121.467(b)(2), (7), (13))."""
    f = o.get("fa")
    if not isinstance(f, dict):
        return None
    r = lim["fa"]
    if "rest_h" in f:
        prev = f.get("previous_duty_h")
        if f["rest_h"] is None or prev is None:
            return "flight attendant rest or the duty before it is unknown"
        need = r["rest_after_14h_or_less_h"] if prev <= r["duty_max_h"] else r["rest_after_over_14h_h"]
        if f["rest_h"] < need:
            return f"flight attendants had {f['rest_h']:g} h rest after a {prev:g} h duty (need {need} h)"
    if "longest_off_h_7d" in f:
        off = f["longest_off_h_7d"]
        if off is None or off < r["weekly_off_h"]:
            return f"flight attendants have not had {r['weekly_off_h']} h off in the last 7 days" if off is not None \
                else "time off in the last 7 days is unknown"


# ------------------------------------------------------------------ ground staff (no federal limit: our assumptions)

def staff_view(row: dict, now) -> dict:
    """Turn a row of the staff table (shift_start, shift_end, last_break; datetimes) into the numbers the check needs."""
    return {"shift_min": int((row["shift_end"] - row["shift_start"]).total_seconds() // 60),
            "min_since_break": int((now - (row.get("last_break") or row["shift_start"])).total_seconds() // 60)}


def ground_staff_limits(o, lim):
    s = o.get("staff")
    if not isinstance(s, dict):
        return None
    g = lim["ground_staff"]
    checks = [("shift_min", lambda v: v > g["max_shift_min"], f"shift would be longer than {g['max_shift_min']} min"),
              ("min_since_break", lambda v: v > g["break_interval_min"], f"no break for more than {g['break_interval_min']} min"),
              ("planned_break_min", lambda v: v < g["break_length_min"], f"break shorter than {g['break_length_min']} min"),
              ("gap_min", lambda v: v < g["min_gap_between_shifts_min"], f"less than {g['min_gap_between_shifts_min']} min since the last shift"),
              ("consecutive_days", lambda v: v > g["max_consecutive_days"], f"more than {g['max_consecutive_days']} days in a row")]
    for key, bad, text in checks:
        if key in s:
            if s[key] is None:
                return f"staff {key} is unknown"
            if bad(s[key]):
                return f"staff member: {text}"
