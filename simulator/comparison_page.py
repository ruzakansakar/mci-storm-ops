"""Self-contained comparison section shared by simulator and chaos pages."""
import html
import json
from simulator.recovery_messages_page import section as recovery_messages_section

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


def situation_strip(copilot):
    """What Communication knew from Situation (each time it changed), and why fixes were held."""
    sit = copilot.get("copilot_situation")
    if not sit:
        return ""
    rows = "".join(
        f"<tr><td>{esc(x['t'][11:16])}</td><td>{esc(x['version'] or '-')}</td>"
        f"<td>{esc(x['status'] or '-')}</td><td>{'yes' if x['available'] else '<b>no (not running)</b>'}</td>"
        f"<td>{esc(', '.join(x['conflict_flights']) or 'none')}</td></tr>" for x in sit["timeline"])
    holds = ", ".join(f"{esc(k)}: {v}" for k, v in sorted((copilot.get("holds_by_reason") or {}).items())) or "none"
    conflicts = "".join(f"<li>{esc(c['flight_id'])}: {c['holds']} holds for {len(c['passengers'])} passenger(s), "
                        f"{hm(c['first'])}&ndash;{hm(c['last'])}</li>" for c in copilot.get("conflict_holds") or [])
    return ('<h3>What Communication knew from Situation</h3><p>Situation ran ' + esc(sit["situation_runs"]) + ' times and wrote '
            + esc(sit["situation_versions"]) + ' versions. Communication read the latest version at or before each minute (never '
            'a later one), only the fields its contract lists. Suspected causes and expected ends are kept as suspicions; they '
            'are not used in anything shown to passengers yet.</p><details><summary>Each change it saw</summary><table><tr><th>Time'
            '</th><th>Version</th><th>Status</th><th>Situation running</th><th>Flights with conflicting data</th></tr>' + rows +
            '</table></details><p><b>Fixes held</b>, by reason: ' + holds + '</p>' + (f'<ul>{conflicts}</ul>' if conflicts else ''))


def messages_section(copilot):
    """Phase 4b-3: what the co-pilot told passengers and staff (today mode sends none)."""
    if "messages_delivered" not in copilot:
        return ""
    maya = "".join(f"<tr><td>{hm(m['t'])}</td><td>{esc(m['channel'])}</td><td>{esc(m['type'].replace('_', ' '))}</td>"
                   f"<td>{esc(m['text'])}</td><td>{esc(m['decided'])}</td></tr>" for m in copilot.get("maya_messages") or [])
    notes = "".join(f"<tr><td>{hm(n['t'])}</td><td>{esc(n['text'])}</td></tr>" for n in copilot.get("staff_notes") or [])
    w, ci, nu = copilot["approval_waits_min"], copilot["cancellation_inform_min"], copilot["next_update_promises"]
    rows = [("Messages delivered", copilot["messages_delivered"]),
            ("By type", ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(copilot["messages_by_type"].items()))),
            ("Messages per passenger with an app or SMS: average / most", f"{copilot['messages_per_passenger_avg']} / "
                                                                         f"{copilot['messages_per_passenger_max']}"),
            ("Minutes from a cancellation or rebooking to a correct message reaching the passenger: median / worst",
             f"{ci['median']} / {ci['max']} ({ci['count']} passengers; never told: {ci['never']})"),
            ("Approval waits (minutes): median / longest", f"{w['median']} / {w['max']} ({w['count']} approvals)"),
            ("Blocked by the message fact check", copilot["messages_blocked"]),
            ("Not delivered because the trip changed first (then replaced)", copilot["messages_dropped_truth_changed"]),
            ("Withdrawn while waiting for approval (truth changed; redrafted)", copilot["messages_withdrawn"]),
            ("Delivery failed / resent on the other channel", f"{copilot['messages_failed_delivery']} / {copilot['messages_resent']}"),
            ("Corrections sent", copilot["corrections_sent"]),
            ("'Next update by' promises: kept / no longer needed / late or missed",
             f"{nu['kept']} / {nu['no_longer_needed']} / {nu['broken']}"),
            ("Promises of money, hotels, meals or guarantees found in sent messages", copilot["promises_found"])]
    table = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
    return ('<h3>Messages from the co-pilot</h3><p>Only confirmed facts, the Situation agent picture and approved decisions. Restating the '
            'truth is automatic; cancellations, rebookings, overnight notices, corrections, staff notes and any message to more than '
            '50 passengers wait for the (simulated) customer service supervisor.</p><table>' + table + '</table>'
            '<h3>Maya Torres: every message she received (with co-pilot)</h3><table><tr><th>Time</th><th>Channel</th><th>Type</th>'
            '<th>Exact text</th><th>Decided</th></tr>' + (maya or '<tr><td colspan="5">none</td></tr>') + '</table>'
            '<details><summary>The staff note (gate agents and call center) as it changed</summary><table><tr><th>Time</th>'
            '<th>Note</th></tr>' + (notes or '<tr><td colspan="2">none</td></tr>') + '</table></details>')


