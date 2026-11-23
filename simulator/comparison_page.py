"""Self-contained comparison section shared by simulator and chaos pages."""
import html
import json

METRICS = [
    ("peak_passengers_disagreeing", "Peak passengers with a wrong screen"),
    ("avg_passengers_disagreeing", "Average passengers with a wrong screen"),
    ("passenger_minutes_of_disagreement", "Passenger-minutes wrong"),
    ("critical_passenger_minutes_wrong", "Critical passenger-minutes wrong"),
    ("all_screens_agree_after_stop_min", "Minutes after stop until all screens first agree"),
    ("all_screens_stay_correct_after_stop_min", "Minutes after stop until all screens stay correct for the rest of the day"),
    ("lost_updates_wrong_end", "Lost updates still wrong at day end"),
    ("catch_up_median_min", "Median catch-up minutes (completed trip versions)"),
    ("catch_up_worst_min", "Worst catch-up minutes (completed trip versions)"),
    ("versions_still_wrong_end", "Trip versions still wrong at day end"),
    ("superseded_versions", "Trip versions replaced before all screens caught up"),
    ("pushes_per_passenger_avg", "Extra correction pushes per passenger: average"),
    ("pushes_per_passenger_max", "Extra correction pushes per passenger: maximum"),
    ("pushes_per_channel", "Extra correction pushes by channel"),
    ("all_pushes_per_passenger_avg", "All trip updates per passenger: average"),
    ("all_pushes_per_passenger_max", "All trip updates per passenger: maximum"),
    ("all_pushes_per_channel", "All trip updates by channel"),
    ("fixes_failed", "Corrections with a failed delivery component"),
    ("failed_fixes_retried", "Failed corrections sent again (same screen or another channel, same trip version)"),
    ("failed_fixes_resent", "Failed corrections resent on another channel (same trip version)"),
]


def hm(t):
    return "not reached" if t is None else f"{t // 60:02}:{t % 60:02}"


def esc(value):
    return html.escape(json.dumps(value) if isinstance(value, dict) else str(value))


def show(value):
    """A metric as a person reads it."""
    if value is None:
        return "never (some screens stayed wrong)"
    if isinstance(value, dict):
        return ", ".join(f"{CHANNEL_NAMES.get(k, k)} {v}" for k, v in value.items())
    return f"{value:,}" if isinstance(value, int) else str(value)


CHANNEL_NAMES = {"app": "app", "sms": "SMS", "gate_screen": "gate", "call_center": "call center"}


def maya_table(today, copilot):
    """Each of Maya's trip changes, and when each of her four screens caught up, in both modes."""
    rows = []
    for a in today["maya_versions"]:
        b = next((v for v in copilot["maya_versions"] if v["changed_at"] == a["changed_at"]), {"catch_up": {}})
        cells = "".join(f'<td>{hm(a["catch_up"].get(ch))} &rarr; <b>{hm(b["catch_up"].get(ch))}</b></td>' for ch in CHANNEL_NAMES)
        rows.append(f'<tr><td>{hm(a["changed_at"])}: {esc(a["truth"]["status"])} {esc(" > ".join(a["truth"]["itinerary"]))}</td>'
                    f'{cells}</tr>')
    return ('<h3>Maya Torres: when each screen caught up (today &rarr; <b>with co-pilot</b>)</h3><table><tr><th>Trip change</th>'
            + "".join(f"<th>{n}</th>" for n in CHANNEL_NAMES.values()) + "</tr>" + "".join(rows) + "</table>")


def section(today, copilot):
    rows = "".join(f"<tr><th>{label}</th><td>{esc(show(today.get(key)))}</td>"
                   f"<td>{esc(show(copilot.get(key)))}</td></tr>" for key, label in METRICS)
    series = [today["comparison_series"], copilot["comparison_series"]]
    lo, hi = series[0]["t"][0], series[0]["t"][-1]
    peak = max(1, *(max(s["pax_disagreeing"], default=0) for s in series))
    lines = []
    for s, color in zip(series, ("#b23a48", "#087f8c")):
        points = " ".join(f"{45 + (t-lo)*900/max(1,hi-lo):.1f},{215-v*180/peak:.1f}" for t, v in zip(s["t"], s["pax_disagreeing"]))
        lines.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
    chart = '<svg viewBox="0 0 980 250" role="img" aria-label="Passengers with wrong screens, both modes">' + \
        f'<text x="5" y="30">{peak}</text><text x="20" y="215">0</text><text x="45" y="240">{hm(lo)}</text>' + \
        f'<text x="900" y="240">{hm(hi)}</text>' + "".join(lines) + '</svg>'
    by_mode = []
    for mode in (today, copilot):
        body = []
        for row in mode["maya_minutes"]:
            cells = [f'<td>{hm(row["t"])}</td>']
            for ch in ("app", "sms", "gate_screen", "call_center"):
                v = row["channels"].get(ch)
                if v is None:                                  # a screen this passenger does not have
                    cells.append("<td>-</td>")
                    continue
                wrong = any(v[k] != row["truth"][k] for k in ("itinerary", "status", "destination"))
                cells.append(f'<td style="background:{"#fff0ef" if wrong else "#e7f5ef"}">{esc(v["status"])} '
                             f'{esc(" > ".join(v["itinerary"]))}</td>')
            body.append('<tr>' + ''.join(cells) + '</tr>')
        by_mode.append('<table><tr><th>Time</th><th>App</th><th>SMS</th><th>Gate</th><th>Call center</th></tr>' + ''.join(body) + '</table>')
    return '<section id="comparison"><h2>Today vs with co-pilot</h2><p><b style="color:#b23a48">Today</b> / ' \
        '<b style="color:#087f8c">With co-pilot</b>: passengers whose screens disagree with confirmed truth.</p>' + chart + \
        '<table><tr><th>Measure</th><th>Today</th><th>With co-pilot</th></tr>' + rows + '</table>' + maya_table(today, copilot) + \
        '<p>Catch-up timing covers completed versions only. Replaced and unfinished versions are listed separately. ' \
        'Push counts include extra corrections and resends; ordinary system copies are unchanged. A merged correction has ' \
        'separate trip and baggage delivery components, each subject to normal delays and failures.</p>' + \
        '<details><summary>Maya minute by minute — today (left), with co-pilot (right)</summary>' \
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;overflow:auto;max-height:650px">' + \
        ''.join(by_mode) + '</div></details></section>'
