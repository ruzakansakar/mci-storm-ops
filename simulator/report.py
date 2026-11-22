"""The debug page: one self-contained HTML file for one simulation run (no server, no external libraries).

    python -m simulator.report --latest
    python -m simulator.report --run-id a31_storm-s7-today

Writes reports/<run_id>.html. Open it by double-clicking the file.
"""
import argparse
import json

from backend.database import connect
from backend.settings import ROOT, TZ
from simulator.clock import minute_of

REPORTS = ROOT / "reports"
DEFAULT_PASSENGER = "Maya Torres"
CHANNEL_TYPES = ("channel_view_updated", "channel_update_failed")


RERUN = "python -m simulator.run --seed 7 --scenario a31_storm"


def _unfinished(conn, run_id: str | None = None) -> str | None:
    """A run that has events but no final numbers did not finish (it crashed or was stopped)."""
    q = "SELECT run_id FROM events WHERE actor = 'simulator' AND run_id IS NOT NULL AND run_id NOT IN (SELECT run_id FROM metric_runs)"
    args = ()
    if run_id:
        q += " AND run_id = %s"
        args = (run_id,)
    row = conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return row[0] if row else None


def _not_finished_message(run_id: str) -> str:
    if run_id.startswith("chaos-"):
        how = f"python -m chaos.run --seed {run_id.split('-', 1)[1]}"
    else:
        parts = run_id.split("-")
        how = f"python -m simulator.run --seed {parts[1][1:]} --scenario {parts[0]}" if len(parts) == 3 else RERUN
    return f"Run {run_id} did not finish: rerun  {how}"


def latest_run_id(conn) -> str:
    row = conn.execute("SELECT run_id FROM metric_runs ORDER BY finished_at DESC, id DESC LIMIT 1").fetchone()
    broken = _unfinished(conn)
    if broken and (not row or conn.execute("SELECT max(id) FROM events WHERE run_id=%s", (broken,)).fetchone()[0] >
                   (conn.execute("SELECT max(id) FROM events WHERE run_id=%s", (row[0],)).fetchone()[0] or 0)):
        raise SystemExit(_not_finished_message(broken))
    if not row:
        raise SystemExit(f"No simulation run found in the database. Run:  {RERUN}")
    return row[0]


def _text(type_, p) -> str:
    if type_ == "channel_view_updated":
        return f"{p['passenger_id']} {p['channel']} now shows: {p['shown_status']}, {' > '.join(p['shown_itinerary'])}" \
               f"{' (bag)' if p['kind'] == 'bag' else ''} [{p['lag_min']} min late]"
    if type_ == "channel_update_failed":
        return f"{p['passenger_id']} {p['channel']}: the update never arrived"
    return ", ".join(f"{k}={v}" for k, v in p.items())


