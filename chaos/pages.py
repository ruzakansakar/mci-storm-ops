"""Plain HTML pass/fail pages for chaos days (no server: open with a double-click).

reports/chaos/index.html      one row per day, totals and pass rates at the top
reports/chaos/<run_id>.html   what really happened (left) next to what the agent said (right), and every check
"""
import html
import json
from datetime import datetime

from backend.settings import ROOT, TZ
from chaos.judge import CHECKS

OUT = ROOT / "reports" / "chaos"
COLOR = {"PASS": "#2e7d32", "WARN": "#b26a00", "FAIL": "#c62828", "N/A": "#777"}
BG = {"PASS": "#e8f5e9", "WARN": "#fff4e0", "FAIL": "#fdecea", "N/A": "#f3f3f3"}
STYLE = """<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222;max-width:1250px}h1{margin:0 0 4px}
.sub{color:#666;font-size:13px}table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}
td,th{border-bottom:1px solid #e4e4e4;padding:6px 8px;text-align:left;vertical-align:top}th{background:#fafafa}
.v{font-weight:600;padding:2px 8px;border-radius:10px;display:inline-block}.box{border:1px solid #e4e4e4;border-radius:8px;padding:12px;margin:14px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.mark{background:#fff4e0}</style>"""


def _e(x):
    return html.escape(str(x))


def _badge(v):
    return f'<span class="v" style="color:{COLOR[v]};background:{BG[v]}">{_e(v)}</span>'


def _hm(t):
    if t is None:
        return "-"
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t.astimezone(TZ).strftime("%H:%M")


def write_day_page(conn, score: dict) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    truth, out = score["truth"], score["outputs"]
    real = []
    for i in truth["incidents"]:
        real.append((i["start"], f"<b>{_e(i['kind'].replace('_', ' '))} starts</b> (really: {_e(i['type'])}, "
                                 f"{'within' if i['controllable'] else 'outside'} Southwest's control)"))
        if i.get("end"):
            real.append((i["end"], f"<b>{_e(i['kind'].replace('_', ' '))} really ends</b>"))
        if i.get("ramp"):
            real.append((i["ramp"][0], "ramp closed for lightning"))
            real.append((i["ramp"][1], "ramp reopened"))
    for f in truth["feed_outages"]:
        real.append((f["start"], f'<span class="mark">injected: {_e(f["feed"])} goes silent ({_e(f["why"])})</span>'))
        real.append((f["end"], f'<span class="mark">{_e(f["feed"])} is back</span>'))
    for c in truth["conflicts"]:
        real.append((c["at"], f'<span class="mark">injected conflict: {_e(c["kind"].replace("_", " "))}</span>'))
    for x in truth.get("normal_variation", []):
        real.append((x["at"], f"normal delay of {_e(x['flight_id'])} (not an incident)"))
    for x in (truth.get("messy") or {}).get("late_events", []):
        real.append((x["at"], f'<span class="mark">injected: a {_e(x["type"])} delivered late at {_hm(x["delivered"])}</span>'))
    for t, f in truth["aftermath"]["cancelled"]:
        real.append((t, f"{_e(f)} cancelled"))
    said = []
    for v in out["versions"]:
        rng = f"{_hm(v['expected_end_earliest'])}-{_hm(v['expected_end_latest'])}" if v["expected_end_likely"] else "no end time"
        conf = f"{v['confidence']:.2f}" if v["confidence"] is not None else "-"
        said.append((v["assessed_at"].isoformat(), f"v{v['version']} <b>{_e(v['status'])}</b> | {_e(v['type'] or '-')} | confidence {conf} | "
                                                  f"end {_e(rng)}" + (f" (end confidence {v['end_confidence']})" if v["end_confidence"] else "")))
    for r in out["recs"]:
        said.append((r["created_at"].isoformat(), f"proposed {_e(', '.join(r['decision_ids'] or []))}: {_e(r['title'])}"
                                                  + (" (replaces an earlier one)" if r["supersedes"] else "")))
    for e in out["events"]:
        if e["type"] in ("human_decision_needed", "agent_error", "contract_violation"):
            said.append((e["occurred_at"].isoformat(), f"<b>{_e(e['type'].replace('_', ' '))}</b>: {_e(json.dumps(e['payload'])[:160])}"))
    rows = sorted([(t, "real", x) for t, x in real] + [(t, "said", x) for t, x in said], key=lambda r: (datetime.fromisoformat(r[0]), r[1]))
    table = "".join(f"<tr><td>{_hm(t)}</td><td>{x if k == 'real' else ''}</td><td>{x if k == 'said' else ''}</td></tr>" for t, k, x in rows)
    checks = "".join(f"<tr><td>{_e(c['name'])}</td><td>{_badge(c['result'])}</td><td>{_e(c['reason'])}</td></tr>" for c in score["checks"])
    page = f"""<!doctype html><meta charset="utf-8"><title>{_e(score['run_id'])}</title>{STYLE}
<p><a href="index.html">&larr; all chaos days</a></p>
<h1>{_e(score['run_id'])} {_badge(score['verdict'])}</h1>
<div class="sub">What happened: {_e(truth['summary'])}. Ingredients: {_e(', '.join(truth['ingredients']))}.
Replay it with: python -m chaos.run --seed {truth['chaos_seed']}</div>
<div class="box"><h3 style="margin-top:0">Checks</h3><table><tr><th>Check</th><th>Result</th><th>Why</th></tr>{checks}</table></div>
<div class="box"><h3 style="margin-top:0">Timeline</h3><div class="sub">Left: what really happened (from the sealed truth file; injected problems
highlighted). Right: what the agent said, when it said it. The agent never saw the left column.</div>
<table><tr><th>Time</th><th>What really happened</th><th>What the agent said</th></tr>{table}</table></div>"""
    path = OUT / f"{score['run_id']}.html"
    path.write_text(page, encoding="utf-8")
    return str(path)


