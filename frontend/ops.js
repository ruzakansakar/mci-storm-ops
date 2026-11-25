// Operations tab: the network map with weather, the flight board (rows expand), gates, ramp and staffing.
// Flight positions come from the day's events up to the live clock; nothing here writes anything.
import { NetworkMap } from './map.js';
import { stormWindow, weatherAt, stormCell, compass } from './weather.js';

const $ = (id) => document.getElementById(id);
const hhmm = (m) => { const v = Math.max(0, Math.round(m)); return `${String(Math.floor(v / 60) % 24).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`; };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const dur = (m) => { const v = Math.max(0, Math.round(m)); return v >= 60 ? `${Math.floor(v / 60)} h ${String(v % 60).padStart(2, '0')} min` : `${v} min`; };
const toMin = (s) => { const [h, m] = String(s).split(':').map(Number); return h * 60 + m; };
const state = { day: null, selected: null };

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

// ------------------------------------------------------------------ the view
const PLANE = '<path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z" transform="rotate(90 12 12)"/>';

export class OpsView {
  constructor({ api, run, onFlightNotes }) {
    this.api = api; this.run = run; this.board = 'dep'; this.open = new Set(); this.t = 660; this.lastLoad = 0; this.flightCache = {};
    this.onFlightNotes = onFlightNotes;
    this.map = new NetworkMap($('map'), $('tooltip'));
    this.map.readColors();
    this.map.onSelect = (id) => this.select(id, true);
    for (const tab of document.querySelectorAll('[data-board]')) {
      tab.addEventListener('click', () => {
        this.board = tab.dataset.board;
        document.querySelectorAll('[data-board]').forEach((x) => { x.classList.toggle('active', x === tab); x.setAttribute('aria-selected', x === tab); });
        this.renderBoard();
      });
    }
    $('board').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-follow]');
      if (btn) { this.select(btn.dataset.follow, true); return; }
      const nb = e.target.closest('[data-notes]');
      if (nb) { this.onFlightNotes && this.onFlightNotes(nb.dataset.notes); return; }
      const tr = e.target.closest('tr[data-id]');
      if (tr) this.toggle(tr.dataset.id);
    });
    $('board').addEventListener('keydown', (e) => {
      const tr = e.target.closest('tr[data-id]');
      if (tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); this.toggle(tr.dataset.id); }
    });
    $('tracker').addEventListener('click', (e) => { if (e.target.closest('[data-close]')) this.select(null); });
    const loop = (now) => {
      if (state.day && !$('tab-ops').hidden) {
        const snap = snapshot(this.t);
        this.map.setPlanes(snap.planes, snap.labels);
        this.map.setWeather({ cell: stormCell(this.t, state.day.storm), wind: snap.wx, lightning: snap.wx.lightning, alert: snap.stopActive });
        this.map.draw(now);
      }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  async loadDay() {
    let runId = this.run;
    if (this.api.practice) {
      const runs = await (await fetch('/api/runs')).json().catch(() => []);
      const r = runs.find((x) => x.mode === 'orchestrated') || runs[0];
      if (!r) return;
      runId = r.run_id;
    }
    const resp = await fetch(`/api/day/${encodeURIComponent(runId)}`);
    if (!resp.ok) return;
    const day = await resp.json();
    day.names = Object.fromEntries(day.airports.map((a) => [a.code, a.name]));
    day.coords = Object.fromEntries(day.airports.map((a) => [a.code, a]));
    day.byId = Object.fromEntries(day.flights.map((f) => [f.id, f]));
    day.evByFlight = {};
    for (const e of day.flight_events) (day.evByFlight[e.flight_id] ||= []).push(e);
    day.situation ||= []; day.orchestrator ||= []; day.station ||= [];
    day.storm = stormWindow(day.world);
    const served = new Set(), routes = [];
    for (const f of day.flights) {
      const other = f.from === 'MCI' ? f.to : f.from;
      if (!served.has(other)) { served.add(other); routes.push(['MCI', other]); }
    }
    for (const e of day.flight_events) if (e.diverted_to) served.add(e.diverted_to);
    this.map.setNetwork(day.airports.filter((a) => a.code === 'MCI' || served.has(a.code)), routes);
    state.day = day;
  }

  async update(t) {
    this.t = t;
    const now = performance.now();
    if (!state.day || (!this.api.practice && now - this.lastLoad > 15000)) { this.lastLoad = now; await this.loadDay().catch(() => {}); }
    if (!state.day) return;
    const snap = snapshot(t);
    this.renderWeather(snap); this.renderBoard(snap); this.renderTracker(t);
    if (now - (this.lastStation || 0) > 4000) { this.lastStation = now; this.renderStation(); }
  }

  toggle(id) {
    if (this.open.has(id)) { this.open.delete(id); if (state.selected === id) this.select(null); }
    else { this.open.add(id); this.select(id); this.fetchFlight(id); }
    this.renderBoard();
  }
  select(id, fromMap = false) {
    state.selected = id && state.selected !== id ? id : (fromMap && id ? null : id);
    if (fromMap && state.selected) { this.open.add(state.selected); this.fetchFlight(state.selected); }
    this.renderTracker(this.t); this.renderBoard();
  }
  async fetchFlight(id) {
    if (!this.api.flight) return;
    const f = await this.api.flight(this.run, id).catch(() => null);
    if (f) { this.flightCache[id] = f; this.renderBoard(); }
  }

  renderWeather(snap) {
    const w = snap.wx;
    $('wx-arrow').setAttribute('transform', `rotate(${((w.dir + 180) % 360).toFixed(1)} 50 50)`);
    $('wx-card').classList.toggle('stormy', w.intensity > 0.3);
    $('wx-card').classList.toggle('lightning', w.lightning);
    $('wx-cond').textContent = w.cond;
    $('wx-trend').textContent = w.trend;
    $('wx-facts').innerHTML = [
      ['Wind', `${compass(w.dir)} ${Math.round(w.speed)} kt`], ['Gusts', w.gust ? `${Math.round(w.gust)} kt` : 'None'],
      ['Temperature', `${Math.round(w.temp)}°F`], ['Visibility', `${w.vis >= 9.5 ? '10+' : w.vis.toFixed(1)} mi`],
    ].map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('');
    $('status-chips').innerHTML = [
      `<span class="pill"><span class="dot ${snap.stopActive ? 'bad' : 'ok'}"></span>${snap.stopActive ? 'Ground stop' : 'Normal operations'}</span>`,
      `<span class="pill"><span class="dot ${snap.rampOpen ? 'ok' : 'bad'}"></span>Ramp ${snap.rampOpen ? 'open' : 'closed'}</span>`,
      `<span class="pill"><b>${snap.inAir}</b> in the air${snap.holding ? `, <b>${snap.holding}</b> holding` : ''}</span>`,
    ].join('');
  }

  async renderStation() {
    if (!this.api.station) return;
    const s = await this.api.station(this.run).catch(() => null);
    if (!s) { $('station').innerHTML = '<p class="muted">Station status is not available yet.</p>'; return; }
    const gates = (s.gates || []).map((g) => `<li class="gate ${g.status}" title="${esc(g.note || '')}">
        <b>${esc(g.gate)}</b><span>${g.status === 'closed' ? 'Closed' : g.flight_id ? esc(g.flight_id) : 'Free'}</span>
        ${g.status !== 'closed' && g.flight_id ? `<i class="${g.staffed ? 'ok' : 'bad'}">${g.staffed ? 'Staffed' : 'Needs staff'}</i>` : ''}</li>`).join('');
    const gaps = (s.staffing_gaps || []).map((x) => `<li><b>${x.short_by}</b> ${esc(String(x.role).replace(/_/g, ' '))} short, ${esc(x.from)} to ${esc(x.to)}</li>`).join('');
    $('station').innerHTML = `
      <div class="ramp ${s.ramp?.open ? 'ok' : 'bad'}"><span class="dot ${s.ramp?.open ? 'ok' : 'bad'}"></span>
        Ramp ${s.ramp?.open ? 'open' : 'closed'}${s.ramp?.reason ? `: ${esc(s.ramp.reason)}` : ''}${s.ramp?.since ? ` <span class="muted">since ${esc(s.ramp.since)}</span>` : ''}</div>
      <ul class="gates">${gates}</ul>
      <h3>Staffing gaps</h3>${gaps ? `<ul class="plain">${gaps}</ul>` : '<p class="muted">None right now.</p>'}`;
  }

  renderBoard(snap = state.day && snapshot(this.t)) {
    if (!snap) return;
    const day = state.day, t = this.t;
    const list = this.board === 'dep' ? snap.rows.dep : snap.rows.arr;
    const key = (r) => (this.board === 'dep' ? r.f.dep : r.f.arr);
    const live = (r) => ['holding', 'away', 'delayed', 'due'].includes(r.s.phase) || (r.s.phase === 'air' && this.board === 'arr');
    const rows = list.filter((r) => key(r) >= t - 40 || live(r) || this.open.has(r.f.id)).sort((a, b) => key(a) - key(b)).slice(0, 14);
    $('board').innerHTML = `<thead><tr><th>${this.board === 'dep' ? 'Leaves' : 'Arrives'}</th><th>Flight</th><th>${this.board === 'dep' ? 'To' : 'From'}</th><th>Status</th><th class="sr-only">Expand</th></tr></thead><tbody>` +
      rows.map((r) => {
        const city = this.board === 'dep' ? day.names[r.f.to] : day.names[r.f.from];
        const open = this.open.has(r.f.id), sel = state.selected === r.f.id;
        const main = `<tr data-id="${esc(r.f.id)}" tabindex="0" class="${open ? 'open' : ''}${sel ? ' selected' : ''}" aria-expanded="${open}">
          <td>${hhmm(key(r))}</td><td class="flight">${r.f.id}</td><td class="city">${esc(city)}</td><td><span class="status ${r.s.tone}">${esc(r.s.label)}</span></td>
          <td class="chev" aria-hidden="true">${open ? '▴' : '▾'}</td></tr>`;
        if (!open) return main;
        const d = this.flightCache[r.f.id];
        const facts = [
          ['Gate', d?.gate || 'Not set yet'], ['Scheduled', `${hhmm(r.f.dep)} → ${hhmm(r.f.arr)}`],
          ['Expected', d?.est_dep ? `${d.est_dep} → ${d.est_arr || '-'}` : (r.s.eta ? `lands ${hhmm(r.s.eta)}` : r.s.label)],
          ['Customers', d?.customers ?? r.f.pax ?? '-'], ['Connections', d?.connections ?? 'Not known'], ['Aircraft', r.f.aircraft || '-'],
          ['Crew', d?.crew ? `${d.crew.id}${d.crew.legal_until ? `, legal until ${d.crew.legal_until}` : ''}` : 'Not known'],
        ];
        return main + `<tr class="expand"><td colspan="5">
          <dl class="fl-facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
          ${d?.script?.text ? `<div class="script"><span class="k">What to tell Customers${d.script.updated ? `, updated ${esc(d.script.updated)}` : ''}</span><p>${esc(d.script.text)}</p></div>` : ''}
          ${d?.notes?.length ? `<ul class="notes">${d.notes.slice(-3).map((n) => `<li><b>${esc(n.t)}</b> ${esc(n.role)}: ${esc(n.text)}</li>`).join('')}</ul>` : ''}
          <div class="act-row"><button class="btn" type="button" data-follow="${esc(r.f.id)}">${sel ? 'Following on the map' : 'Show on the map'}</button>
          <button class="btn" type="button" data-notes="${esc(r.f.id)}">Add a note</button></div></td></tr>`;
      }).join('') + '</tbody>';
  }

  renderTracker(t) {
    const box = $('tracker');
    const f = state.selected && state.day && state.day.byId[state.selected];
    if (!f) { box.hidden = true; this.map.setSelection(null); return; }
    const s = track(f, t), names = state.day.names;
    this.map.setSelection({ id: f.id, dir: s.dir, tone: s.tone === 'warn' ? 'warn' : '', legs: s.legs, planned: s.legs.length > 1 ? s.planned : null, gate: s.gate });
    const total = (s.flown ?? 0) + (s.left ?? 0);
    const pct = s.phase === 'landed' || s.phase === 'holding' ? 100 : total ? Math.round(100 * s.flown / total) : 0;
    const leftT = s.eta != null ? s.eta - t : null;
    const facts = s.start != null
      ? [['Flown', `${Math.round(s.flown).toLocaleString()} mi`], ['To go', s.phase === 'holding' ? 'Circling' : `${Math.round(s.left).toLocaleString()} mi`],
        ['Time flying', dur((s.landed ?? t) - s.start)], ['Time left', s.phase === 'landed' ? 'Landed' : leftT != null ? dur(leftT) : 'Not known yet']]
      : [['Distance', `${Math.round(miles(f.from, f.to)).toLocaleString()} mi`], ['Flight time', dur(f.arr - f.dep)]];
    box.hidden = false;
    box.innerHTML = `<div class="trk-head"><div><span class="trk-id">${esc(f.id)}</span> <span class="status ${s.tone}">${esc(s.label)}</span></div>
        <button class="trk-close" type="button" data-close aria-label="Stop following ${esc(f.id)}">&times;</button></div>
      <div class="trk-route"><div><span class="trk-code">${esc(f.from)}</span><span class="trk-city">${esc(names[f.from] || '')}</span><span class="trk-time">${s.start != null ? `Left ${hhmm(s.start)}` : `Due out ${hhmm(f.dep)}`}</span></div>
        <div class="trk-right"><span class="trk-code">${esc(f.to)}</span><span class="trk-city">${esc(names[f.to] || '')}</span><span class="trk-time">${s.phase === 'landed' ? `Landed ${hhmm(s.landed)}` : s.eta != null ? `ETA ${hhmm(s.eta)}` : s.start != null ? 'ETA not known' : `Due in ${hhmm(f.arr)}`}</span></div></div>
      <div class="trk-bar ${s.tone === 'warn' ? 'warn' : ''}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="Share of the trip flown">
        <i style="width:${pct}%"></i><svg class="trk-plane" style="left:${pct}%" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${PLANE}</svg></div>
      <dl class="trk-facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
      ${s.note ? `<p class="trk-note">${esc(s.note)}</p>` : ''}`;
  }

  colors() { this.map.readColors(); }
}