def collect(conn, run_id: str) -> dict:
    row = conn.execute("SELECT seed, mode, scenario, metrics FROM metric_runs WHERE run_id=%s ORDER BY id DESC LIMIT 1",
                       (run_id,)).fetchone()
    if not row:
        if _unfinished(conn, run_id):
            raise SystemExit(_not_finished_message(run_id))
        raise SystemExit(f"No run called '{run_id}'. Use --latest, or check the name with:  SELECT run_id FROM metric_runs")
    seed, mode, scenario, metrics = row
    inc = conn.execute("SELECT kind, title, likely_cause, controllable, status, opened_at, resolved_at, expected_end_min, "
                       "expected_end_max FROM incidents WHERE run_id=%s ORDER BY id LIMIT 1", (run_id,)).fetchone()
    incident = None
    if inc:
        incident = {"kind": inc[0], "title": inc[1], "likely_cause": inc[2], "controllable": inc[3], "status": inc[4],
                    "opened": minute_of(inc[5]), "resolved": minute_of(inc[6]) if inc[6] else None,
                    "announced_end": minute_of(inc[7]), "actual_end": minute_of(inc[8])}

    people = {r[0]: {"name": r[1], "segment": r[2], "destination": r[3]} for r in
              conn.execute("SELECT id, full_name, segment, final_destination FROM passengers ORDER BY id")}
    for pid, itin in conn.execute("SELECT passenger_id, array_agg(flight_id ORDER BY seq) FROM bookings "
                                  "WHERE status <> 'cancelled' GROUP BY passenger_id"):
        people[pid]["final_itinerary"] = itin

    events, per_pax = [], {}
    for eid, at, type_, actor, station, payload in conn.execute(
            "SELECT id, occurred_at, type, actor_name, station, payload FROM events WHERE run_id=%s ORDER BY id", (run_id,)):
        t = minute_of(at)
        events.append({"id": eid, "t": t, "type": type_, "actor": actor, "station": station, "text": _text(type_, payload)})
        if type_ in CHANNEL_TYPES:
            pid = payload["passenger_id"]
            entry = per_pax.setdefault(pid, {"truth": [], "channels": {}, "lost": []})
            if type_ == "channel_view_updated":
                entry["channels"].setdefault(payload["channel"], []).append({
                    "t": t, "kind": payload["kind"], "itinerary": payload["shown_itinerary"], "status": payload["shown_status"],
                    "bag": payload["shown_bag_routing"]})
            else:
                entry["lost"].append({"t": t, "channel": payload["channel"], "kind": payload["kind"]})
    initial = {}
    for at, pid, before, after in conn.execute(
            "SELECT at, entity_id, before, after FROM audit_log WHERE run_id=%s AND entity_type='passenger' ORDER BY id", (run_id,)):
        entry = per_pax.setdefault(pid, {"truth": [], "channels": {}, "lost": []})
        if pid not in initial:
            initial[pid] = before
        entry["truth"].append({"t": minute_of(at), "itinerary": after["itinerary"], "status": after["status"],
                               "bag": after["bag_routing"], "message": after["message"]})
    for pid, before in initial.items():
        per_pax[pid]["initial"] = {"itinerary": before["itinerary"], "status": before["status"], "bag": before["bag_routing"]}

    pax_list = []
    for pid, info in people.items():
        entry = per_pax.get(pid, {"truth": [], "channels": {}, "lost": []})
        if "initial" not in entry:
            start = info.get("final_itinerary", [])
            entry["initial"] = {"itinerary": start, "status": "confirmed", "bag": []}
        pax_list.append({"id": pid, **info, "changed": bool(entry["truth"]), **entry})
    default = next((p["id"] for p in pax_list if p["name"] == DEFAULT_PASSENGER),
                   next((p["id"] for p in pax_list if p["changed"]), pax_list[0]["id"]))
    series = metrics.pop("timeseries")
    situation = collect_situation(conn, run_id)
    communication = collect_communication(conn, run_id, default)
    return {"situation": situation, "communication": communication, "run": {"id": run_id, "seed": seed, "mode": mode, "scenario": scenario}, "incident": incident,
            "metrics": metrics, "series": series, "events": events, "passengers": pax_list, "default_passenger": default}


KEY_TYPES = ("ground_stop_started", "ground_stop_extended", "ground_stop_ended", "ground_delay_started", "ground_delay_extended",
             "ground_delay_ended", "ramp_closed", "ramp_reopened", "flight_cancelled", "flight_diverted")


def _hm(dt) -> str:
    return dt.astimezone(TZ).strftime("%H:%M") if dt else "-"


def collect_situation(conn, run_id: str) -> dict | None:
    """The Situation agent's latest replay of this run: its versions and proposals, next to what really happened."""
    row = conn.execute("SELECT agent_run_id FROM situation_assessments WHERE run_id=%s ORDER BY created_at DESC, id DESC LIMIT 1",
                       (run_id,)).fetchone()
    if not row:
        return None
    arid = row[0]
    by_t = {}

    def slot(t):
        return by_t.setdefault(minute_of(t), {"said": [], "proposed": [], "happened": []})
    for v, at, status, typ, conf, e0, e1, e2, summary in conn.execute(
            "SELECT version, assessed_at, status, type, confidence, expected_end_earliest, expected_end_likely, expected_end_latest, "
            "summary FROM situation_assessments WHERE agent_run_id=%s ORDER BY version", (arid,)):
        rng = f"likely {_hm(e1)} (range {_hm(e0)}-{_hm(e2)})" if e1 else "no end time"
        slot(at)["said"].append(f"v{v} {status} | {typ or '-'} | confidence {conf if conf is not None else '-'} | {rng}")
    for created, title, decisions, role in conn.execute(
            "SELECT created_at, title, decision_ids, approver_role FROM recommendations WHERE run_id=%s AND agent='situation' ORDER BY id",
            (arid,)):
        slot(created)["proposed"].append(f"{', '.join(decisions or [])}: {title} (to {role})")
    for at, typ, payload in conn.execute("SELECT occurred_at, type, payload FROM events WHERE run_id=%s AND type = ANY(%s) ORDER BY id",
                                         (run_id, list(KEY_TYPES))):
        slot(at)["happened"].append(f"{typ}: {_text(typ, payload)}")
    rows = [{"t": t, **v} for t, v in sorted(by_t.items())]
    return {"agent_run_id": arid, "rows": rows}


