"""What a passenger is owed, split into the legal floor, Southwest's commitments and optional goodwill."""


def compensation_for(p: dict, rules: dict) -> dict:
    """p keys: cause, delay_min (arrival delay), cancelled, rebooking_accepted, international,
    overnight, lives_locally. For a delayed checked bag: bag_delivery_hours (hours after arrival until the bag was
    delivered; None if it came with the passenger), bag_fee_paid (did they pay a checked-bag fee), bag_report_filed
    (mishandled baggage report, default True), flight_hours (scheduled flight length, picks the 15 h or 30 h limit).
    """
    c = rules["compensation"]
    cause = p["cause"]
    if cause in c["controllable_causes"]:
        controllable = True
    elif cause in c["uncontrollable_causes"]:
        controllable = False
    else:
        controllable = None  # unknown cause: a human must classify it before anything is promised

    delay = p.get("delay_min", 0)
    sig = c["significant_delay_international_min"] if p.get("international") else c["significant_delay_domestic_min"]
    significant = p.get("cancelled", False) or delay >= sig

    out = {"controllable": controllable, "items": [], "total_usd": 0, "needs_human": controllable is None}

    def add(kind, item, usd=0, why=""):
        out["items"].append({"kind": kind, "item": item, "usd": usd, "why": why})
        out["total_usd"] += usd

    # 1. Legal floor: everyone, any cause
    if significant and not p.get("rebooking_accepted", False):
        add("legal", "cash refund of the ticket", 0, "cancelled or significantly delayed and rebooking declined")

    # 1b. Legal floor, delayed bags (14 CFR 260.5): refund the checked-bag fee if the bag is late enough
    hours = p.get("bag_delivery_hours")
    if hours is not None and p.get("bag_fee_paid") and p.get("bag_report_filed", True):
        if not p.get("international"):
            limit = c["bag_refund_hours_domestic"]
        elif p.get("flight_hours", 0) > c["long_flight_hours"]:
            limit = c["bag_refund_hours_international_long"]
        else:
            limit = c["bag_refund_hours_international"]
        if hours > limit:
            add("legal", "refund of the checked-bag fee", p.get("bag_fee_usd", c["bag_fee_usd"]),
                f"bag not delivered within {limit} h of arrival")

    # 2. Southwest commitments: controllable causes only
    if controllable and delay >= c["controllable_delay_min"]:
        add("southwest", "meal voucher", c["meal_usd"], "controllable delay of 3+ hours")
        add("southwest", "travel voucher", c["voucher_usd"], "controllable delay of 3+ hours")
    if controllable and p.get("overnight") and c["hotel_needed_if_overnight"] and not p.get("lives_locally"):
        add("southwest", "hotel room and ground transport", 0, "controllable overnight away from home")

    # 3. Goodwill: optional, uncontrollable cases with a long wait, always approved by a supervisor
    if controllable is False and delay >= c["controllable_delay_min"]:
        add("goodwill", "optional travel credit", c["goodwill_usd"], "not owed; supervisor decides")
        out["needs_human"] = True
    return out
