// Step 0: the command map on a normal day. Reads one simulated day from the read-only API and replays it.
import { NetworkMap } from './map.js';

const $ = (id) => document.getElementById(id);
const hhmm = (m) => { const v = Math.max(0, Math.round(m)); return `${String(Math.floor(v / 60) % 24).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`; };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const state = { day: null, t: 480, playing: false, speed: 5, board: 'dep', lastPanel: 0 };
const map = new NetworkMap($('map'), $('tooltip'));

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
  const preferred = runs.find((r) => r.mode === 'orchestrated') || runs[0];
  if (!preferred) {
    $('feed').innerHTML = '<li class="empty">No simulated day in the database yet. Run: python -m simulator.run --seed 7 --scenario a31_storm --mode orchestrated</li>';
    return;
  }
  sel.value = preferred.run_id;
  sel.addEventListener('change', () => loadDay(sel.value));
  await loadDay(preferred.run_id);
}

async function loadDay(runId) {
  const day = await (await fetch(`/api/day/${encodeURIComponent(runId)}`)).json();
  const names = Object.fromEntries(day.airports.map((a) => [a.code, a.name]));
  const evByFlight = {};
  for (const e of day.flight_events) (evByFlight[e.flight_id] ||= []).push(e);
  day.names = names;
  day.evByFlight = evByFlight;
  const served = new Set();
  const routes = [];
  for (const f of day.flights) {
    const other = f.from === 'MCI' ? f.to : f.from;
    if (!served.has(other)) { served.add(other); routes.push(['MCI', other]); }
  }
  map.setNetwork(day.airports.filter((a) => a.code === 'MCI' || served.has(a.code)), routes);
  state.day = day;
  state.lastPanel = 0;
}

// ---------------------------------------------------------------- the world at time t
function flightState(f, t) {
  const evs = (state.day.evByFlight[f.id] || []).filter((e) => e.t <= t);
  const last = (type) => [...evs].reverse().find((e) => e.type === type);
  const cancelled = last('flight_cancelled');
  const arrived = last('flight_arrived');
  const diverted = last('flight_diverted');
  const resumed = last('flight_resumed');
  const holding = last('flight_holding');
  const delayed = [...evs].reverse().find((e) => e.type === 'flight_delayed' || e.type === 'flight_delay_updated');
  const takeoff = f.dir === 'departure' ? last('flight_departed') : last('flight_airborne');
  const block = f.arr - f.dep;
  if (cancelled) return { phase: 'cancelled', label: 'Cancelled', tone: 'bad' };
  if (arrived) return { phase: 'landed', label: f.dir === 'departure' ? 'Departed' : 'Landed', tone: 'quiet', at: arrived.t };
  if (diverted && !(resumed && resumed.t > diverted.t)) return { phase: 'diverted', label: `Diverted to ${diverted.diverted_to}`, tone: 'warn' };
  if (holding && !(resumed && resumed.t > holding.t) && f.dir === 'arrival' && !arrived) {
    if (takeoff) return { phase: 'holding', label: 'Holding', tone: 'warn', takeoff: takeoff.t, block };
  }
  if (takeoff) {
    const from = resumed && f.dir === 'arrival' ? (diverted ? diverted.diverted_to : f.from) : f.from;
    const start = resumed && f.dir === 'arrival' ? resumed.t : takeoff.t;
    const dur = resumed && f.dir === 'arrival' ? 40 : block;
    return { phase: 'air', label: f.dir === 'departure' ? 'Departed' : 'In the air', tone: f.dir === 'departure' ? 'quiet' : 'info',
      from, start, dur, late: takeoff.delay_min || 0 };
  }
  if (delayed) return { phase: 'delayed', label: `Delayed to ${delayed.new_departure || hhmm(f.dep + (delayed.delay_min || 0))}`, tone: 'warn' };
  if (t > f.dep + 5) return { phase: 'due', label: 'Departure due', tone: 'warn' };
  return { phase: 'scheduled', label: 'On time', tone: 'ok' };
}

function snapshot(t) {
  const day = state.day;
  const planes = [], labels = new Set();
  let inAir = 0, depDone = 0, depOnTime = 0;
  const rows = { dep: [], arr: [] };
  for (const f of day.flights) {
    const s = flightState(f, t);
    if (f.dir === 'departure') {
      const dep = (day.evByFlight[f.id] || []).find((e) => e.type === 'flight_departed' && e.t <= t);
      if (dep) { depDone++; if ((dep.delay_min || 0) <= 15) depOnTime++; }
      rows.dep.push({ f, s });
    } else rows.arr.push({ f, s });
    if (s.phase === 'air') {
      const p = Math.min(0.98, Math.max(0.01, (t - s.start) / s.dur));
      inAir++;
      planes.push({ id: f.id, from: s.from, to: f.to, dir: f.dir === 'departure' ? 'out' : 'in', progress: p,
        fromName: day.names[s.from] || s.from, toName: day.names[f.to] || f.to, status: s.label });
      labels.add(f.dir === 'departure' ? f.to : s.from);
    }
  }
  const station = [...day.station].reverse().find((x) => x.t <= t) || day.station[0] || {};
  const world = day.world.filter((w) => w.t <= t);
  const stop = [...world].reverse().find((w) => w.type.startsWith('ground_stop'));
  const stopActive = stop && stop.type !== 'ground_stop_ended';
  const ramp = [...world].reverse().find((w) => w.type.startsWith('ramp_'));
  const rampOpen = ramp ? ramp.type === 'ramp_reopened' : (station.ramp_open ?? true);
  const nextHour = (dir) => day.flights.filter((f) => f.dir === dir && (dir === 'departure' ? f.dep : f.arr) > t && (dir === 'departure' ? f.dep : f.arr) <= t + 60).length;
  return { planes, labels, inAir, depDone, depOnTime, rows, station, stopActive, rampOpen,
    depNext: nextHour('departure'), arrNext: nextHour('arrival') };
}