def write_index(scores: list[dict], title: str = "Chaos days for the Situation agent") -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    n = len(scores)
    verdicts = {k: sum(s["verdict"] == k for s in scores) for k in ("PASS", "WARN", "FAIL")}
    rates = []
    for name in CHECKS:
        res = [c["result"] for s in scores for c in s["checks"] if c["name"] == name and c["result"] != "N/A"]
        rates.append((name, sum(r == "PASS" for r in res), sum(r == "WARN" for r in res), sum(r == "FAIL" for r in res), len(res)))
    cov_n = sum(s["coverage"]["n"] for s in scores)
    cov_in = sum(s["coverage"]["inside"] for s in scores)
    hi_n = sum(s["coverage"]["high_n"] for s in scores)
    hi_in = sum(s["coverage"]["high_inside"] for s in scores)
    hi_claim = sum(s["coverage"]["high_claim"] for s in scores)
    false_alarms = sum(1 for s in scores for c in s["checks"] if c["name"] == "detection" and c["reason"].startswith("false alarm"))
    det = [m for s in scores for m in s["detect_minutes"]]
    tops = (f"<b>{n}</b> days: {_badge('PASS')} {verdicts['PASS']} &nbsp; {_badge('WARN')} {verdicts['WARN']} &nbsp; {_badge('FAIL')} {verdicts['FAIL']}"
            f"<br>Real end inside the agent's range: {cov_in}/{cov_n} ({cov_in / cov_n:.0%})" if cov_n else f"<b>{n}</b> days")
    if hi_n:
        tops += f"; 'high confidence' ranges right {hi_in / hi_n:.0%} of the time (they claim {hi_claim / hi_n:.0%})"
    tops += f"<br>False alarms: {false_alarms}. Average time to notice an incident: {sum(det) / len(det):.1f} min." if det else f"<br>False alarms: {false_alarms}."
    rate_rows = "".join(f"<tr><td>{_e(nm)}</td><td>{p}/{t}</td><td>{w}</td><td>{f}</td><td>{(p / t if t else 0):.0%}</td></tr>"
                        for nm, p, w, f, t in rates)
    day_rows = "".join(
        f"<tr><td><a href='{_e(s['run_id'])}.html'>{s['chaos_seed']}</a></td><td>{_e(s['summary'])}</td><td>{_badge(s['verdict'])}</td>"
        f"<td>{'<br>'.join(_e(c['name'] + ': ' + c['reason']) for c in s['checks'] if c['result'] in ('FAIL', 'WARN')) or '-'}</td></tr>"
        for s in scores)
    page = f"""<!doctype html><meta charset="utf-8"><title>{_e(title)}</title>{STYLE}
<h1>{_e(title)}</h1><div class="sub">Random worst-case days the agent had never seen, scored automatically against what really happened.</div>
<div class="box">{tops}</div>
<div class="box"><h3 style="margin-top:0">Pass rate per check</h3><table><tr><th>Check</th><th>Passed</th><th>Warnings</th><th>Failed</th><th>Pass rate</th></tr>{rate_rows}</table>
<div class="sub">Checks that don't apply to a day (for example "security" on a day without one) are left out of its count.</div></div>
<div class="box"><h3 style="margin-top:0">Days</h3><table><tr><th>Chaos seed</th><th>What happened</th><th>Verdict</th><th>What went wrong</th></tr>{day_rows}</table></div>"""
    path = OUT / "index.html"
    path.write_text(page, encoding="utf-8")
    return str(path)