def network_section(copilot):
    """Phase 4d: the Network Recovery agent (advisory: it proposes, nothing is applied), next to what today's process did."""
    n = copilot.get("network")
    if not n:
        return ""
    crew = "".join(f"<tr><td>{esc(c['flight_id'])}</td><td>{'flight attendants' if c['role'] == 'fa' else 'pilots'}</td>"
                   f"<td>{esc(c['what_happened'])} at {esc(c['at'])}</td><td>{esc(c['first_warning'] or 'no warning')}</td>"
                   f"<td>{esc(c['lead_min'] if c['lead_min'] is not None else '-')}</td><td>{esc(c['best_option'] or '-')}</td></tr>"
                   for c in n["crew_timeouts"])
    fuel = "".join(f"<tr><td>{esc(f['flight_id'])}</td><td>{esc(f['first_warning'] or 'no warning')}</td>"
                   f"<td>{esc(f['must_leave_by'] or '-')}</td><td>{esc(f['diverted_at'])} to {esc(f['to'])}</td>"
                   f"<td>{esc(f['lead_min'] if f['lead_min'] is not None else '-')}</td><td>{esc(', '.join(f['safe_alternates']))}</td></tr>"
                   for f in n["fuel_diversions"])
    t, pl = n["today"], n["plan"]
    delays = ", ".join(f"{d['flight_id']} to {d['new_departure'][11:16]}" for d in pl["delays"]) or "none"
    vs = (f"<tr><th>Cancelled</th><td>{esc(', '.join(t['cancelled']) or 'none')}</td><td>{esc(', '.join(pl['cancel']) or 'none')}"
          f" (plan at {esc(pl['final_in_stop_at'])}; ever proposed: {esc(', '.join(pl['ever_proposed_to_cancel']) or 'none')})</td></tr>"
          f"<tr><th>Delayed to</th><td>as it happened</td><td>{esc(delays)}</td></tr>"
          f"<tr><th>Crew saves</th><td>-</td><td>{esc(', '.join(pl['crew_saves']) or 'none')}</td></tr>"
          f"<tr><th>Passengers stranded overnight (ESTIMATE, same method)</th><td>{t['estimate']['passengers_stranded_overnight']}"
          f" (actual day: {t['actual_passengers_stranded_overnight']} incl. diversions and missed connections)</td>"
          f"<td>{pl['estimate']['passengers_stranded_overnight']}</td></tr>")
    return ('<h3>Network Recovery (advisory: it proposes, nothing is applied)</h3>'
            f'<p>{n["proposals"]} proposals; illegal (blocked by the rule check): <b>{n["illegal_proposals"]}</b>. Every proposal would '
            'need a person: network operations for flights, crew scheduling for crews; divert and duty extensions are only asked.</p>'
            '<h4>Crew warnings vs real crew time-outs</h4><table><tr><th>Flight</th><th>Crew</th><th>What happened today</th>'
            '<th>First warning</th><th>Minutes ahead</th><th>Best legal option</th></tr>' + crew + '</table>'
            '<h4>The plan vs today</h4><table><tr><th></th><th>Today</th><th>The plan</th></tr>' + vs + '</table>'
            f'<p>Estimate method: {esc(pl["estimate"]["method"])}.</p>'
            '<h4>Hold or divert warnings (A31 = WN2031)</h4><table><tr><th>Flight</th><th>First warning</th><th>Must leave by</th>'
            '<th>Actually diverted</th><th>Minutes ahead</th><th>Safe alternates</th></tr>' + fuel + '</table>')