def collect_communication(conn, run_id: str, focus: str) -> dict | None:
    """The Communication agent's latest replay of this run: every mismatch it detected (start, end, severity, cause)."""
    row = conn.execute("SELECT agent_run_id FROM mismatches WHERE run_id=%s ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    if not row:
        return None
    arid = row[0]
    items = []
    for pid, ch, field, shown, true, cause, sev, since, detected, cleared in conn.execute(
            "SELECT passenger_id, channel, field, shown_value, true_value, cause, severity, since, detected_at, cleared_at FROM mismatches "
            "WHERE agent_run_id=%s ORDER BY since, id", (arid,)):
        items.append({"pid": pid, "channel": ch, "field": field, "shown": shown, "true": true, "cause": cause, "severity": sev,
                      "since": minute_of(since), "detected": minute_of(detected), "cleared": minute_of(cleared) if cleared else None})
    sev = {}
    for x in items:
        sev[x["severity"]] = sev.get(x["severity"], 0) + 1
    return {"agent_run_id": arid, "items": items, "by_severity": sev, "focus": focus,
            "focus_items": [x for x in items if x["pid"] == focus]}


def build_html(data: dict) -> str:
    blob = json.dumps(data, default=str).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob).replace("__TITLE__", f"Run {data['run']['id']}")