// ---------------------------------------------------------------- panels
function renderPanels(t, snap) {
  const day = state.day;
  $('status-chips').innerHTML = [
    `<span class="pill"><span class="dot ${snap.stopActive ? 'bad' : 'ok'}"></span>Operations: <b>${snap.stopActive ? 'Ground stop' : 'Normal'}</b></span>`,
    `<span class="pill"><span class="dot ${snap.rampOpen ? 'ok' : 'bad'}"></span>Ramp: <b>${snap.rampOpen ? 'Open' : 'Closed'}</b></span>`,
    `<span class="pill"><span class="dot ${snap.stopActive ? 'warn' : 'ok'}"></span>Weather at MCI: <b>${snap.stopActive ? 'Storms' : 'Clear'}</b></span>`,
  ].join('');

  const onTime = snap.depDone ? Math.round(100 * snap.depOnTime / snap.depDone) + '%' : '-';
  const people = snap.station.people != null ? snap.station.people.toLocaleString() : '-';
  const cap = snap.station.capacity ? Math.round(100 * snap.station.people / snap.station.capacity) : null;
  $('stats').innerHTML = [
    ['Departures on time', onTime, `${snap.depDone} of ${day.flights.filter((f) => f.dir === 'departure').length} have left`],
    ['In the air now', snap.inAir, 'to or from MCI'],
    ['Departing next hour', snap.depNext, 'scheduled'],
    ['Arriving next hour', snap.arrNext, 'scheduled'],
    ['People in the terminal', people, cap != null ? `${cap}% of capacity` : ''],
    ['Crews flagged at 05:00', day.roster_flags, 'roster rest under 10 h'],
  ].map(([l, v, s]) => `<div class="stat"><span class="stat-label">${l}</span><span class="stat-value">${v}</span><span class="stat-sub">${s}</span></div>`).join('');

  const list = state.board === 'dep' ? snap.rows.dep : snap.rows.arr;
  const key = (r) => (state.board === 'dep' ? r.f.dep : r.f.arr);
  const upcoming = list.filter((r) => key(r) >= t - 40).sort((a, b) => key(a) - key(b)).slice(0, 9);
  $('board').innerHTML = `<thead><tr><th>${state.board === 'dep' ? 'Leaves' : 'Arrives'}</th><th>Flight</th><th>${state.board === 'dep' ? 'To' : 'From'}</th><th>Status</th></tr></thead><tbody>` +
    upcoming.map((r) => {
      const city = state.board === 'dep' ? day.names[r.f.to] : day.names[r.f.from];
      const past = (r.s.phase === 'landed' || r.s.phase === 'air') && state.board === 'dep';
      return `<tr class="${past ? 'past' : ''}"><td>${hhmm(key(r))}</td><td class="flight">${r.f.id}</td><td>${esc(city)}</td><td><span class="status ${r.s.tone}">${esc(r.s.label)}</span></td></tr>`;
    }).join('') + '</tbody>';

  const recent = day.activity.filter((a) => a.t <= t).slice(-7).reverse();
  $('feed').innerHTML = recent.length
    ? recent.map((a) => `<li><span class="t">${hhmm(a.t)}</span><span class="who">${esc(a.who)}</span><span class="what">${esc(a.text)}</span></li>`).join('')
    : '<li class="empty">Nothing yet today.</li>';

  const tick = [...day.orchestrator].reverse().find((o) => o.t <= t);
  if (!day.orchestrator.length) {
    $('queue-note').textContent = 'Co-pilot is off in this run (today\'s process only)';
    $('agents').innerHTML = '<div class="agent"><span class="agent-name">Co-pilot</span><span class="agent-state">Not running in this run</span></div>';
  } else {
    $('queue-note').textContent = tick ? `${tick.queue} waiting for a person` : '';
    const lastRan = {};
    for (const o of day.orchestrator) { if (o.t > t) break; for (const a of o.ran) lastRan[a] = o.t; }
    const agents = ['situation', 'network_recovery', 'passenger_care', 'station_staff', 'communication'];
    $('agents').innerHTML = agents.map((a) => {
      const ran = lastRan[a];
      const fresh = ran != null && t - ran <= 6;
      return `<div class="agent"><span class="agent-name"><span class="dot ${fresh ? 'ok' : 'warn'}"></span>${esc(day.agents[a])}</span><span class="agent-state">${ran != null ? 'checked at ' + hhmm(ran) : 'waiting to start'}</span></div>`;
    }).join('') + `<div class="agent"><span class="agent-name"><span class="dot ok"></span>Orchestrator</span><span class="agent-state">${tick ? (tick.incidents.length ? tick.incidents.length + ' open incident' : 'no open incidents') : 'waiting to start'}</span></div>`;
  }
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
    if (now - state.lastPanel > 300) { renderPanels(state.t, snap); state.lastPanel = now; }
  }
  map.draw(now);
  requestAnimationFrame(frame);
}

applyThemeLabel();
setTime(480);
loadRuns().catch((e) => { $('feed').innerHTML = `<li class="empty">Could not load data: ${esc(e.message)}</li>`; });
requestAnimationFrame(frame);