def care_section(copilot):
    """Phase 4e: the Passenger Care agent: who needed care overnight, hotels used, costs, Maya's care plan and message."""
    c = copilot.get("care")
    if not c:
        return ""
    rows = "".join(f"<tr><td>{esc(e['passenger_id'])}</td><td>{'home' if e['local'] else esc(e['hotel_name'] or '-')}</td>"
                   f"<td>{esc(e['hotel_status'] or '-')}</td><td>{esc(e['transport'] or '-')} ({esc(e['transport_status'] or '-')})</td>"
                   f"<td>{esc(e['compensation'] or '-')}</td></tr>" for e in c["overnight"])
    hotels = "".join(f"<tr><td>{esc(h['name'])}</td><td>{h['rooms_available']}</td><td>{h['held']}</td><td>{h['released']}</td>"
                     f"<td>{h['assigned']}</td></tr>" for h in c["hotels_used"])
    vul = "".join(f"<tr><td>{esc(v['passenger_id'])}</td><td>{esc(v['kind'].replace('_', ' '))}</td><td>{esc(v['plan'])}</td>"
                  f"<td>{'yes' if v['follows_the_rules'] else '<b>NO</b>'}</td></tr>" for v in c["vulnerable"])
    m = c.get("maya") or {}
    cost = c.get("costs") or {}
    return ('<h3>Passenger Care</h3>'
            f'<p>{c["proposals"]} proposals; rule-check blocks: {c["rule_check_blocks"]} (none applied). Rooms held '
            f'{c["rooms_held"]}, assigned {c["rooms_assigned"]}. Costs committed ${cost.get("committed_usd", 0):,} (pending '
            f'${cost.get("pending_usd", 0):,}) against a cap of ${cost.get("cap_usd", 0):,}. Still waiting for a person at day end: '
            f'{esc(", ".join(f"{k} {v}" for k, v in c["pending_at_day_end"].items()) or "nothing")}.</p>'
            '<h4>Maya Torres</h4><p>' + esc(f"Hotel: {m.get('hotel_name')} ({m.get('hotel_status')}, approved by "
                                             f"{(m.get('approved_by') or '-').replace('_', ' ')} after {m.get('waited_min')} min); "
                                             f"transport: {m.get('transport')}; compensation: {m.get('compensation')}") +
            '</p><p>Her overnight message: <i>' + esc(c.get("maya_overnight_message") or "none") + '</i></p>'
            '<h4>Overnight passengers</h4><table><tr><th>Passenger</th><th>Tonight</th><th>Room</th><th>Transport</th>'
            '<th>Compensation</th></tr>' + rows + '</table>'
            '<h4>Hotels used</h4><table><tr><th>Hotel</th><th>Free at start</th><th>Held</th><th>Released</th><th>Assigned</th></tr>'
            + hotels + '</table><h4>Children alone and wheelchair users</h4><table><tr><th>Passenger</th><th>Kind</th><th>Plan</th>'
            '<th>Follows the rules</th></tr>' + (vul or '<tr><td colspan="4">none on an affected flight</td></tr>') + '</table>')


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
        '<table><tr><th>Measure</th><th>Today</th><th>With co-pilot</th></tr>' + rows + '</table>' + maya_table(today, copilot) + situation_strip(copilot) + messages_section(copilot) + network_section(copilot) + care_section(copilot) + recovery_messages_section(copilot) + \
        '<p>Catch-up timing covers completed versions only. Replaced and unfinished versions are listed separately. ' \
        'Push counts include extra corrections and resends; ordinary system copies are unchanged. A merged correction has ' \
        'separate trip and baggage delivery components, each subject to normal delays and failures.</p>' + \
        '<details><summary>Maya minute by minute — today (left), with co-pilot (right)</summary>' \
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;overflow:auto;max-height:650px">' + \
        ''.join(by_mode) + '</div></details></section>'
