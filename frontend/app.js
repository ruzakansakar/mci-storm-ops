// Command map: replays one simulated day from the read-only API.
// Step 0: normal day. Steps 1-2: the storm arrives and Situation notices it.
import { NetworkMap } from './map.js';
import { icon } from './icons.js';
import { stormWindow, weatherAt, stormCell, compass } from './weather.js';

const $ = (id) => document.getElementById(id);
const hhmm = (m) => { const v = Math.max(0, Math.round(m)); return `${String(Math.floor(v / 60) % 24).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`; };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const dur = (m) => { const v = Math.max(0, Math.round(m)); return v >= 60 ? `${Math.floor(v / 60)} h ${String(v % 60).padStart(2, '0')} min` : `${v} min`; };
const toMin = (s) => { const [h, m] = String(s).split(':').map(Number); return h * 60 + m; };
const AGENTS = ['situation', 'network_recovery', 'passenger_care', 'station_staff', 'communication'];

const state = { day: null, t: 480, playing: false, speed: 5, board: 'dep', lastPanel: 0, selected: null };
const map = new NetworkMap($('map'), $('tooltip'));
map.onSelect = (id) => select(id);

// ---------------------------------------------------------------- theme
function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function applyThemeLabel() {
  $('theme').textContent = currentTheme() === 'dark' ? 'Light mode' : 'Dark mode';
  map.readColors();
}
$('theme').addEventListener('click', () => {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('gsc-theme', next); } catch (e) {}
  applyThemeLabel();
});
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyThemeLabel);

// ---------------------------------------------------------------- data
async function loadRuns() {
  const runs = await (await fetch('/api/runs')).json();
  const sel = $('run');
  sel.innerHTML = runs.map((r) => `<option value="${esc(r.run_id)}">${esc(r.label)} (${esc(r.run_id)})</option>`).join('');
  const preferred = runs.find((r) => r.mode === (params.get('mode') || 'orchestrated')) || runs[0];
  if (!preferred) {
    $('board').innerHTML = '<caption>No simulated day in the database yet. Run: python -m simulator.run --seed 7 --scenario a31_storm --mode orchestrated</caption>';
    return;
  }
  sel.value = preferred.run_id;
  sel.addEventListener('change', () => loadDay(sel.value));
  await loadDay(preferred.run_id);
}

async function loadDay(runId) {
  const day = await (await fetch(`/api/day/${encodeURIComponent(runId)}`)).json();
  day.names = Object.fromEntries(day.airports.map((a) => [a.code, a.name]));
  day.coords = Object.fromEntries(day.airports.map((a) => [a.code, a]));
  day.byId = Object.fromEntries(day.flights.map((f) => [f.id, f]));
  day.evByFlight = {};
  for (const e of day.flight_events) (day.evByFlight[e.flight_id] ||= []).push(e);
  day.storm = stormWindow(day.world);
  const served = new Set();
  const routes = [];
  for (const f of day.flights) {
    const other = f.from === 'MCI' ? f.to : f.from;
    if (!served.has(other)) { served.add(other); routes.push(['MCI', other]); }
  }
  for (const e of day.flight_events) if (e.diverted_to) served.add(e.diverted_to);
  map.setNetwork(day.airports.filter((a) => a.code === 'MCI' || served.has(a.code)), routes);
  state.day = day;
  state.lastPanel = 0;
  if (state.selected && !day.byId[state.selected]) state.selected = null;
}

// great-circle distance in statute miles
function miles(a, b) {
  const A = state.day.coords[a], B = state.day.coords[b];
  if (!A || !B) return 0;
  const r = Math.PI / 180, dLat = (B.lat - A.lat) * r, dLon = (B.lon - A.lon) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(A.lat * r) * Math.cos(B.lat * r) * Math.sin(dLon / 2) ** 2;
  return 3959 * 2 * Math.asin(Math.sqrt(h));
}