def write_report(run_id: str | None = None, latest: bool = False):
    with connect() as conn:
        rid = latest_run_id(conn) if latest or not run_id else run_id
        data = collect(conn, rid)
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{rid}.html"
    path.write_text(build_html(data), encoding="utf-8")
    return path, data


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the debug page for a simulation run.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id")
    g.add_argument("--latest", action="store_true")
    args = ap.parse_args(argv)
    path, data = write_report(args.run_id, args.latest)
    shown = len([e for e in data["events"] if e["type"] not in CHANNEL_TYPES])
    print(f"Wrote {path}")
    print(f"  {len(data['events'])} events ({shown} not counting channel copies), {len(data['passengers'])} passengers, "
          f"{sum(p['changed'] for p in data['passengers'])} whose trip changed.")
    print("  Open it by double-clicking the file (no server needed).")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 body{font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;margin:0;color:#1d2530;background:#f6f7f9}
 main{max-width:1100px;margin:0 auto;padding:20px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #d5d9e0;padding-bottom:4px}
 .sub{color:#5b6573} section{background:#fff;border:1px solid #e1e4ea;border-radius:6px;padding:12px 16px;margin-top:14px}
 table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #eceff3;vertical-align:top}
 th{font-weight:600;color:#44505f;background:#fafbfc;position:sticky;top:0}
 .num{text-align:right;font-variant-numeric:tabular-nums} .muted{color:#7a8493}
 .wrong{background:#fde8e6;color:#9b1c10} .lost{background:#fff1cc;color:#7a5200} .ok{background:#eaf6ee}
 .chips label{display:inline-block;margin:2px 10px 2px 0;white-space:nowrap}
 .scroll{max-height:420px;overflow:auto;border:1px solid #eceff3}
 svg{width:100%;height:auto;display:block} .legend span{display:inline-block;margin-right:14px}
 .sw{display:inline-block;width:10px;height:10px;margin-right:4px;border-radius:2px}
 select{font-size:14px;padding:3px} code{font-size:12px}
</style></head><body><main>
<h1>Ground-Stop Co-Pilot: simulation run</h1>
<div class="sub" id="runline"></div>

<section><h2 style="margin-top:0">The incident</h2><div id="incident"></div></section>
<section><h2 style="margin-top:0">Numbers for this run</h2><table id="metrics"></table></section>
<section><h2 style="margin-top:0">Flights by status over the day</h2><div id="c1"></div><div class="legend" id="l1"></div></section>
<section><h2 style="margin-top:0">Passengers whose screens disagree with the truth</h2>
 <div class="sub">Of the tracked sample passengers: how many have at least one wrong channel (app, text, gate screen or call center).</div>
 <div id="c2"></div></section>
<section><h2 style="margin-top:0">One passenger, minute by minute</h2>
 <div>Passenger <select id="pax"></select> <span class="sub" id="paxinfo"></span></div>
 <div class="sub">Each row is a moment something changed. Red = that screen is showing something different from the truth. Yellow = the update was lost.</div>
 <div class="scroll" style="margin-top:8px"><table id="ptable"></table></div></section>
<section><h2 style="margin-top:0">Situation agent: what it said vs what really happened</h2>
 <div class="sub" id="sitinfo"></div>
 <div class="scroll" style="margin-top:8px"><table id="situation"></table></div></section>
<section><h2 style="margin-top:0">Communication agent: the mismatches it detected vs the real wrong screens</h2>
 <div class="sub" id="cominfo"></div>
 <div id="c3"></div>
 <div class="legend"><span><i class="sw" style="background:#c0392b"></i>Real: passengers with a wrong screen (simulator)</span>
  <span><i class="sw" style="background:#2c6fbb"></i>Detected: passengers with an open mismatch (agent)</span></div>
 <h3 id="comfocush">Detections for the default passenger</h3>
 <div class="scroll"><table id="comfocus"></table></div></section>
<section><h2 style="margin-top:0">What happened (events)</h2>
 <div class="chips" id="chips"></div>
 <div class="scroll" style="margin-top:8px"><table id="events"></table></div></section>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const hm = m => { const d=Math.floor(m/1440), r=m%1440; return String(Math.floor(r/60)).padStart(2,'0')+':'+String(r%60).padStart(2,'0')+(d?'+'+d:''); };
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const CH = [['app','App'],['sms','Text (SMS)'],['gate_screen','Gate screen'],['call_center','Call center']];

// ---------- situation agent
(function(){
  const S = D.situation;
  if (!S) { $('sitinfo').textContent = 'The Situation agent has not been run on this day. Run: python -m backend.agents.situation.run --run-id ' + D.run.id + ' --replay'; return; }
  $('sitinfo').textContent = 'Latest replay ' + S.agent_run_id + '. The agent saw only events up to each time; the right column is the simulator\'s record.';
  const cell = a => a.map(esc).join('<br>');
  $('situation').innerHTML = '<tr><th>Time</th><th>What the agent said (new versions)</th><th>What it proposed to a human</th><th>What really happened</th></tr>' +
    S.rows.map(r => `<tr><td>${hm(r.t)}</td><td>${cell(r.said)}</td><td>${cell(r.proposed)}</td><td>${cell(r.happened)}</td></tr>`).join('');
})();

// ---------- header + incident
$('runline').textContent = `Run ${D.run.id} | scenario ${D.run.scenario} | seed ${D.run.seed} | mode ${D.run.mode}`;
const I = D.incident;
$('incident').innerHTML = I ? `<b>${esc(I.title)}</b> (${esc(I.kind)}, ${I.controllable?'within Southwest\'s control':'outside Southwest\'s control'})<br>
  ${esc(I.likely_cause)}<br>Opened ${hm(I.opened)}; first announced to end ${hm(I.announced_end)}; actually ended ${hm(I.actual_end)}
  (${I.actual_end-I.opened} min). Status: ${esc(I.status)}.` : 'No incident was recorded.';

// ---------- metrics
const LABEL = {flights_total:'Flights in the day',flights_delayed:'Flights delayed (30+ min)',flights_cancelled:'Flights cancelled',crews_illegal_rest:'Crews flying with rest below the legal minimum (flagged at 05:00)',crews_short_for_plan:'Crews short only for our planning estimate (not illegal)',pilot_duty_extensions:'Pilot duty extensions (human decision: captain + crew scheduling)',flights_diverted:'Flights diverted',
 avg_delay_min:'Average delay of delayed flights (min)',max_delay_min:'Longest delay (min)',passengers_disrupted:'Passengers disrupted (counting each flight)',
 passengers_rebooked:'Passengers moved to another flight',tracked_passengers:'Sample passengers we follow one by one',tracked_passengers_stranded:'Sample passengers with no seat at all (not even tomorrow)',
 passengers_stranded_overnight:'Passengers stranded overnight (rebooked for 7 Oct)',tracked_passengers_overnight:'Sample passengers stranded overnight',
 tracked_overnight_who_live_locally:'...of them live in the Kansas City area',next_day_flights_created:'Next-day flights created',
 peak_passengers_disagreeing:'Most sample passengers with a wrong screen at once',avg_passengers_disagreeing:'Average sample passengers with a wrong screen (whole day)',
 avg_passengers_disagreeing_during_stop:'Average sample passengers with a wrong screen (during the stop)',passenger_minutes_of_disagreement:'Passenger-minutes of disagreement',
 minutes_until_all_channels_agree_after_stop:'Minutes after the stop ends until every screen first agrees (lost messages aside)',
 minutes_until_last_late_copy_arrives:'Minutes after the stop ends until the last late copy arrives',
 lost_messages_still_wrong_at_end:'Lost messages still wrong at the end',max_tarmac_min:'Longest tarmac wait (min)',
 wheelchair_passengers_disrupted:'Wheelchair passengers disrupted',child_alone_passengers_disrupted:'Children flying alone disrupted',
 peak_people_in_terminal:'Most people in the terminal'};
const skip = new Set(['stop_start','stop_end','wrong_field_minutes_by_cause']);
let rows = Object.entries(D.metrics).filter(([k])=>!skip.has(k)).sort((a,b)=>Object.keys(LABEL).indexOf(a[0])-Object.keys(LABEL).indexOf(b[0])).map(([k,v]) => `<tr><td>${LABEL[k]||k}</td><td class="num">${v===null?'n/a':v}</td></tr>`).join('');
const bc = D.metrics.wrong_field_minutes_by_cause || {};
rows += `<tr><td>Wrong-screen minutes by cause</td><td class="num">${Object.entries(bc).map(([k,v])=>k+': '+v).join(' | ')}</td></tr>`;
$('metrics').innerHTML = rows;

// ---------- charts (plain SVG)
const S = D.series, T0 = S.t[0], T1 = S.t[S.t.length-1];
const bands = [];
if (I) bands.push({a:I.opened,b:I.actual_end,c:'#fdecc8',n:'stop'});
const rc = D.events.filter(e=>e.type==='ramp_closed'), ro = D.events.filter(e=>e.type==='ramp_reopened');
rc.forEach((e,i)=>{ if(ro[i]) bands.push({a:e.t,b:ro[i].t,c:'#f9c9c4',n:'ramp closed'}); });
function frame(W,H,ymax,ytitle){
  const m={l:52,r:12,t:10,b:26}; const x=t=>m.l+(t-T0)/(T1-T0)*(W-m.l-m.r), y=v=>H-m.b-v/ymax*(H-m.t-m.b);
  let s=`<svg viewBox="0 0 ${W} ${H}">`;
  bands.forEach(b=>s+=`<rect x="${x(b.a)}" y="${m.t}" width="${x(b.b)-x(b.a)}" height="${H-m.t-m.b}" fill="${b.c}"><title>${b.n} ${hm(b.a)}-${hm(b.b)}</title></rect>`);
  const step = ymax>=200? 100 : ymax>=50? 25 : ymax>=20? 10 : 5;
  for(let v=0;v<=ymax;v+=step) s+=`<line x1="${m.l}" x2="${W-m.r}" y1="${y(v)}" y2="${y(v)}" stroke="#e5e8ee"/><text x="${m.l-6}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="#5b6573">${v}</text>`;
  for(let t=Math.ceil(T0/120)*120;t<=T1;t+=120) s+=`<text x="${x(t)}" y="${H-8}" text-anchor="middle" font-size="11" fill="#5b6573">${hm(t)}</text>`;
  return {s,x,y,m,H,W};
}
const GROUPS = [['Scheduled',['scheduled'],'#9fb4c7'],['Delayed or held',['ground_stop','delayed','held'],'#f0a830'],['In the air',['departed','airborne'],'#5aa469'],
  ['Diverted',['diverted'],'#8e6bb8'],['Landed',['arrived'],'#cfd6de'],['Cancelled',['cancelled'],'#d9534f']];
(function(){
  const total = S.flights[0] ? Object.values(S.flights[0]).reduce((a,b)=>a+b,0) : 0;
  const f = frame(1060,300,Math.ceil(total/25)*25);
  let base = S.t.map(()=>0), s=f.s;
  GROUPS.forEach(([name,keys,color])=>{
    const vals = S.flights.map(o=>keys.reduce((a,k)=>a+(o[k]||0),0));
    const top = vals.map((v,i)=>base[i]+v);
    const up = S.t.map((t,i)=>`${f.x(t)},${f.y(top[i])}`), dn = S.t.map((t,i)=>`${f.x(t)},${f.y(base[i])}`).reverse();
    s+=`<polygon points="${up.concat(dn).join(' ')}" fill="${color}" stroke="none"><title>${name}</title></polygon>`;
    base = top;
  });
  $('c1').innerHTML = s+'</svg>';
  $('l1').innerHTML = GROUPS.map(([n,,c])=>`<span><i class="sw" style="background:${c}"></i>${n}</span>`).join('')+
    '<span class="muted">Shaded background: yellow = stop in effect, red = ramp closed</span>';
})();
(function(){
  const ymax = Math.max(5, Math.ceil(Math.max(...S.pax_disagreeing)/10)*10);
  const f = frame(1060,240,ymax);
  const pts = S.t.map((t,i)=>`${f.x(t)},${f.y(S.pax_disagreeing[i])}`).join(' ');
  $('c2').innerHTML = f.s+`<polyline points="${pts}" fill="none" stroke="#c0392b" stroke-width="2"/></svg>`;
})();

// ---------- communication agent
(function(){
  const C = D.communication;
  if (!C) { $('cominfo').textContent = 'The Communication agent has not been run on this day. Run: python -m backend.agents.communication.run --run-id ' + D.run.id + ' --replay'; return; }
  $('cominfo').textContent = `Replay ${C.agent_run_id}: ${C.items.length} mismatches detected (` +
    Object.entries(C.by_severity).map(([k,v])=>`${v} ${k}`).join(', ') + '). Each counts a passenger from when it started being wrong to when the channel caught up.';
  const detected = S.t.map(t => new Set(C.items.filter(x => x.since <= t && (x.cleared === null || t < x.cleared)).map(x => x.pid)).size);
  const ymax = Math.max(5, Math.ceil(Math.max(...S.pax_disagreeing, ...detected)/10)*10);
  const f = frame(1060,240,ymax);
  const real = S.t.map((t,i)=>`${f.x(t)},${f.y(S.pax_disagreeing[i])}`).join(' ');
  const mine = S.t.map((t,i)=>`${f.x(t)},${f.y(detected[i])}`).join(' ');
  $('c3').innerHTML = f.s+`<polyline points="${real}" fill="none" stroke="#c0392b" stroke-width="4" opacity="0.45"/>`+
    `<polyline points="${mine}" fill="none" stroke="#2c6fbb" stroke-width="1.6" stroke-dasharray="5 3"/></svg>`;
  const rows = C.focus_items; $("comfocush").textContent = "What it detected on the screens of " + C.focus + " (Maya Torres by default)";
  $('comfocus').innerHTML = `<tr><th>Wrong from</th><th>Detected</th><th>Cleared</th><th>Channel</th><th>Field</th><th>Shown</th><th>True</th><th>Suspected cause</th><th>Severity</th></tr>` +
    (rows.length ? rows.map(x=>`<tr><td>${hm(x.since)}</td><td>${hm(x.detected)}</td><td>${x.cleared===null?'still open':hm(x.cleared)}</td><td>${esc(x.channel)}</td><td>${esc(x.field)}</td><td>${esc(x.shown)}</td><td>${esc(x.true)}</td><td>${esc(x.cause)}</td><td>${esc(x.severity)}</td></tr>`).join('')
     : `<tr><td colspan="9">No mismatches detected for ${esc(C.focus)}.</td></tr>`);
})();

// ---------- passenger lookup
const P = Object.fromEntries(D.passengers.map(p=>[p.id,p]));
$('pax').innerHTML = D.passengers.map(p=>`<option value="${p.id}">${p.changed?'* ':''}${esc(p.name)} (${p.id}, ${p.segment})</option>`).join('');
$('pax').value = D.default_passenger;
const same = (a,b) => JSON.stringify(a.itinerary)===JSON.stringify(b.itinerary) && a.status===b.status && JSON.stringify(a.bag)===JSON.stringify(b.bag);
function showPax(){
  const p = P[$('pax').value];
  $('paxinfo').textContent = `${p.segment}, going to ${p.destination}. ${p.changed?'* Their trip changed during the day.':'Nothing changed for this passenger.'}`;
  const times = new Set([T0]); p.truth.forEach(x=>times.add(x.t));
  CH.forEach(([c])=>(p.channels[c]||[]).forEach(x=>times.add(x.t))); p.lost.forEach(x=>times.add(x.t));
  const cur = {truth:{...p.initial}}; CH.forEach(([c])=>cur[c]={...p.initial});
  const lostAt = {};
  p.lost.forEach(x=>(lostAt[x.t]=lostAt[x.t]||{})[x.channel]=1);
  let html = `<tr><th>Time</th><th>Truth (Southwest systems)</th>${CH.map(([,n])=>`<th>${n}</th>`).join('')}</tr>`;
  const cell = (s,wrong,lost) => `<td class="${lost?'lost':wrong?'wrong':'ok'}">${esc(s.status)}<br><span class="muted">${esc(s.itinerary.join(' > ')||'(no flights)')}${s.bag.length?'<br>bag: '+esc(s.bag.join(' > ')):''}</span>${lost?'<br><b>update lost</b>':''}</td>`;
  [...times].sort((a,b)=>a-b).forEach(t=>{
    p.truth.filter(x=>x.t===t).forEach(x=>cur.truth={itinerary:x.itinerary,status:x.status,bag:x.bag});
    CH.forEach(([c])=>(p.channels[c]||[]).filter(x=>x.t===t).forEach(x=>{
      if(x.kind==='main') cur[c]={...cur[c],itinerary:x.itinerary,status:x.status}; else cur[c]={...cur[c],bag:x.bag}; }));
    html += `<tr><td>${hm(t)}${t===T0?' <span class="muted">(start)</span>':''}</td>${cell(cur.truth,false,false)}`+
      CH.map(([c])=>cell(cur[c],!same(cur[c],cur.truth),lostAt[t]&&lostAt[t][c])).join('')+'</tr>';
  });
  $('ptable').innerHTML = html;
}
$('pax').onchange = showPax; showPax();

// ---------- events
const types = [...new Set(D.events.map(e=>e.type))].sort();
const isChan = t => t.startsWith('channel_');
$('chips').innerHTML = types.map(t=>`<label><input type="checkbox" value="${t}" ${isChan(t)?'':'checked'}> ${t} (${D.events.filter(e=>e.type===t).length})</label>`).join('');
function showEvents(){
  const on = new Set([...document.querySelectorAll('#chips input:checked')].map(i=>i.value));
  $('events').innerHTML = '<tr><th>Time</th><th>Type</th><th>Where</th><th>Details</th></tr>'+
    D.events.filter(e=>on.has(e.type)).map(e=>`<tr><td>${hm(e.t)}</td><td>${e.type}</td><td>${e.station||''}</td><td>${esc(e.text)}</td></tr>`).join('');
}
document.querySelectorAll('#chips input').forEach(i=>i.onchange=showEvents); showEvents();
</script></main></body></html>
"""

if __name__ == "__main__":
    main()
