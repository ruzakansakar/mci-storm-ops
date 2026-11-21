"""The debug page: one self-contained HTML file for one simulation run (no server, no external libraries).

    python -m simulator.report --latest
    python -m simulator.report --run-id a31_storm-s7-today

Writes reports/<run_id>.html. Open it by double-clicking the file.
"""
import argparse
import json

from backend.database import connect
from backend.settings import ROOT
from simulator.clock import minute_of

REPORTS = ROOT / "reports"
DEFAULT_PASSENGER = "Maya Torres"
CHANNEL_TYPES = ("channel_view_updated", "channel_update_failed")


def latest_run_id(conn) -> str:
    row = conn.execute("SELECT run_id FROM metric_runs ORDER BY finished_at DESC, id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("No simulation run found in the database. Run:  python -m simulator.run --seed 7 --scenario a31_storm")
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
    return {"run": {"id": run_id, "seed": seed, "mode": mode, "scenario": scenario}, "incident": incident,
            "metrics": metrics, "series": series, "events": events, "passengers": pax_list, "default_passenger": default}


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