// ---------------------------------------------------------------- one flight at time t
// What the board shows, where the plane is drawn, and what the tracker says.
function track(f, t) {
  const evs = (state.day.evByFlight[f.id] || []).filter((e) => e.t <= t);
  const last = (type) => [...evs].reverse().find((e) => e.type === type);
  const out = f.dir === 'departure';
  const block = f.arr - f.dep;
  const base = { dir: out ? 'out' : 'in', planned: { from: f.from, to: f.to } };
  const cancelled = last('flight_cancelled'), arrived = last('flight_arrived');
  const diverted = last('flight_diverted'), resumed = last('flight_resumed'), holding = last('flight_holding');
  const delayed = [...evs].reverse().find((e) => e.type === 'flight_delayed' || e.type === 'flight_delay_updated');
  // flights that took off before the simulation starts (05:00) have no take-off event
  const takeoff = (out ? last('flight_departed') : last('flight_airborne')) || (f.dep < 300 && t >= f.dep ? { t: f.dep } : null);

  if (cancelled) return { ...base, phase: 'cancelled', label: 'Cancelled', tone: 'bad', legs: [{ from: f.from, to: f.to, p: 0 }] };
  if (!takeoff) {
    const legs = [{ from: f.from, to: f.to, p: 0 }];
    if (delayed) return { ...base, phase: 'delayed', label: `Delayed to ${delayed.new_departure || hhmm(f.dep + (delayed.delay_min || 0))}`, tone: 'warn', legs, gate: f.from };
    if (t > f.dep + 5) return { ...base, phase: 'due', label: 'Departure due', tone: 'warn', legs, gate: f.from };
    return { ...base, phase: 'scheduled', label: 'On time', tone: 'ok', legs, gate: f.from };
  }
  const start = takeoff.t;
  if (arrived) {
    const legs = diverted ? [{ from: f.from, to: 'MCI', p: 1 }, { from: diverted.diverted_to, to: 'MCI', p: 1 }] : [{ from: f.from, to: f.to, p: 1 }];
    return { ...base, phase: 'landed', label: out ? 'Departed' : 'Landed', tone: 'quiet', legs, start, landed: arrived.t,
      flown: diverted ? miles(f.from, 'MCI') + 2 * miles('MCI', diverted.diverted_to) : miles(f.from, f.to), left: 0 };
  }
  if (!out && diverted) {
    const alt = diverted.diverted_to;
    const toAlt = diverted.extra_flying_min || 25;
    const before = miles(f.from, 'MCI');
    const d = miles('MCI', alt);
    if (resumed && resumed.t > diverted.t) {          // flying back to Kansas City
      const legDur = 40, p = Math.min(0.98, Math.max(0.01, (t - resumed.t) / legDur));
      return { ...base, phase: 'air', kind: 'arc', from: alt, to: 'MCI', progress: p, tone: 'warn', label: `Back from ${alt}`,
        legs: [{ from: f.from, to: 'MCI', p: 1 }, { from: alt, to: 'MCI', p }], start, eta: resumed.t + legDur,
        flown: before + d + d * p, left: d * (1 - p), note: `Diverted to ${alt} at ${hhmm(diverted.t)}, left ${alt} at ${hhmm(resumed.t)}.` };
    }
    if (t < diverted.t + toAlt) {                     // on the way to the alternate airport
      const p = Math.min(0.98, Math.max(0.01, (t - diverted.t) / toAlt));
      return { ...base, phase: 'air', kind: 'arc', from: 'MCI', to: alt, progress: p, tone: 'warn', label: `Diverting to ${alt}`,
        legs: [{ from: f.from, to: 'MCI', p: 1 }, { from: 'MCI', to: alt, p }], start, eta: null,
        flown: before + d * p, left: d * (1 - p), note: `${diverted.reason ? diverted.reason[0].toUpperCase() + diverted.reason.slice(1) : 'Diverted'}. Turned for ${alt} at ${hhmm(diverted.t)}.` };
    }
    return { ...base, phase: 'away', kind: 'ground', from: alt, to: 'MCI', progress: 0, tone: 'warn', label: `Diverted to ${alt}`,
      legs: [{ from: f.from, to: 'MCI', p: 1 }, { from: 'MCI', to: alt, p: 1 }, { from: alt, to: 'MCI', p: 0 }], start, eta: null,
      flown: before + d, left: d, note: `On the ground at ${alt} since ${hhmm(diverted.t + toAlt)}, waiting for Kansas City to reopen.` };
  }
  if (!out && holding) {
    const n = Number(f.id.replace(/\D/g, '')) || 0;
    const orbit = { r: 0.55 + 0.2 * (n % 3), alt: 0.35 + 0.18 * (n % 3), ang: (t - holding.t) * 0.55 + n };
    const expected = holding.expected_landing ? toMin(holding.expected_landing) : null;
    return { ...base, phase: 'holding', kind: 'orbit', orbit, from: f.from, to: f.to, tone: 'warn', label: 'Holding',
      legs: [{ from: f.from, to: f.to, p: 1 }], start, eta: expected, flown: miles(f.from, f.to), left: 0,
      note: `Circling near Kansas City since ${hhmm(holding.t)}${expected ? `, expected to land ${holding.expected_landing}` : ''}.` };
  }
  const p = Math.min(0.98, Math.max(0.01, (t - start) / block)), d = miles(f.from, f.to);
  return { ...base, phase: 'air', kind: 'arc', from: f.from, to: f.to, progress: p, tone: out ? 'quiet' : 'info',
    label: out ? 'Departed' : 'In the air', legs: [{ from: f.from, to: f.to, p }], start, eta: start + block,
    flown: d * p, left: d * (1 - p) };
}

