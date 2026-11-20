"""Show the rules in plain English and run the A31 example through them.

Run:  python -m scripts.rules_demo
"""
from datetime import datetime, timedelta

from backend.rules import load_rules
from backend.rules.autonomy import autonomy_for
from backend.rules.compensation import compensation_for
from backend.rules.safety import decide, limits_for, screen
from backend.settings import TZ


def main():
    r = load_rules()
    print("\nSAFETY LADDER (top wins; filter rungs remove options, rank rungs only order them)")
    for rung in r["safety_ladder"]:
        detail = ", ".join(rung["checks"]) if rung["kind"] == "filter" else f"order by {rung['score']}"
        print(f"  {rung['rung']}. {rung['name']:<15} {rung['kind']:<7} {detail}")

    print("\nWHO DECIDES")
    for action in r["autonomy"]:
        a = autonomy_for(action, r)
        print(f"  {action:<34} {a['level']:<15} {a['approver'] or ''}")

    print("\nA31 FROM DENVER, MCI UNDER A GROUND STOP (55 min of spare holding fuel, 4 h 20 min crew duty, 3 wheelchair)")
    noon = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
    base = {"international": False, "flight_end": noon + timedelta(hours=2),
            "crew": {"duty_left_min": 260, "rest_hours": 10}, "planned_duty_min": 90,
            "pax": {"wheelchair": 3, "child_alone": 0}, "pax_disrupted": 158, "cost_usd": 0}

    def opt(id_, hold_min, extra_min, station=None, score=5):
        o = dict(base, id=id_, hold_minutes=hold_min, minutes_to_alternate=extra_min, network_score=score,
                 aircraft={"needs_inspection": False, "fuel_minutes_extra": 55})
        if station:
            o["station"] = station
        return o

    result = decide([
        opt("Hold 45 min over MCI", 45, 0, score=9),
        opt("Hold 50 min over MCI", 50, 0, score=9),
        opt("Hold 30 min, then divert to St. Louis (15 min extra flying)", 30, 15,
            {"gates_free": 6, "has_customs": True, "wheelchair_staff": 6}),
        opt("Divert to Wichita", 0, 0, {"gates_free": 2, "has_customs": False, "wheelchair_staff": 2}, score=6),
        opt("Divert to Tulsa", 0, 0, {"gates_free": 0, "has_customs": False, "wheelchair_staff": 3}),
    ], r)
    for o in result["ranked"]:
        print(f"  KEEP    {o['id']}")
    for o in result["removed"]:
        print(f"  REMOVE  {o['option']} (rung {o['rung']}, {o['rung_name']}): {o['reasons'][0]}")
    print(f"  Who decides the diversion: {autonomy_for('divert_flight', r)['approver']}")

    print("\nOTHER RULES, ONE EXAMPLE EACH")
    kept, removed = screen([
        {"id": "Child alone on a nonstop", "pax": {"child_alone": 1}, "plane_changes": 0, "child_alone_plan": True},
        {"id": "Child alone on a connection via Denver", "pax": {"child_alone": 1}, "plane_changes": 1, "child_alone_plan": True},
        {"id": "2 h 10 min on the tarmac, no food plan", "tarmac_min": 130},
        {"id": "2 h 10 min on the tarmac, with food and water", "tarmac_min": 130, "food_water_plan": True},
    ], r)
    for o in kept:
        print(f"  KEEP    {o['id']}")
    for o in removed:
        print(f"  REMOVE  {o['option']} (rung {o['rung']}, {o['rung_name']}): {o['reasons'][0]}")
    bag = compensation_for({"cause": "weather", "bag_delivery_hours": 14, "bag_fee_paid": True}, r)
    print(f"  Checked bag delivered 14 h after a domestic flight landed (limit {r['compensation']['bag_refund_hours_domestic']} h): "
          + "; ".join(f"{i['item']} (${i['usd']}, {i['kind']})" for i in bag["items"]))

    print("\nWHAT MAYA IS OWED")
    for label, case in [("Storm (weather), 4 h late, takes the rebooking", {"cause": "weather", "delay_min": 240, "rebooking_accepted": True}),
                        ("Crew problem, 4 h late, takes the rebooking", {"cause": "crew", "delay_min": 240, "rebooking_accepted": True}),
                        ("Flight cancelled, she declines rebooking", {"cause": "weather", "cancelled": True, "rebooking_accepted": False})]:
        c = compensation_for(case, r)
        items = "; ".join(f"{i['item']} (${i['usd']}, {i['kind']})" if i["usd"] else f"{i['item']} ({i['kind']})"
                          for i in c["items"]) or "nothing owed"
        print(f"  {label}:\n      {items}")
    print()


if __name__ == "__main__":
    main()