function snapshot(t) {
  const day = state.day;
  const planes = [], labels = new Set();
  let inAir = 0, holding = 0, depDone = 0, depOnTime = 0;
  const rows = { dep: [], arr: [] };
  for (const f of day.flights) {
    const s = track(f, t);
    if (f.dir === 'departure') {
      const dep = (day.evByFlight[f.id] || []).find((e) => e.type === 'flight_departed' && e.t <= t);
      if (dep) { depDone++; if ((dep.delay_min || 0) <= 15) depOnTime++; }
      rows.dep.push({ f, s });
    } else rows.arr.push({ f, s });
    if (s.kind) {
      if (s.kind !== 'ground') inAir++;
      if (s.kind === 'orbit') holding++;
      planes.push({ id: f.id, kind: s.kind, orbit: s.orbit, from: s.from, to: s.to, dir: s.dir, tone: s.tone === 'warn' ? 'warn' : '',
        progress: s.progress, fromName: day.names[f.from] || f.from, toName: day.names[f.to] || f.to, status: s.label });
      labels.add(f.dir === 'departure' ? f.to : f.from);
      if (s.kind === 'ground' || s.from !== f.from) labels.add(s.from === 'MCI' ? s.to : s.from);
    }
  }
  const station = [...day.station].reverse().find((x) => x.t <= t) || day.station[0] || {};
  const world = day.world.filter((w) => w.t <= t);
  const stop = [...world].reverse().find((w) => w.type.startsWith('ground_stop'));
  const stopActive = !!stop && stop.type !== 'ground_stop_ended';
  const ramp = [...world].reverse().find((w) => w.type.startsWith('ramp_'));
  const rampOpen = ramp ? ramp.type === 'ramp_reopened' : (station.ramp_open ?? true);
  const nextHour = (dir) => day.flights.filter((f) => f.dir === dir && (dir === 'departure' ? f.dep : f.arr) > t && (dir === 'departure' ? f.dep : f.arr) <= t + 60).length;
  const picture = [...day.situation].reverse().find((s) => s.t <= t) || null;
  const tick = [...day.orchestrator].reverse().find((o) => o.t <= t) || null;
  return { planes, labels, inAir, holding, depDone, depOnTime, rows, station, stopActive, rampOpen, picture, tick,
    depNext: nextHour('departure'), arrNext: nextHour('arrival'), wx: weatherAt(t, day.storm) };
}

// ---------------------------------------------------------------- selection (flight tracker)
function select(id) {
  state.selected = id && state.selected !== id ? id : null;
  state.lastPanel = 0;
}
$('board').addEventListener('click', (e) => { const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id); });
$('board').addEventListener('keydown', (e) => {
  const tr = e.target.closest('tr[data-id]');
  if (tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); select(tr.dataset.id); }
});
$('tracker').addEventListener('click', (e) => { if (e.target.closest('[data-close]')) select(null); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.selected) select(null); });

const PLANE = '<path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z" transform="rotate(90 12 12)"/>';

function renderTracker(t) {
  const box = $('tracker');
  const f = state.selected && state.day.byId[state.selected];
  if (!f) { box.hidden = true; map.setSelection(null); return; }
  const s = track(f, t), names = state.day.names;
  map.setSelection({ id: f.id, dir: s.dir, tone: s.tone === 'warn' ? 'warn' : '', legs: s.legs,
    planned: s.legs.length > 1 ? s.planned : null, gate: s.gate });
  const total = (s.flown ?? 0) + (s.left ?? 0);
  const pct = s.phase === 'landed' || s.phase === 'holding' ? 100 : total ? Math.round(100 * s.flown / total) : 0;
  const leftT = s.eta != null ? s.eta - t : null;
  const facts = [];
  if (s.start != null) {
    facts.push(['Flown', `${Math.round(s.flown).toLocaleString()} mi`], ['To go', s.phase === 'holding' ? 'Circling' : `${Math.round(s.left).toLocaleString()} mi`]);
    facts.push(['Time flying', dur((s.landed ?? t) - s.start)]);
    facts.push(['Time left', s.phase === 'landed' ? 'Landed' : leftT != null ? dur(leftT) : 'Not known yet']);
  } else {
    facts.push(['Distance', `${Math.round(miles(f.from, f.to)).toLocaleString()} mi`], ['Flight time', dur(f.arr - f.dep)]);
  }
  facts.push(['Aircraft', f.aircraft || '-'], ['Passengers', f.pax ?? '-']);
  const leftLabel = s.start != null ? `Left ${hhmm(s.start)}` : `Due out ${hhmm(f.dep)}`;
  const rightLabel = s.phase === 'landed' ? `Landed ${hhmm(s.landed)}` : s.eta != null ? `ETA ${hhmm(s.eta)}` : s.start != null ? 'ETA not known' : `Due in ${hhmm(f.arr)}`;
  box.hidden = false;
  box.innerHTML = `
    <div class="trk-head">
      <div><span class="trk-id">${esc(f.id)}</span> <span class="status ${s.tone}">${esc(s.label)}</span></div>
      <button class="trk-close" type="button" data-close aria-label="Stop following ${esc(f.id)}">&times;</button>
    </div>
    <div class="trk-route">
      <div><span class="trk-code">${esc(f.from)}</span><span class="trk-city">${esc(names[f.from] || '')}</span><span class="trk-time">${leftLabel}</span></div>
      <div class="trk-right"><span class="trk-code">${esc(f.to)}</span><span class="trk-city">${esc(names[f.to] || '')}</span><span class="trk-time">${rightLabel}</span></div>
    </div>
    <div class="trk-bar ${s.tone === 'warn' ? 'warn' : ''}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="Share of the trip flown">
      <i style="width:${pct}%"></i>
      <svg class="trk-plane" style="left:${pct}%" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${PLANE}</svg>
    </div>
    <dl class="trk-facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
    ${s.note ? `<p class="trk-note">${esc(s.note)}</p>` : ''}`;
}

// ---------------------------------------------------------------- panels
function renderAlert(t, snap) {
  const box = $('alert');
  const pic = snap.picture;
  const open = snap.tick ? snap.tick.incidents.length > 0 : false;
  const recovering = !!pic && pic.status === 'recovery' && !snap.stopActive;
  const show = snap.stopActive || (pic && pic.status === 'abnormal') || (recovering && open);
  box.hidden = !show;
  document.body.classList.toggle('is-alert', !!show && !recovering);
  if (!show) return;
  box.classList.toggle('recovering', recovering);
  if (!pic) {
    box.innerHTML = `<div class="alert-main"><span class="alert-icon">${icon('situation')}</span><div>
      <h2>Ground stop at MCI</h2>
      <p class="muted">No co-pilot in this run. Staff piece the picture together from the FAA notice, the weather radar and phone calls.</p></div></div>`;
    return;
  }
  const cause = String(pic.cause || '').replace(/^suspected:\s*/i, '').replace(/\s*\(signals from.*\)$/i, '');
  const pctOf = (x) => (x == null ? '-' : `${Math.round(x * 100)}%`);
  const title = recovering ? 'Recovering at MCI after the ground stop' : `Ground stop at MCI: ${cause.split(' / ')[0] || 'cause not yet known'}`;
  const im = pic.impact;
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  box.innerHTML = `
    <div class="alert-main">
      <span class="alert-icon">${icon('situation')}</span>
      <div>
        <h2>${esc(cap(title))}</h2>
        <p class="muted">Situation's picture, version ${pic.version}, built at ${hhmm(pic.t)} from ${esc(pic.sources.join(', ').replace(/_/g, ' ') || 'the feeds')}</p>
      </div>
    </div>
    <div class="alert-facts">
      <div><span class="k">${recovering ? 'Ground stop ended' : 'Likely end'}</span><span class="v">${esc(recovering ? (pic.end_likely || '-') : (pic.end_likely || '-'))}</span><span class="s">${recovering ? 'aftermath still being worked' : `between ${esc(pic.end_earliest || '?')} and ${esc(pic.end_latest || '?')}, ${pctOf(pic.end_confidence)} sure`}</span></div>
      <div><span class="k">Suspected cause</span><span class="v small">${esc(cap(cause || '-'))}</span><span class="s">${pctOf(pic.confidence)} sure</span></div>
      <div><span class="k">Flights hit</span><span class="v">${im.diverted + im.cancelled + im.delayed}</span><span class="s">${im.diverted} diverted, ${im.cancelled} cancelled, ${im.delayed} delayed 30+ min</span></div>
      <div><span class="k">Passengers on them</span><span class="v">${Number(im.passengers || 0).toLocaleString()}</span><span class="s">${snap.holding ? `${snap.holding} ${snap.holding === 1 ? 'flight' : 'flights'} holding now` : 'none holding now'}</span></div>
    </div>
    <details class="alert-more"${state.summaryOpen ? ' open' : ''}><summary>Read Situation's summary</summary><p>${esc(pic.summary)}</p></details>`;
  box.querySelector('details').addEventListener('toggle', (e) => { state.summaryOpen = e.target.open; });
}

function renderAgents(t, snap) {
  const day = state.day;
  const strip = $('agent-strip');
  if (!day.orchestrator.length) {
    strip.innerHTML = `<span class="strip-off">Co-pilot off in this run</span>`;
    return;
  }
  const lastRan = {};
  for (const o of day.orchestrator) { if (o.t > t) break; for (const a of o.ran) lastRan[a] = o.t; }
  const pic = snap.picture;
  const fresh = pic && t - pic.t <= 10 && pic.status !== 'normal';
  const open = snap.tick && snap.tick.incidents.length > 0;
  const chip = (key, name, extra, cls) => `<span class="agent-chip ${cls}" role="img" title="${esc(name)}: ${esc(extra)}" aria-label="${esc(name)}: ${esc(extra)}">${icon(key)}${cls === 'speaking' ? `<span class="chip-text">New picture, v${pic.version}</span>` : ''}</span>`;
  strip.innerHTML = AGENTS.map((a) => {
    const ran = lastRan[a];
    const busy = open && ran != null && t - ran <= 3;
    const cls = a === 'situation' && fresh ? 'speaking' : busy ? 'busy' : ran != null ? '' : 'idle';
    const extra = ran != null ? `last checked ${hhmm(ran)}` : 'waiting to start';
    return chip(a, day.agents[a], a === 'situation' && pic ? `${extra}, picture version ${pic.version}` : extra, cls);
  }).join('') + chip('orchestrator', 'Orchestrator',
    snap.tick ? `${snap.tick.incidents.length ? snap.tick.incidents.length + ' open incident' : 'no open incidents'}, ${snap.tick.queue} waiting for a person` : 'waiting to start',
    open ? 'busy' : '');
}

function renderWeather(t, snap) {
  const w = snap.wx;
  const toDeg = (w.dir + 180) % 360;
  $('wx-arrow').setAttribute('transform', `rotate(${toDeg.toFixed(1)} 50 50)`);
  $('wx-card').classList.toggle('stormy', w.intensity > 0.3);
  $('wx-card').classList.toggle('lightning', w.lightning);
  $('wx-cond').textContent = w.cond;
  $('wx-trend').textContent = w.trend;
  $('wx-facts').innerHTML = [
    ['Wind', `${compass(w.dir)} ${Math.round(w.speed)} kt`, `from ${Math.round(w.dir)}°`],
    ['Gusts', w.gust ? `${Math.round(w.gust)} kt` : 'None', w.lightning ? 'strongest now' : ''],
    ['Temperature', `${Math.round(w.temp)}°F`, ''],
    ['Visibility', `${w.vis >= 9.5 ? '10+' : w.vis.toFixed(1)} mi`, `cloud base ${(Math.round(w.base / 100) * 100).toLocaleString()} ft`],
  ].map(([k, v, s]) => `<div><dt>${k}</dt><dd>${v}</dd>${s ? `<span>${s}</span>` : ''}</div>`).join('');
}

function renderPanels(t, snap) {
  const day = state.day;
  const wxLabel = snap.wx.lightning ? 'Lightning' : snap.wx.intensity > 0.3 ? 'Storms' : snap.wx.intensity > 0.05 ? 'Storms near' : 'Clear';
  $('status-chips').innerHTML = [
    `<span class="pill"><span class="dot ${snap.stopActive ? 'bad' : 'ok'}"></span>Operations: <b>${snap.stopActive ? 'Ground stop' : 'Normal'}</b></span>`,
    `<span class="pill"><span class="dot ${snap.rampOpen ? 'ok' : 'bad'}"></span>Ramp: <b>${snap.rampOpen ? 'Open' : 'Closed'}</b></span>`,
    `<span class="pill"><span class="dot ${snap.wx.lightning ? 'bad' : snap.wx.intensity > 0.05 ? 'warn' : 'ok'}"></span>Weather: <b>${wxLabel}</b></span>`,
  ].join('');
  renderAlert(t, snap);
  renderAgents(t, snap);
  renderWeather(t, snap);
  renderTracker(t);

  const onTime = snap.depDone ? Math.round(100 * snap.depOnTime / snap.depDone) + '%' : '-';
  const people = snap.station.people != null ? snap.station.people.toLocaleString() : '-';
  const cap = snap.station.capacity ? Math.round(100 * snap.station.people / snap.station.capacity) : null;
  $('stats').innerHTML = [
    ['Departures on time', onTime, `${snap.depDone} of ${day.flights.filter((f) => f.dir === 'departure').length} have left`],
    ['In the air now', snap.inAir, snap.holding ? `${snap.holding} holding near MCI` : 'to or from MCI'],
    ['Departing next hour', snap.depNext, 'scheduled'],
    ['Arriving next hour', snap.arrNext, 'scheduled'],
    ['People in the terminal', people, cap != null ? `${cap}% of capacity` : ''],
    ['Crews flagged at 05:00', day.roster_flags, 'roster rest under 10 h'],
  ].map(([l, v, s]) => `<div class="stat"><span class="stat-label">${l}</span><span class="stat-value">${v}</span><span class="stat-sub">${s}</span></div>`).join('');

  const list = state.board === 'dep' ? snap.rows.dep : snap.rows.arr;
  const key = (r) => (state.board === 'dep' ? r.f.dep : r.f.arr);
  const live = (r) => ['holding', 'away', 'delayed', 'due'].includes(r.s.phase) || (r.s.phase === 'air' && state.board === 'arr');
  const upcoming = list.filter((r) => key(r) >= t - 40 || live(r) || r.f.id === state.selected).sort((a, b) => key(a) - key(b)).slice(0, 10);
  $('board').innerHTML = `<thead><tr><th>${state.board === 'dep' ? 'Leaves' : 'Arrives'}</th><th>Flight</th><th>${state.board === 'dep' ? 'To' : 'From'}</th><th>Status</th></tr></thead><tbody>` +
    upcoming.map((r) => {
      const city = state.board === 'dep' ? day.names[r.f.to] : day.names[r.f.from];
      const past = (r.s.phase === 'landed' || r.s.phase === 'air') && state.board === 'dep';
      const sel = r.f.id === state.selected;
      return `<tr data-id="${esc(r.f.id)}" tabindex="0" class="${past ? 'past' : ''}${sel ? ' selected' : ''}" aria-selected="${sel}" title="Follow ${esc(r.f.id)} on the map"><td>${hhmm(key(r))}</td><td class="flight">${r.f.id}</td><td class="city">${esc(city)}</td><td><span class="status ${r.s.tone}">${esc(r.s.label)}</span></td></tr>`;
    }).join('') + '</tbody>';
}

// ---------------------------------------------------------------- controls and loop
function setTime(t) {
  state.t = Math.max(300, Math.min(1439, t));
  $('time').value = Math.floor(state.t);
  $('clock').textContent = hhmm(state.t);
}
$('time').addEventListener('input', (e) => { setTime(Number(e.target.value)); state.lastPanel = 0; });
$('play').addEventListener('click', () => {
  state.playing = !state.playing;
  $('play').setAttribute('aria-label', state.playing ? 'Pause' : 'Play');
  $('play-icon').innerHTML = state.playing ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/>' : '<path d="M8 5v14l11-7z" fill="currentColor"/>';
});
for (const b of document.querySelectorAll('[data-speed]')) {
  b.addEventListener('click', () => {
    state.speed = Number(b.dataset.speed);
    document.querySelectorAll('[data-speed]').forEach((x) => x.classList.toggle('active', x === b));
  });
}
for (const tab of document.querySelectorAll('[data-board]')) {
  tab.addEventListener('click', () => {
    state.board = tab.dataset.board;
    document.querySelectorAll('[data-board]').forEach((x) => { x.classList.toggle('active', x === tab); x.setAttribute('aria-selected', x === tab); });
    state.lastPanel = 0;
  });
}

let prev = performance.now();
function frame(now) {
  const dt = (now - prev) / 1000; prev = now;
  if (state.playing && state.day) {
    setTime(state.t + dt * state.speed);
    if (state.t >= 1439 && state.playing) $('play').click();
  }
  if (state.day) {
    const snap = snapshot(state.t);
    map.setPlanes(snap.planes, snap.labels);
    map.setWeather({ cell: stormCell(state.t, state.day.storm), wind: snap.wx, lightning: snap.wx.lightning, alert: snap.stopActive });
    if (now - state.lastPanel > 300) { renderPanels(state.t, snap); state.lastPanel = now; }
  }
  map.draw(now);
  requestAnimationFrame(frame);
}

// Optional links for demos and screenshots: ?t=750&flight=WN2031&mode=today
const params = new URLSearchParams(location.search);
applyThemeLabel();
setTime(Number(params.get('t')) || 480);
if (params.get('flight')) state.selected = params.get('flight');
if (params.get('board') === 'arr') document.querySelector('[data-board="arr"]').click();
loadRuns().catch((e) => { $('board').innerHTML = `<caption>Could not load data: ${esc(e.message)}</caption>`; });
requestAnimationFrame(frame);
