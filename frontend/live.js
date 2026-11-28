// Groundwork: one app for the station. The user is the human in the loop for every decision
// the agents cannot make alone; legally required small actions (refunds) are done automatically and logged.
import { pickApi, ROLE_TITLES, FRONTLINE } from './live-api.js';
import { icon } from './icons.js';
import { drawDecisionFlow } from './graph.js';
import { OpsView } from './ops.js';
import { oneStoryHtml } from './story.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (n) => Number(n || 0).toLocaleString();
const CHANNELS = { app: 'App', sms: 'Text message', gate_screen: 'Gate screen', call_center: 'Call center' };
const ALL_ROLES = { ...ROLE_TITLES, ...FRONTLINE, director_of_operations: 'Duty manager (sees everything)' };
const NEEDS = { wheelchair: 'Wheelchair', unaccompanied_minor: 'Unaccompanied minor' };

const ui = { api: null, run: null, state: null, items: [], selected: null, role: 'director_of_operations', tab: 'ops',
  series: [], traceFor: null, mode: null, busy: false, decided: new Map(), ops: null, custSel: null, choice: {}, noteFlight: null };

// ---------------------------------------------------------------- basics
function theme() { return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
function themeLabel() { $('theme').textContent = theme() === 'dark' ? 'Light mode' : 'Dark mode'; ui.ops && ui.ops.colors(); }
$('theme').addEventListener('click', () => {
  const next = theme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('gsc-theme', next); } catch (e) {}
  themeLabel();
});
themeLabel();
function toast(text, bad = false) {
  const t = $('toast');
  t.textContent = text; t.hidden = false; t.classList.toggle('bad', bad);
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 4200);
}
const agentName = (k) => ({ situation: 'Situation', network_recovery: 'Network Recovery', passenger_care: 'Passenger Care', station_staff: 'Station & Staff', communication: 'Communication', orchestrator: 'Orchestrator' }[k] || k);
const kindName = (k) => ({ situation: 'weather and operations picture', recovery_plan: 'flight and crew plan', care_plan: 'Customer care plan', station_plan: 'station plan', communication: 'message',
  network_recovery: 'flight and crew plan', passenger_care: 'Customer care plan', station_staff: 'station plan' }[k] || 'update');
const listWords = (a) => a.length < 2 ? a.join('') : `${a.slice(0, -1).join(', ')} and ${a[a.length - 1]}`;
const plain = (s) => String(s ?? '').replace(/\b[a-z]+(_[a-z]+)+\b/g, (w) => w.replace(/_/g, ' '))
  .replace(/\b[Tt]he co-pilot's\b/g, "Groundwork's").replace(/\b[Tt]he co-pilot\b/g, 'Groundwork').replace(/\b[Cc]o-pilot\b/g, 'Groundwork');

// ---------------------------------------------------------------- start
async function showPast() {
  const list = await ui.api.sessions().catch(() => []);
  $('past').innerHTML = list.length ? list.map((s) => `<li><button class="link" data-run="${esc(s.run_id)}" type="button">${esc(s.run_id)}</button>
    <span class="muted">${esc(s.status)}${s.started_at ? ', started ' + new Date(s.started_at).toLocaleString() : ''}</span></li>`).join('') : '<li class="muted">None yet.</li>';
  $('past').querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => open(b.dataset.run)));
}
$('start-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!ui.api) { toast('Still connecting to the database. Try again in a few seconds.', true); return; }
  const random = new FormData(e.target).get('day') === 'random';
  const body = { seed: random ? Math.floor(Math.random() * 9000) + 100 : 7, scenario: random ? 'random' : 'a31_storm', roles: Object.keys(ROLE_TITLES) };
  const btn = e.target.querySelector('button[type="submit"]');
  btn.disabled = true; btn.textContent = 'Starting the day…';
  try { const r = await ui.api.start(body); open(r.run_id); }
  catch (err) { toast(`Could not start: ${err.message}`, true); }
  finally { btn.disabled = false; btn.textContent = 'Start the day'; }
});

function open(runId) {
  ui.run = runId;
  $('start').hidden = true; $('live').hidden = false; $('top-live').hidden = false; $('role-field').hidden = false; $('report-btn').hidden = false; $('popups-btn').hidden = false;
  history.replaceState(null, '', `?run=${encodeURIComponent(runId)}${ui.api.practice ? '&mock=1' : ''}`);
  $('role').innerHTML = Object.entries(ALL_ROLES).map(([k, v]) => `<option value="${k}" ${k === ui.role ? 'selected' : ''}>${esc(v)}</option>`).join('');
  ui.ops = new OpsView({ api: ui.api, run: runId, onFlightNotes: openNote, onFlightCustomers: openFlight });
  loop();
}
$('role').addEventListener('change', () => {
  ui.role = $('role').value;
  const front = ui.role === 'counter_gate_agent';
  document.querySelector('[data-tab="decisions"]').hidden = front;
  if (front && ui.tab === 'decisions') setTab('customers');
  ui.selected = null; ui.detailSig = null; refreshQueue();
});
function setTab(name) {
  ui.tab = name;
  document.querySelectorAll('[data-tab]').forEach((x) => { x.classList.toggle('active', x.dataset.tab === name); x.setAttribute('aria-selected', x.dataset.tab === name); });
  for (const p of ['ops', 'decisions', 'customers', 'clocks', 'summary', 'flow']) $(`tab-${p}`).hidden = p !== name;
  if (name === 'summary') refreshSummary();
  if (name === 'clocks') refreshClocks();
  if (name === 'flow') refreshFlow();
  if (name === 'ops' && ui.state) ui.ops.update(ui.state.minute);
}
document.querySelectorAll('[data-tab]').forEach((t) => t.addEventListener('click', () => setTab(t.dataset.tab)));
$('truth-strip').addEventListener('click', () => { setTab('customers'); refreshOod(); $('ood').scrollIntoView({ behavior: 'smooth', block: 'start' }); });

$('pause').addEventListener('click', async () => {
  ui.state = await ui.api.control(ui.run, { action: ui.state && ui.state.status === 'running' ? 'pause' : 'resume' });
  renderState();
});
for (const id of ['speed-idle', 'speed-pending']) {
  $(id).addEventListener('change', async () => {
    ui.state = await ui.api.control(ui.run, { action: 'speed', speed_idle: Number($('speed-idle').value), speed_pending: Number($('speed-pending').value) });
  });
}

// ---------------------------------------------------------------- report from the floor, notes
$('report-btn').addEventListener('click', () => $('report-dlg').showModal());
$('report-cancel').addEventListener('click', () => $('report-dlg').close());
$('report-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const gate = String(fd.get('gate') || '').trim().toUpperCase() || null, flight = String(fd.get('flight_id') || '').trim().toUpperCase().replace(/^(\d)/, 'WN$1') || null;
  if (gate && !/^[A-Z]{1,2}\d{1,3}$/.test(gate)) { toast('Gate should look like B53 or G05.', true); return; }
  if (flight && !/^WN\d{3,4}$/.test(flight)) { toast('Flight should look like WN1031.', true); return; }
  const body = { kind: fd.get('kind'), gate, flight_id: flight, text: fd.get('text'), role: ui.role };
  try { await ui.api.report(ui.run, body); toast('Report sent. Groundwork is taking it into account.'); $('report-dlg').close(); e.target.reset(); }
  catch (err) { toast(`Could not send the report: ${err.message}`, true); }
});
function openNote(flightId) { ui.noteFlight = flightId; $('note-title').textContent = `Note on ${flightId}`; $('note-dlg').showModal(); }
$('note-cancel').addEventListener('click', () => $('note-dlg').close());
$('note-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = new FormData(e.target).get('text');
  try { await ui.api.addNote(ui.run, ui.noteFlight, { text, role: ui.role }); toast(`Note added to ${ui.noteFlight}.`); $('note-dlg').close(); e.target.reset(); ui.ops.fetchFlight(ui.noteFlight); }
  catch (err) { toast(`Could not add the note: ${err.message}`, true); }
});

// ---------------------------------------------------------------- live state
function renderState() {
  const s = ui.state;
  if (!s) return;
  $('clock').textContent = s.clock;
  $('status').textContent = s.status === 'running' ? (s.pending ? `Slowed: ${s.pending} waiting` : 'Running') : s.status === 'paused' ? 'Paused' : s.status === 'finished' ? 'Day finished' : s.status;
  $('status').className = `status-line ${s.status}`;
  $('pause').textContent = s.status === 'running' ? 'Pause' : 'Resume';
  $('pause').disabled = !['running', 'paused'].includes(s.status);
  $('speed-idle').value = String(s.speed_idle);
  const sp = [...$('speed-pending').options].map((o) => Number(o.value)).reduce((a, b) => Math.abs(b - s.speed_pending) < Math.abs(a - s.speed_pending) ? b : a);
  $('speed-pending').value = String(sp);
  const w = s.world || {};
  $('world').innerHTML = [
    `<span class="pill"><span class="dot ${w.ground_stop ? 'bad' : 'ok'}"></span>${w.ground_stop ? `Ground stop, likely until ${esc(w.stop_expected_end || '?')}` : 'Normal operations'}</span>`,
    `<span class="pill"><span class="dot ${w.ramp_open ? 'ok' : 'bad'}"></span>Ramp ${w.ramp_open ? 'open' : 'closed'}</span>`,
  ].join('');
  $('count').textContent = s.pending;
  $('count').classList.toggle('hot', s.urgent_pending > 0);

  const t = s.truth || {}, f = s.focus;
  const wrongScreens = f ? f.screens.filter((x) => x.wrong) : [];
  $('truth-strip').className = `truth-strip ${t.wrong_now ? 'bad' : 'ok'}`;
  $('truth-strip').innerHTML = t.wrong_now
    ? `<span class="dot bad"></span><span class="ts-text"><b>${num(t.wrong_now)} Customers</b> are seeing out-of-date information${wrongScreens.length ? `, including ${esc(f.name)}'s ${listWords(wrongScreens.map((x) => (CHANNELS[x.channel] || x.channel).toLowerCase()))}` : ''}.</span> <u>See who</u>`
    : `<span class="dot ok"></span><span class="ts-text">Every Customer's screens match the truth right now.</span>`;
  $('wrong-now').textContent = num(t.wrong_now);
  $('wrong-now-sub').textContent = t.wrong_now ? 'until the latest updates reach their screens' : 'every screen matches the truth';
  $('wrong-total').textContent = num(t.wrong_minutes_total);
  $('wrong-total-sub').textContent = 'one Customer on one wrong screen for one minute = 1';
  const last = ui.series[ui.series.length - 1];
  if (!last || last.m !== s.minute) ui.series.push({ m: s.minute, you: t.wrong_now || 0 });
  if (ui.series.length > 600) ui.series.shift();
  const pts = ui.series;
  if (pts.length > 1) {
    const m0 = pts[0].m, m1 = Math.max(pts[pts.length - 1].m, m0 + 60), max = Math.max(10, ...pts.map((p) => p.you)) * 1.1;
    $('spark').innerHTML = `<polyline class="sp-you" points="${pts.map((p) => `${((p.m - m0) / (m1 - m0) * 300).toFixed(1)},${(70 - p.you / max * 66).toFixed(1)}`).join(' ')}"/>`;
  }
  if (f) {
    $('focus-name').textContent = f.name || f.passenger_id;
    $('focus-truth').textContent = `Right now: ${plain(f.true_status)}`;
    $('screens').innerHTML = f.screens.map((sc) => `<div class="screen ${sc.wrong ? 'wrong' : ''}">
      <span class="screen-ch">${esc(CHANNELS[sc.channel] || sc.channel)}</span><span class="screen-shows">${esc(plain(sc.shows))}</span>
      <span class="screen-state">${sc.wrong ? `Out of date since ${esc(sc.since)}` : `Correct since ${esc(sc.since)}`}</span></div>`).join('');
  }
}

// ---------------------------------------------------------------- decisions: queue by event, urgent first
const visible = () => ui.items.filter((x) => ui.role === 'director_of_operations' || x.role === ui.role);
const itemGroup = (it) => ui.items.filter((x) => x.group_key === it.group_key);
const headline = (it) => it.headline || plain(it.title);

function renderQueue() {
  const items = visible();
  $('queue-roles').textContent = ui.role === 'director_of_operations' ? 'Showing every role. Pick your role at the top to see only yours.' : `Showing decisions for ${ALL_ROLES[ui.role]}.`;
  if (!items.length) {
    $('qlist').innerHTML = `<p class="empty">${ui.state && ui.state.status === 'finished' ? 'The day is over. See the day summary.' : 'Nothing waiting for you. The clock runs at full speed until something comes in.'}</p>`;
  } else {
    const bundles = new Map();
    for (const it of items) {
      const b = it.bundle || { id: 'b0', title: 'Decisions', started: '' };
      if (!bundles.has(b.id)) bundles.set(b.id, { b, urgent: [], routine: [] });
      (((it.section || (it.urgent || it.safety_legal ? 'urgent' : 'routine')) === 'urgent') ? bundles.get(b.id).urgent : bundles.get(b.id).routine).push(it);
    }
    const row = (it) => {
      const n = it.group_size > 1 ? itemGroup(it).length : 1;
      const sel = ui.selected === it.id || (n > 1 && itemGroup(it).some((x) => x.id === ui.selected));
      return `<li><button type="button" class="qitem ${sel ? 'selected' : ''} ${it.safety_legal ? 'safety' : ''}" data-id="${it.id}" aria-pressed="${sel}">
        <span class="q-top"><span class="q-icon">${icon(it.agent)}</span>
          <span class="q-title">${esc(n > 1 ? `${headline(it).replace(/ (to|for) (gate )?[A-Z]?\d+.*$/, '')} (${n})` : headline(it))}</span>
          <span class="q-time ${it.minutes_left <= 3 ? 'late' : ''}">${it.status === 'escalated' ? 'Escalated' : `${it.minutes_left} min`}</span></span>
        <span class="q-tags">${it.safety_legal ? '<span class="tagx bad">Safety or legal</span>' : ''}${it.passengers_seeing_wrong_info_now ? `<span class="tagx focus">${num(it.passengers_seeing_wrong_info_now)} Customers seeing old info</span>` : ''}
          ${it.status === 'snoozed' ? `<span class="tagx quiet">Back at ${esc(it.snoozed_until)}</span>` : ''}<span class="tagx quiet">${esc(ROLE_TITLES[it.role] || it.role_title)}</span></span></button></li>`;
    };
    const dedupe = (list) => { const seen = new Set(); return list.filter((it) => { if (it.group_size > 1) { if (seen.has(it.group_key)) return false; seen.add(it.group_key); } return true; }); };
    $('qlist').innerHTML = [...bundles.values()].map(({ b, urgent, routine }) => `<section class="bundle">
      <h3 class="bundle-title">${esc(b.title)} <span class="muted">${urgent.length + routine.length} for you</span></h3>
      ${urgent.length ? `<p class="sec-label bad">Urgent and safety</p><ol class="qgroup">${dedupe(urgent).map(row).join('')}</ol>` : ''}
      ${routine.length ? `<p class="sec-label">Routine</p><ol class="qgroup">${dedupe(routine).map(row).join('')}</ol>` : ''}</section>`).join('');
    $('qlist').querySelectorAll('[data-id]').forEach((b) => b.addEventListener('click', () => select(Number(b.dataset.id))));
  }
  if (!items.some((x) => x.id === ui.selected) && !ui.decided.has(ui.selected)) select(items[0] ? items[0].id : null, true);
  else updateDetail();
}
function current() {
  const live = ui.items.find((x) => x.id === ui.selected);
  if (live) return { it: live, rec: null };
  const rec = ui.decided.get(ui.selected);
  return rec ? { it: rec.item, rec } : { it: null, rec: null };
}
function select(id, quiet = false) {
  if (ui.selected !== id) { ui.mode = null; ui.traceFor = null; }
  ui.selected = id;
  if (!quiet) renderQueue(); else renderDetail();
}
function detailSig() {
  const { it, rec } = current();
  return it ? [it.id, it.status, rec ? `${rec.answer}:${visible().length > 0}` : itemGroup(it).length, it.can_wait, it.last_option, ui.mode].join('|') : 'none';
}
function updateDetail() {
  const { it, rec } = current();
  if (detailSig() !== ui.detailSig) { renderDetail(); return; }
  if (!it || rec) return;
  const left = $('detail').querySelector('.d-left');
  if (left) left.textContent = it.status === 'escalated' ? 'Escalated' : `${it.minutes_left} min`;
  $('detail').querySelector('.d-clock')?.classList.toggle('late', it.minutes_left <= 3);
}

function renderDetail() {
  const box = $('detail');
  const { it, rec } = current();
  ui.detailSig = detailSig();
  if (!it) { box.innerHTML = `<div class="detail-empty">${icon('orchestrator', 'ico big')}<p>When Groundwork needs you, the situation, its suggestion and your options appear here.</p></div>`; return; }
  const group = rec ? rec.group : it.group_size > 1 ? itemGroup(it) : [it];
  const w = it.why || {};
  const story = it.story?.length ? it.story : (w.trigger || []).map((x) => ({ t: x.t, text: x.text }));
  const options = it.options?.length ? it.options : [{ id: 'opt1', label: headline(it), why: it.recommendation || w.plan, recommended: true }];
  const chosen = rec?.optionId || ui.choice[it.id] || options.find((o) => o.recommended)?.id || options[0].id;
  const ifNo = it.if_declined || it.consequence_if_rejected || 'Groundwork comes back with a different plan.';
  const changes = it.allowed_changes || null;
  const ANSWER = { approved: 'Approved', rejected: 'Declined', hand_off: 'Sent to the duty manager' };
  box.innerHTML = `
    <header class="d-head">
      <div><p class="d-role">${icon(it.agent)} ${esc(agentName(it.agent))} needs <b>${esc(ROLE_TITLES[it.role] || it.role_title)}</b></p><h2>${esc(headline(it))}</h2></div>
      ${rec ? `<div class="d-clock done ${rec.answer}"><span class="d-left">${rec.answer === 'approved' && rec.notSuggested ? 'You chose' : esc(ANSWER[rec.answer] || rec.answer)}</span><span class="muted">${rec.answer === 'approved' && rec.notSuggested ? `${esc(plain(rec.optionLabel))}, ` : ''}at ${esc(rec.at)}</span></div>`
        : `<div class="d-clock ${it.minutes_left <= 3 ? 'late' : ''}"><span class="d-left">${it.status === 'escalated' ? 'Escalated' : `${it.minutes_left} min`}</span><span class="muted">${it.status === 'escalated' ? 'with the duty manager' : `to decide, by ${esc(it.deadline)}`}</span></div>`}
    </header>
    ${it.effect?.text || it.passengers_affected ? `<p class="d-effect">${esc(String(it.effect?.text || `${num(it.passengers_affected)} Customers affected`).replace(/\.+$/, ''))}${it.safety_legal ? ' <span class="tagx bad">Safety or legal</span>' : ''}</p>` : ''}
    <section class="d-sec"><h3>What's going on</h3>
      <ol class="story">${story.map((x) => `<li><span class="t">${esc(x.t)}</span><span>${esc(plain(x.text))}</span></li>`).join('') || '<li class="muted">No details recorded.</li>'}</ol></section>
    <section class="d-sec"><h3>${options.length > 1 ? 'Your options' : 'Groundwork suggests'}</h3>
      <div class="options" role="radiogroup" aria-label="Options">${options.map((o) => `<label class="option ${o.id === chosen ? 'on' : ''}">
        <input type="radio" name="opt-${it.id}" value="${esc(o.id)}" ${o.id === chosen ? 'checked' : ''} ${rec || options.length === 1 ? 'disabled' : ''}>
        <span class="opt-body"><span class="opt-label">${esc(plain(o.label))}${o.recommended ? ' <span class="tagx ok">Suggested</span>' : ''}</span>
        <span class="opt-why">${esc(plain(o.why || ''))}</span>${o.effect ? `<span class="opt-eff">${esc(o.effect)}</span>` : ''}</span></label>`).join('')}</div>
      ${!rec && changes && Object.values(changes).some((v) => Array.isArray(v) && v.every((x) => typeof x !== 'object')) ? `<details class="adjust"><summary>Change something before approving</summary><div class="adjust-row">${Object.entries(changes).filter(([, v]) => Array.isArray(v) && v.every((x) => typeof x !== 'object')).map(([k, vals]) => `<label class="field"><span class="field-label">${({ delay_min: 'Extra delay (minutes)', gate: 'Gate', crew_id: 'Crew', extension_min: 'Duty extension (minutes)', message_text: 'Message wording' })[k] || plain(k)}</span>
        <select data-change="${esc(k)}"><option value="">Keep as suggested</option>${vals.map((v) => `<option>${esc(v)}</option>`).join('')}</select></label>`).join('')}</div></details>` : ''}</section>
    ${careHtml(it)}
    <section class="d-sec warnbox"><h3>${it.last_option ? 'This is the last option' : 'If you say no'}</h3><p>${esc(plain(ifNo))}</p></section>
    <section class="d-sec"><h3>${rec ? 'What happened next' : 'How this reached you, and where it goes'}</h3>
      <div class="trace" id="trace"><p class="muted">Loading…</p></div><p class="trace-note muted" id="trace-note">Click any box for details.</p>
      <p class="in-short" id="in-short"></p></section>
    <footer class="actions">${rec
      ? `<div class="act-row">${visible().length ? `<button class="btn primary" id="next" type="button">Next decision (${visible().length} waiting)</button>` : '<span class="muted">Nothing else is waiting for you.</span>'}</div>`
      : ui.mode === 'decline' ? `<label class="reason"><span>Why not? Groundwork uses this to look for something else.</span><textarea id="reason" rows="2"></textarea></label>
        <div class="act-row"><button class="btn danger" id="do-decline" type="button">Decline${group.length > 1 ? ` all ${group.length}` : ''}</button><button class="btn" id="back" type="button">Back</button></div>`
      : `<div class="act-row">
          <button class="btn primary" id="do-approve" type="button">${group.length > 1 ? `Approve all ${group.length}` : 'Approve'}</button>
          <button class="btn" id="start-decline" type="button">Decline</button>
          <button class="btn" data-wait="10" type="button" ${it.can_wait ? '' : 'disabled'}>Remind me in 10 min</button>
          <button class="btn" id="do-handoff" type="button" ${it.can_hand_off === false ? `disabled title="${esc(it.hand_off_reason || 'Only the named role can decide this one')}"` : ''}>Send to duty manager</button></div>
        ${it.can_hand_off === false ? `<p class="muted small">Can't send this to the duty manager: ${esc(plain(it.hand_off_reason || 'only the named role can decide it'))}.</p>` : ''}
        ${it.can_wait ? '' : `<p class="blocked">Can't wait: ${esc((it.blocked_by || []).map((b) => b.title).join(', '))} depends on this.</p>`}`}
    </footer>`;
  const ids = group.map((g) => g.id);
  const approveLabel = () => {
    const b = box.querySelector('#do-approve'); if (!b) return;
    const o = options.find((x) => x.id === (ui.choice[it.id] || chosen));
    b.textContent = group.length > 1 ? `Approve all ${group.length}` : o && !o.recommended && options.length > 1 ? `Approve: ${plain(o.label)}` : 'Approve';
  };
  box.querySelectorAll(`input[name="opt-${it.id}"]`).forEach((r) => r.addEventListener('change', () => { ui.choice[it.id] = r.value; box.querySelectorAll('.option').forEach((o) => o.classList.toggle('on', o.querySelector('input').checked)); approveLabel(); }));
  approveLabel();
  box.querySelectorAll('[data-cust]').forEach((b) => b.addEventListener('click', () => { setTab('customers'); showCustomer(b.dataset.cust); }));
  box.querySelector('#next')?.addEventListener('click', () => { ui.selected = null; renderQueue(); });
  box.querySelector('#do-approve')?.addEventListener('click', () => {
    const change = {};
    box.querySelectorAll('[data-change]').forEach((s) => { if (s.value) change[s.dataset.change] = isNaN(Number(s.value)) ? s.value : Number(s.value); });
    const opt = ui.choice[it.id];
    const recommended = options.find((o) => o.recommended)?.id || options[0].id;
    if (Object.keys(change).length) decide(ids, { action: 'approve_with_change', change, option_id: opt || recommended }, 'Approved with your change.');
    else if (opt && opt !== recommended) decide(ids, { action: 'approve_option', option_id: opt }, 'Approved your chosen option.');
    else decide(ids, { action: 'approve' }, 'Approved.');
  });
  box.querySelector('#start-decline')?.addEventListener('click', () => { ui.mode = 'decline'; renderDetail(); box.querySelector('#reason').focus(); });
  box.querySelector('#back')?.addEventListener('click', () => { ui.mode = null; renderDetail(); });
  box.querySelector('#do-decline')?.addEventListener('click', () => {
    const reason = box.querySelector('#reason').value.trim();
    if (!reason) { toast('Add a short reason so Groundwork knows what to change.', true); return; }
    decide(ids, { action: 'reject', reason }, 'Declined. Groundwork will look for something else.');
  });
  box.querySelectorAll('[data-wait]').forEach((b) => b.addEventListener('click', () => decide([it.id], { action: 'wait', minutes: Number(b.dataset.wait) }, `It'll come back in ${b.dataset.wait} minutes, or sooner if its deadline gets close.`)));
  box.querySelector('#do-handoff')?.addEventListener('click', () => decide(ids, { action: 'hand_off' }, 'Sent to the duty manager.'));
  loadTrace(it);
}


// One plain paragraph from what the flowchart shows; nothing added that is not on screen.
function inShort(it, rec, m) {
  const parts = [];
  const first = (it.story || [])[0];
  if (first) parts.push(`${first.t}: ${plain(first.text).replace(/\.$/, '')}.`);
  parts.push(`${agentName(it.agent)} asked ${ROLE_TITLES[it.role] || it.role_title || 'a person'} to decide: ${plain(headline(it)).replace(/\.$/, '')}.`);
  if (!rec) parts.push(it.status === 'escalated' ? 'It went to the duty manager because the deadline passed.' : `It is waiting for you until ${it.deadline}.`);
  else if (rec.answer === 'approved') parts.push(rec.notSuggested ? `You chose "${plain(rec.optionLabel)}" at ${rec.at}.` : `You approved at ${rec.at}${rec.change ? ' with a change' : ''}.`);
  else if (rec.answer === 'rejected') parts.push(`You declined at ${rec.at}${rec.reason ? ` ("${rec.reason}")` : ''}; Groundwork looks for something else.`);
  else parts.push(`You sent it to the duty manager at ${rec.at}.`);
  if (!m.preview && m.changes.length) parts.push(`What changed: ${m.changes.map((c) => plain(c.label)).join('; ')}.`);
  const delivered = m.dests.filter((d) => d.status === 'delivered');
  if (m.preview && m.dests.length) parts.push(`If approved, ${m.dests.length} places will get the same update at the same time.`);
  else if (!m.preview && m.dests.length) {
    const last = delivered.map((d) => String(d.t || '').match(/\d\d:\d\d/)?.[0]).filter(Boolean).sort().pop();
    parts.push(`One update went to ${listWords(m.dests.map((d) => d.label))}: ${delivered.length} of ${m.dests.length} confirmed${last ? ` by ${last}` : ''}.`);
  } else if (!m.preview && rec?.answer === 'approved') parts.push('No messages were needed.');
  return parts.join(' ');
}

// The Customers inside a Passenger Care decision: who gets what, and the exact words they will receive.
function careHtml(it) {
  const c = it.care;
  if (!c || !c.customers?.length) return '';
  const money = (n) => `$${Number(n || 0).toLocaleString()}`;
  const what = (x) => {
    const f = (x.detail?.flights || []).map((fl) => `${fl.flight_id} ${fl.dep}${fl.arr ? `, arrives ${fl.arr}` : ''}`).join(' then ');
    if (x.action === 'hotel') return `${f ? `${f}, ` : ''}hotel: ${x.detail?.hotel?.name || ''}${x.detail?.hotel?.price ? ` (${money(x.detail.hotel.price)})` : ''}`;
    if (x.action === 'refund') return x.detail?.amount != null ? `Refund ${money(x.detail.amount)}` : 'Refund of the unused fare';
    if (x.action === 'ride') return `Ride home${x.detail?.amount ? ` (${money(x.detail.amount)})` : ''}`;
    return f;
  };
  return `<section class="d-sec"><h3>Customers in this decision</h3>
    <ul class="care-list">${c.customers.map((x) => `<li><div><b>${esc(x.name)}</b> <span class="tagx quiet">${esc({ rebook: 'Rebook', hotel: 'Rebook + hotel', refund: 'Refund', ride: 'Ride' }[x.action] || x.action)}</span>
      <br>${esc(what(x))}<br><span class="muted">${esc(plain(x.why))}</span></div>
      <button class="btn" type="button" data-cust="${esc(x.id)}">Open Customer</button></li>`).join('')}</ul>
    <div class="script"><span class="k">What they'll be told</span><p>${c.message_text ? esc(c.message_text) : '<span class="muted">Communication writes the exact words as soon as you approve; they will show here and in the message pop-ups.</span>'}</p></div>
    ${(c.auto_done || []).length ? `<p class="muted small">Already done automatically today: ${c.auto_done.map((a) => `${a.count} ${a.action}${a.count > 1 ? 's' : ''}${a.total ? ` (${money(a.total)})` : ''}`).join(', ')}.</p>` : ''}</section>`;
}

async function decide(ids, body, done) {
  if (ui.busy) return;
  ui.busy = true;
  try {
    const group = ids.map((id) => ui.items.find((x) => x.id === id)).filter(Boolean);
    const r = await ui.api.decide(ui.run, { question_ids: ids, ...body });
    const errs = r.results.filter((x) => x.error);
    if (errs.length) toast(errs.map((x) => x.error).join('; '), true); else toast(done);
    if (body.action !== 'wait' && group.length && !errs.length) {
      const opts = group[0].options || [];
      const picked = opts.find((o) => o.id === (body.option_id || (opts.find((x) => x.recommended) || opts[0] || {}).id));
      const rec = { item: group[0], group, answer: body.action.startsWith('approve') ? 'approved' : body.action === 'reject' ? 'rejected' : body.action, reason: body.reason || '',
        at: ui.state?.clock || '', optionId: picked?.id, optionLabel: picked?.label, notSuggested: picked && !picked.recommended, change: body.change || null };
      for (const g of group) ui.decided.set(g.id, rec);
      ui.selected = group[0].id;
    }
    ui.mode = null; ui.traceFor = null;
    await refreshQueue();
  } catch (e) { toast(e.status === 409 ? e.message : `Could not send your answer: ${e.message}`, true); }
  finally { ui.busy = false; }
}

// ---------------------------------------------------------------- the decision's flowchart
async function loadTrace(it) {
  if (ui.traceFor && ui.traceFor.id === it.id && ui.traceFor.graph) { paintTrace(); return; }
  ui.traceFor = { id: it.id };
  try { const g = await ui.api.trace(ui.run, it.id); if (ui.selected !== it.id) return; ui.traceFor.graph = g; paintTrace(); }
  catch (e) { const el = $('trace'); if (el) el.innerHTML = `<p class="muted">Could not load: ${esc(e.message)}</p>`; }
}
async function refreshTrace() {
  const { it, rec } = current();
  if (!it || !rec || !ui.traceFor || ui.traceFor.id !== it.id) return;
  const g = await ui.api.trace(ui.run, it.id).catch(() => null);
  if (!g || ui.selected !== it.id) return;
  const key = JSON.stringify(g.nodes.map((n) => [n.id, n.detail && n.detail.status]));
  if (key === ui.traceFor.key) return;
  ui.traceFor.graph = g; ui.traceFor.key = key; paintTrace();
}
const AGENT_KEYS = ['situation', 'network_recovery', 'passenger_care', 'station_staff', 'communication'];
const INPUT_AGENT = { situation: 'situation', recovery_plan: 'network_recovery', care_plan: 'passenger_care', station_plan: 'station_staff', communication: 'communication',
  network_recovery: 'network_recovery', passenger_care: 'passenger_care', station_staff: 'station_staff' };

// Turn the trace into the three parts of the flowchart: how it came about, you, and where it went.
function flowModel(it, rec, g) {
  const nodes = g.nodes || [];
  const qNode = nodes.find((n) => n.kind === 'question');
  const human = nodes.find((n) => n.kind === 'human');
  const after = (n) => human && n.t && n.t >= human.t && n.kind === 'communication' && nodes.indexOf(n) > nodes.indexOf(human);
  const w = it.why || {};
  const feeds = nodes.filter((n) => n.kind === 'feed').map((n) => {
    const m = String(n.label).match(/^([a-z_]+):\s*(.*)$/i);
    const what = m ? m[2] : n.label, src = m ? m[1].replace(/_/g, ' ') : '';
    return { label: what.charAt(0).toUpperCase() + what.slice(1), t: `${src}${src && n.t ? ', ' : ''}${n.t || ''}`, text: n.detail?.text || `${src}: ${what}` };
  });
  if (!feeds.length) for (const x of w.trigger || []) feeds.push({ label: String(x.source).replace(/_/g, ' '), t: x.t, text: x.text });
  const agents = AGENT_KEYS.map((key) => {
    const mine = nodes.filter((n) => n.kind === key && !after(n));
    const used = (w.inputs || []).filter((x) => INPUT_AGENT[x.kind] === key);
    const involved = mine.length > 0 || used.length > 0 || it.agent === key;
    const lastN = mine[mine.length - 1], lastU = used[used.length - 1];
    const short = lastN?.t || lastU?.t || (it.agent === key ? 'asked' : '');
    return { key, name: agentName(key), involved, short: short.trim(),
      label: agentName(key), t: lastN?.t || lastU?.t || '',
      text: involved ? (lastN?.detail?.says || lastN?.detail?.text || (it.agent === key ? (it.recommendation || w.plan) : `Shared its latest ${kindName(lastU?.kind)} at ${lastU?.t}`)) : 'Had nothing to add to this decision.' };
  });
  const rcN = nodes.find((n) => n.kind === 'rule_check');
  const rule = { ok: w.rule_check ? w.rule_check.ok : !/block/i.test(rcN?.label || ''), rules: (w.rule_check?.rules || []).join(', '),
    label: 'Rule check', t: rcN?.t, text: w.rule_check?.text || rcN?.detail?.text || '' };
  const orN = nodes.find((n) => n.kind === 'orchestrator');
  const orch = { label: 'Orchestrator', t: orN?.t, text: orN?.detail?.text || w.why_human || '' };
  const answer = rec ? rec.answer : null;
  const finalApproved = answer === 'approved' || (human && /approved/i.test(human.label));
  const youState = finalApproved ? 'approved' : answer === 'rejected' ? 'rejected' : answer === 'hand_off' ? 'handed off' : it.status === 'escalated' ? 'escalated' : 'waiting';
  const you = { state: youState,
    label: youState === 'waiting' ? 'Your call' : youState === 'approved' ? (human && /duty manager/i.test(human.label) ? 'Duty manager approved' : 'You approved') :
      youState === 'rejected' ? 'You rejected' : youState === 'handed off' ? 'With the duty manager' : 'Escalated to duty manager',
    sub: youState === 'waiting' ? `decide by ${it.deadline}` : (human?.t || rec?.at || ''), t: human?.t || '', text: human?.detail?.text || (qNode?.detail?.text || '') };
  const preview = !finalApproved;
  const changeNodes = nodes.filter((n) => n.kind === 'change');
  const changes = preview ? (g.preview?.changes?.length ? g.preview.changes : []).map((c) => ({ label: c.label, t: '', text: c.label })) :
    changeNodes.map((n) => ({ label: n.label, t: n.t, text: n.detail?.text || n.label }));
  const comm = nodes.filter((n) => n.kind === 'communication' && after(n)).pop();
  const destNodes = nodes.filter((n) => n.kind === 'channel' || n.kind === 'staff');
  const iconOf = (k) => ({ app: 'app', sms: 'sms', gate_screen: 'gate_screen', call_center: 'call_center', call_center_agents: 'call_center', crew: 'crew' }[k] || 'staff');
  const dests = preview
    ? (g.preview?.destinations || [])
      .map((d) => ({ label: d.label, icon: d.icon || iconOf(d.key), status: 'preview', text: 'Will get the same update at the same time' }))
    : destNodes.map((n) => ({ label: String(n.label).replace(/:\s*(delivered|failed|waiting|sent)\b.*$/i, ''), icon: n.detail?.icon || iconOf(n.detail?.key || n.label), status: n.detail?.status || 'waiting',
        t: n.detail?.delivered > 1 ? `${n.detail.delivered} delivered${n.t ? ', ' + n.t : ''}` : n.t, text: [n.detail?.text, n.detail?.confirmed_by && `Confirmed by: ${n.detail.confirmed_by}`].filter(Boolean).join('. ') }));
  const hub = { label: 'One truth', sub: 'Communication', t: comm?.t,
    text: comm?.detail?.text ? `Sent to everyone: "${comm.detail.text}"` : 'The change is written once to the shared state and Communication sends that one version to every screen and every person at the same time.' };
  const rejected = answer === 'rejected' ? { agent: it.agent, label: `${agentName(it.agent)} is looking for a different plan`, sub: rec.reason ? `Your reason: ${rec.reason}` : '', text: it.consequence_if_rejected || '' } : null;
  return { feeds, agents, rule, orch, you, changes, hub, dests, preview, rejected };
}

function paintTrace() {
  const el = $('trace');
  const { it, rec } = current();
  if (!el || !it || !ui.traceFor?.graph) return;
  const m = flowModel(it, rec, ui.traceFor.graph);
  for (const a of m.agents) a.short = a.involved ? a.short : '';
  m.feeds = m.feeds.map((f) => { const l = plain(f.label); return { ...f, label: l.charAt(0).toUpperCase() + l.slice(1) }; });
  const sum = $('in-short');
  if (sum) sum.innerHTML = `<b>In short:</b> ${esc(inShort(it, rec, m))}`;
  drawDecisionFlow(el, m, { onNode: (n) => {
    $('trace-note').innerHTML = `<b>${esc(plain(n.label || n.name || ''))}</b>${n.t ? ` at ${esc(n.t)}` : ''}${n.text ? `: ${esc(plain(n.text))}` : ''}`;
  } });
}

// ---------------------------------------------------------------- Customers: lookup and rebooking (counter and gate)
$('cust-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = $('cust-q').value.trim();
  if (/^(wn)?\d{3,4}$/i.test(q)) { openFlight(q.toUpperCase().startsWith('WN') ? q.toUpperCase() : `WN${q}`); return; }
  if (!q) { $('cust-list').innerHTML = '<li class="muted">Type a name, booking reference or flight number first.</li>'; return; }
  const list = await ui.api.customers(ui.run, q).catch((err) => { toast(err.message, true); return []; });
  $('cust-list').innerHTML = list.length ? list.map((c) => `<li><button type="button" class="cust-row" data-c="${esc(c.id)}"><b>${esc(c.name)}</b>
    <span class="muted">${esc(c.booking_ref)}, ${esc(c.flight_id)}</span><span class="status ${/cancel|rebook/i.test(c.status) ? 'bad' : /delay|held/i.test(c.status) ? 'warn' : 'ok'}">${esc(plain(c.status))}</span></button></li>`).join('')
    : '<li class="muted">No Customer found.</li>';
  $('cust-list').querySelectorAll('[data-c]').forEach((b) => b.addEventListener('click', () => showCustomer(b.dataset.c)));
});
async function showCustomer(id) {
  ui.custSel = id;
  const c = await ui.api.customer(ui.run, id).catch((err) => { toast(err.message, true); return null; });
  if (!c) return;
  const card = $('cust-card');
  const leg = c.trip.find((f) => /cancel|delay|divert|held/i.test(f.status)) || c.trip[0];
  const flight = leg && ui.api.flight ? await ui.api.flight(ui.run, leg.flight_id).catch(() => null) : null;
  card.innerHTML = `
    <div class="cust-head"><h2>${esc(c.name)}</h2>${(c.needs || []).map((n) => `<span class="tagx warn">${esc(NEEDS[n] || plain(n))}</span>`).join(' ')}</div>
    <p class="cust-truth"><b>Right now:</b> ${esc(plain(c.true_status))}</p>
    <h3>Their trip</h3>
    <div class="legs">${c.trip.map((f) => `<div class="leg ${f === leg ? 'hit' : ''}">
      <div class="leg-top"><b>${esc(f.flight_id)}</b> <span>${esc(f.from)} → ${esc(f.to)}</span> <span class="status ${/cancel/i.test(f.status) ? 'bad' : /delay|divert|held/i.test(f.status) ? 'warn' : 'ok'}">${esc(plain(f.status))}</span>
        <button class="link" type="button" data-fl="${esc(f.flight_id)}">Everyone on this flight</button></div>
      <dl class="fl-facts small">
        <div><dt>Scheduled</dt><dd>${esc(f.sched_dep || f.dep)} → ${esc(f.sched_arr || f.arr)}</dd></div>
        <div><dt>Expected</dt><dd>${esc(f.est_dep || f.sched_dep || f.dep)}${f.est_arr ? ` → ${esc(f.est_arr)}` : ''}</dd></div>
        <div><dt>Gate</dt><dd>${esc(f.gate || 'not set')}</dd></div>
        ${f.connection ? `<div><dt>Connection</dt><dd>${connText(f.connection)}</dd></div>` : ''}
      </dl></div>`).join('')}</div>
    ${c.international ? '<p><span class="tagx warn">International trip</span></p>' : ''}
    ${oneStoryHtml(flight)}
    <div class="rebook-box" id="rebook-box">${c.rebooking?.length ? rebookList(c.rebooking, 'Groundwork suggests') : `<button class="btn primary" type="button" id="rebook-ask">Rebook this Customer</button>
      <span class="muted small">For a Customer who asks to change, or a flight running late.</span>`}</div>
    ${c.hotel ? `<p><b>Hotel:</b> ${esc(c.hotel.name)}${c.hotel.price ? `, $${c.hotel.price}` : ''}${c.hotel.shuttle === true ? ', shuttle included' : c.hotel.shuttle ? `, shuttle ${esc(c.hotel.shuttle)}` : ''}${c.hotel.status ? ` <span class="muted">(${esc(plain(c.hotel.status))})</span>` : ''}</p>` : ''}
    ${(c.refunds || []).length ? `<p><b>Refunds:</b> ${c.refunds.map((r) => `${r.amount != null ? `$${r.amount}` : 'Unused fare'} ${esc(r.status)}${r.t ? ` at ${esc(r.t)}` : ''}`).join('; ')}</p>` : ''}
    <div class="act-row">${c.refund_due ? '<button class="btn" id="refund" type="button">Refund instead</button>' : ''}</div>
    <h3>Messages they received</h3>
    ${(c.messages || []).length ? `<ol class="msgs">${c.messages.map((x) => `<li><span class="t">${esc(x.t)}</span> <span class="tagx quiet">${esc(cap(CHANNEL_WORDS[x.channel] || plain(x.channel || 'note')))}</span> "${esc(x.text)}"</li>`).join('')}</ol>` : '<p class="muted">No messages yet.</p>'}`;
  card.querySelectorAll('[data-fl]').forEach((b) => b.addEventListener('click', () => openFlight(b.dataset.fl)));
  card.querySelector('#rebook-ask')?.addEventListener('click', async () => {
    ui.rebookAsked = id;
    const opts = await ui.api.rebookOptions(ui.run, id).catch((err) => { toast(`Could not load options: ${err.message}`, true); return null; });
    if (!opts) return;
    $('rebook-box').innerHTML = opts.length ? rebookList(opts, 'Ways to get there, best first') : '<p class="muted">No other flight to this destination has seats today or tomorrow.</p>';
    wireRebook(card, id);
  });
  card.querySelector('#refund')?.addEventListener('click', async () => { await ui.api.refund(ui.run, id); toast('Refund issued.'); showCustomer(id); });
  wireRebook(card, id);
  if (ui.rebookAsked === id) card.querySelector('#rebook-ask')?.click();     // keep the options open while the page refreshes
}
function connText(k) {      // a negative connection time means the first flight now lands after the second one leaves
  const m = Number(k.minutes);
  const words = m < 0 ? `misses it by ${-m} min` : `${m} min to connect`;
  return `${esc(k.to_flight)}, ${words}${k.at_risk ? ` <span class="tagx bad">${m < 0 ? 'Missed' : 'At risk'}</span>` : ''}`;
}
function rebookList(opts, title) {
  const sorted = [...opts].sort((a, b) => (a.rank ?? (a.recommended ? 0 : 9)) - (b.rank ?? (b.recommended ? 0 : 9)));
  return `<h3>${title}</h3><ul class="rebook">${sorted.map((o, i) => `<li class="${i === 0 ? 'rec' : ''}">
    <div><span class="tagx ${i === 0 ? 'ok' : 'quiet'}">${esc(o.label && !/^[A-Z]{2}\d/.test(o.label) ? o.label : ['Best option', 'Next best', 'Third choice'][i] || `Choice ${i + 1}`)}</span>
    <b>${o.flights.map((f) => `${esc(f.flight_id)} ${esc(f.dep)}`).join(' + ')}</b> → arrives ${esc(o.arrives)}
    <br><span class="muted">${esc(plain(o.why))}${o.seats_left != null && !/seat/i.test(o.why || '') ? `, ${o.seats_left} seats left` : ''}</span></div>
    <button class="btn ${i === 0 ? 'primary' : ''}" type="button" data-rebook="${esc(o.option_id)}">Rebook on this</button></li>`).join('')}</ul>`;
}
function wireRebook(card, id) {
  card.querySelectorAll('[data-rebook]').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try { await ui.api.rebook(ui.run, id, { option_id: b.dataset.rebook }); ui.rebookAsked = null; toast('Rebooked. The Customer gets a text, and every screen shows the new trip.'); showCustomer(id); }
    catch (err) { b.disabled = false; toast(`Could not rebook: ${err.message}`, true); }
  }));
}



// ---------------------------------------------------------------- who is out of date, and everyone on a flight
const CH = { app: 'app', sms: 'text message', gate_screen: 'gate screen', call_center: 'call center' };
async function refreshOod() {
  if (!ui.api.outOfDate) return;
  const o = await ui.api.outOfDate(ui.run).catch(() => null);
  const box = $('ood');
  if (!o) { box.hidden = true; return; }
  box.hidden = false;
  const diff = (o.ten_min_ago ?? o.total) - o.total;
  box.innerHTML = `<div class="card-head"><h2>Out of date right now: ${num(o.total)} Customers</h2>
    <span class="tagx ${diff > 0 ? 'ok' : diff < 0 ? 'bad' : 'quiet'}">${diff > 0 ? `${num(diff)} fewer than 10 min ago` : diff < 0 ? `${num(-diff)} more than 10 min ago` : 'Same as 10 min ago'}</span></div>
    ${o.flights.length ? `<ul class="ood-list">${o.flights.map((f) => `<li><div><b>${esc(f.label || f.flight_id)}</b> <span class="muted">${num(f.customers)} Customers</span><br>
      <span class="small">${f.channels.map((c) => `${esc(CH[c.channel] || plain(c.channel))} out of date since ${esc(c.since)}`).join(', ')}${f.truth ? `. Truth: ${esc(plain(f.truth))}` : ''}</span></div>
      <button class="btn" type="button" data-ood="${esc(f.flight_id)}">Open flight</button></li>`).join('')}</ul>` : '<p class="muted">Every Customer sees the truth right now.</p>'}`;
  box.querySelectorAll('[data-ood]').forEach((b) => b.addEventListener('click', () => openFlight(b.dataset.ood)));
}

const NEED_WORD = { wheelchair: 'Wheelchair', unaccompanied_minor: 'Unaccompanied minor' };
ui.fd = { filter: 'all', data: null, flight: null, picks: {} };
$('fd-close').addEventListener('click', () => $('flight-dlg').close());
async function openFlight(fid) {
  ui.fd = { filter: 'all', data: null, flight: fid, picks: {} };
  $('fd-title').textContent = `Customers on ${fid}`;
  $('fd-body').innerHTML = '<p class="muted">Loading…</p>';
  if (!$('flight-dlg').open) $('flight-dlg').showModal();
  const d = await ui.api.flightCustomers(ui.run, fid).catch((err) => { $('fd-body').innerHTML = `<p class="muted">Could not load this flight: ${esc(err.message)}</p>`; return null; });
  if (!d) return;
  ui.fd.data = d; renderFlight();
}
function renderFlight() {
  const d = ui.fd.data, f = d.flight || {};
  const all = d.customers || [];
  const counts = { all: all.length, connecting: all.filter((c) => c.connection).length, needs: all.filter((c) => (c.needs || []).length).length,
    international: all.filter((c) => c.international).length, todo: all.filter((c) => !c.rebooked && c.best_option).length };
  const show = all.filter((c) => ui.fd.filter === 'all' || (ui.fd.filter === 'connecting' && c.connection) || (ui.fd.filter === 'needs' && (c.needs || []).length)
    || (ui.fd.filter === 'international' && c.international) || (ui.fd.filter === 'todo' && !c.rebooked && c.best_option))
    .sort((a, b) => ((b.needs || []).length - (a.needs || []).length) || ((b.connection ? 1 : 0) - (a.connection ? 1 : 0)));
  const chip = (k, label) => `<button type="button" class="btn chip ${ui.fd.filter === k ? 'active' : ''}" data-ff="${k}">${label} ${counts[k]}</button>`;
  $('fd-body').innerHTML = `
    <p class="fd-status"><b>${esc(f.id || ui.fd.flight)}</b> ${esc(f.from || '')}${f.to ? ` → ${esc(f.to)}` : ''} <span class="status ${/cancel/i.test(f.status || '') ? 'bad' : /delay|held|divert/i.test(f.status || '') ? 'warn' : 'ok'}">${esc(plain(f.status || ''))}</span>
      ${f.truth ? `<span class="muted">${esc(plain(f.truth))}</span>` : ''}</p>
    <div class="fd-filters">${chip('all', 'All')}${chip('todo', 'Not rebooked yet')}${chip('needs', 'Special needs')}${chip('connecting', 'Connecting')}${counts.international ? chip('international', 'International') : ''}</div>
    <div class="board-wrap"><table class="board fd-table"><thead><tr><th>Customer</th><th>Needs</th><th>Connection</th><th>Best option</th><th></th></tr></thead><tbody>
    ${show.map((c) => {
      const pick = ui.fd.picks[c.id];
      const opt = pick?.option || c.best_option;
      return `<tr><td><button class="link" type="button" data-oc="${esc(c.id)}">${esc(c.name)}</button><br><span class="muted small">${esc(plain(c.status || ''))}</span></td>
        <td>${(c.needs || []).map((n) => `<span class="tagx warn">${esc(NEED_WORD[n] || plain(n))}</span>`).join(' ')}${c.international ? ' <span class="tagx quiet">International</span>' : ''}</td>
        <td>${c.connection ? connText(c.connection) : '<span class="muted">-</span>'}</td>
        <td>${c.rebooked ? '<span class="status ok">Rebooked</span>' : opt ? `<b>${opt.flights.map((x) => `${esc(x.flight_id)} ${esc(x.dep)}`).join(' + ')}</b> → ${esc(opt.arrives)}<br><span class="muted small">${esc(plain(opt.why || ''))}</span>
          <div class="fd-alt" id="alt-${esc(c.id)}"></div>` : '<span class="muted">No rebooking needed</span>'}</td>
        <td class="fd-act">${!c.rebooked && opt ? `<button class="btn primary" type="button" data-one="${esc(c.id)}">Rebook</button> <button class="btn" type="button" data-change="${esc(c.id)}">Change</button>` : ''}</td></tr>`;
    }).join('') || '<tr><td colspan="5" class="muted">No Customers in this view.</td></tr>'}
    </tbody></table></div>
    ${counts.todo ? `<div class="act-row fd-all"><button class="btn primary" type="button" id="fd-all">Rebook all ${counts.todo} on their best option</button><span class="muted small">Special needs and connections are listed first. Each Customer gets a text with their new trip.</span></div>` : ''}`;
  $('fd-body').querySelectorAll('[data-ff]').forEach((b) => b.addEventListener('click', () => { ui.fd.filter = b.dataset.ff; renderFlight(); }));
  $('fd-body').querySelectorAll('[data-oc]').forEach((b) => b.addEventListener('click', () => { $('flight-dlg').close(); setTab('customers'); showCustomer(b.dataset.oc); }));
  $('fd-body').querySelectorAll('[data-change]').forEach((b) => b.addEventListener('click', async () => {
    const cid = b.dataset.change;
    const opts = await ui.api.rebookOptions(ui.run, cid).catch((err) => { toast(err.message, true); return []; });
    $(`alt-${cid}`).innerHTML = opts.length ? `<label class="field"><span class="field-label">Choose another</span><select data-pick="${esc(cid)}">${opts.map((o, i) => `<option value="${esc(o.option_id)}">${esc(o.label || ['Best option', 'Next best', 'Third choice'][i] || '')}: ${esc(o.flights.map((x) => `${x.flight_id} ${x.dep}`).join(' + '))}, arrives ${esc(o.arrives)}</option>`).join('')}</select></label>` : '<span class="muted small">No other flight with seats.</span>';
    $(`alt-${cid}`).querySelector('select')?.addEventListener('change', (e) => { ui.fd.picks[cid] = { option: opts.find((o) => o.option_id === e.target.value) }; });
  }));
  const go = async (assignments) => {
    try {
      const r = await ui.api.flightRebook(ui.run, ui.fd.flight, assignments);
      const bad = (r.results || []).filter((x) => x.error);
      toast(bad.length ? `${assignments.length - bad.length} rebooked; ${bad.length} could not be: ${bad[0].error}` : `${assignments.length} rebooked. Each Customer gets a text with their new trip.`, !!bad.length);
      openFlight(ui.fd.flight);
    } catch (err) { toast(`Could not rebook: ${err.message}`, true); }
  };
  $('fd-body').querySelectorAll('[data-one]').forEach((b) => b.addEventListener('click', () => {
    const c = all.find((x) => x.id === b.dataset.one); const opt = ui.fd.picks[c.id]?.option || c.best_option;
    b.disabled = true; go([{ customer_id: c.id, option_id: opt.option_id }]);
  }));
  $('fd-all')?.addEventListener('click', (e) => {
    e.target.disabled = true;
    go(all.filter((c) => !c.rebooked && c.best_option).sort((a, b) => ((b.needs || []).length - (a.needs || []).length) || ((b.connection ? 1 : 0) - (a.connection ? 1 : 0)))
      .map((c) => ({ customer_id: c.id, option_id: (ui.fd.picks[c.id]?.option || c.best_option).option_id })));
  });
}

// ---------------------------------------------------------------- clocks
async function refreshClocks() {
  const c = await ui.api.clocks(ui.run).catch(() => null);
  if (!c) { $('clocks').innerHTML = '<div class="card"><p class="muted">Clocks are not available yet.</p></div>'; return; }
  const card = (title, rows, empty) => `<section class="card clock-card"><h2>${title}</h2>${rows.length ? `<ul class="clock-list">${rows.join('')}</ul>` : `<p class="muted">${empty}</p>`}</section>`;
  const bar = (used, limit) => `<span class="cbar"><i style="width:${Math.min(100, Math.round(100 * used / limit))}%" class="${used / limit > 0.8 ? 'bad' : used / limit > 0.6 ? 'warn' : ''}"></i></span>`;
  $('clocks').innerHTML = [
    card('Customers waiting on the tarmac', (c.tarmac || []).map((x) => `<li><b>${esc(x.flight_id)}</b> ${x.minutes} of ${x.limit} min ${bar(x.minutes, x.limit)}</li>`), 'No aircraft holding Customers on the tarmac.'),
    card('Crew legal time left', (c.crew || []).map((x) => `<li class="${x.minutes_left <= 30 ? 'bad' : x.minutes_left <= 90 ? 'warn' : ''}"><b>${esc(x.crew_id)}</b> on ${esc(x.flight_id)}: legal until ${esc(x.legal_until)} <span class="muted">(${x.minutes_left} min)</span></li>`), 'No crew close to their limit.'),
    card('Customers needing assistance', (c.special_assistance || []).map((x) => `<li class="${x.waiting_min > 45 ? 'warn' : ''}"><b>${esc(x.customer_id)}</b> ${esc(NEEDS[x.need] || plain(x.need))} on ${esc(x.flight_id)}, waiting ${x.waiting_min} min</li>`), 'Nobody waiting.'),
    card('Refunds', (c.refunds || []).map((x) => `<li><b>${esc(x.customer_id)}</b> ${x.status === 'auto_issued' ? 'issued automatically' : `due by ${esc(x.due_by)}`}</li>`), 'No refunds due yet.'),
  ].join('');
}

// ---------------------------------------------------------------- day summary
async function refreshSummary() {
  const since = ui.handoverSince || '11:00';
  const h = await ui.api.handover(ui.run, since).catch(() => null);
  $('handover').innerHTML = h ? `<div class="card-head"><h2>Handover since ${esc(h.since)}</h2><button class="btn" id="handover-now" type="button">Start a new shift now</button></div>
    <p class="handover-text">${esc(plain(h.text))}</p>
    ${h.watch?.length ? `<h3>Keep an eye on</h3><ul class="plain">${h.watch.map((x) => `<li>${esc(plain(x))}</li>`).join('')}</ul>` : ''}
    ${h.open?.length ? `<h3>Still open</h3><ul class="plain">${h.open.map((x) => `<li>${esc(plain(x.title))}</li>`).join('')}</ul>` : ''}` : '<p class="muted">No handover yet.</p>';
  $('handover-now')?.addEventListener('click', () => { ui.handoverSince = ui.state?.clock; refreshSummary(); });
  const r = await ui.api.results(ui.run).catch(() => null);
  if (!r) $('results').innerHTML = `<div class="card"><h2>How the day went</h2><p class="muted">The comparison with today's process is ready when the day ends at 23:59.</p></div>`;
  if (r) {
    const cols = [['Your day', r.you, 'you'], ["Today's process", r.today, 'today'], ['Groundwork, approving everything', r.orchestrated, 'auto']].filter((c) => c[1]);
    const row = (label, key, fmt = num) => `<tr><th>${label}</th>${cols.map(([, v, cls]) => `<td class="${cls}">${v[key] != null ? fmt(v[key]) : '-'}</td>`).join('')}</tr>`;
    const d = r.decisions || {};
    $('results').innerHTML = `<div class="card results-hero"><h2>${ui.state?.status === 'finished' ? 'How the day went' : 'How the day is going'}</h2>
      <div class="board-wrap"><table class="board results-table"><thead><tr><th></th>${cols.map(([n]) => `<th>${esc(n)}</th>`).join('')}</tr></thead>
      <tbody>${row('Customer-minutes on a wrong screen', 'wrong_screen_passenger_minutes')}${row('Flights cancelled', 'cancelled')}${row('Customers stranded overnight', 'stranded_overnight')}${row(`Where ${esc(ui.state?.focus?.name || 'the Customer we follow')} ended up`, 'focus_final_trip', (x) => esc(plain(x)))}</tbody></table></div>
      <div class="stats five">${[['Approved', d.approved], ['Declined', d.rejected], ['Reminders', d.waited], ['Sent to duty manager', d.handed_off], ['Escalated', d.escalated], ['Median time to decide', `${d.median_answer_min ?? 0} min`]]
        .map(([l, v]) => `<div class="stat"><span class="stat-label">${l}</span><span class="stat-value">${v ?? 0}</span></div>`).join('')}</div></div>`;
  }
  const hist = await ui.api.history(ui.run).catch(() => ({ decisions: [] }));
  const WORD = { approved: 'Approved', rejected: 'Declined', waited: 'Reminder', 'handed off': 'Sent to duty manager', auto: 'Done automatically' };
  $('history').innerHTML = `<thead><tr><th>Time</th><th>Decision</th><th>Who</th><th>Answer</th><th>Took</th></tr></thead><tbody>` +
    (hist.decisions.length ? hist.decisions.map((d) => `<tr ${ui.decided.has(d.id) ? `data-open="${d.id}" tabindex="0" class="clickable"` : ''}><td>${esc(d.answered)}</td><td>${esc(plain(d.title))}</td>
      <td>${esc(ALL_ROLES[d.role] || plain(d.role))}${d.by === 'director_of_operations' ? ' (duty manager)' : ''}</td>
      <td><span class="status ${d.answer === 'approved' || d.answer === 'auto' ? 'ok' : d.answer === 'rejected' ? 'bad' : 'warn'}">${esc(WORD[d.answer] || d.answer)}</span>${d.reason ? `<br><span class="muted">${esc(plain(d.reason))}</span>` : ''}</td>
      <td>${esc(d.waited_min)} min</td></tr>`).join('') : '<tr><td colspan="5" class="muted">No decisions yet.</td></tr>') + '</tbody>';
  $('history').querySelectorAll('[data-open]').forEach((r) => r.addEventListener('click', () => { ui.selected = Number(r.dataset.open); ui.traceFor = null; setTab('decisions'); renderDetail(); }));
}
// ---------------------------------------------------------------- agent flow (whole system)
const NODES = {
  feeds: { x: 10, y: 175, w: 170, label: 'Outside feeds', sub: 'FAA, weather, flights' },
  situation: { x: 225, y: 175, w: 175, label: 'Situation', agent: 'situation' },
  shared: { x: 445, y: 175, w: 170, label: 'Shared state', sub: 'every version, who used it' },
  network_recovery: { x: 665, y: 30, w: 200, label: 'Network Recovery', agent: 'network_recovery' },
  passenger_care: { x: 665, y: 125, w: 200, label: 'Passenger Care', agent: 'passenger_care' },
  station_staff: { x: 665, y: 220, w: 200, label: 'Station & Staff', agent: 'station_staff' },
  communication: { x: 665, y: 315, w: 200, label: 'Communication', agent: 'communication' },
  orchestrator: { x: 910, y: 120, w: 200, label: 'Orchestrator', sub: 'rule check, queue' },
  you: { x: 910, y: 260, w: 200, label: 'You', sub: '' },
};
const LINKS = [['feeds', 'situation'], ['situation', 'shared'], ['shared', 'network_recovery'], ['shared', 'passenger_care'], ['shared', 'station_staff'], ['shared', 'communication'],
  ['network_recovery', 'orchestrator'], ['passenger_care', 'orchestrator'], ['station_staff', 'orchestrator'], ['communication', 'orchestrator'], ['orchestrator', 'you']];

function drawFlow(flow) {
  const now = ui.state ? ui.state.minute : 0;
  const toMin = (s) => { if (!s) return -999; const [h, m] = s.split(':').map(Number); return h * 60 + m; };
  const byKey = Object.fromEntries((flow.agents || []).map((a) => [a.key, a]));
  const H = 64;
  const center = (n, side) => side === 'r' ? [n.x + n.w, n.y + H / 2] : [n.x, n.y + H / 2];
  let svg = `<defs><marker id="arr2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" class="g-arrow"/></marker></defs>`;
  for (const [a, b] of LINKS) {
    const A = NODES[a], B = NODES[b];
    const [x1, y1] = center(A, 'r'), [x2, y2] = center(B, 'l');
    const hot = (byKey[b] && now - toMin(byKey[b].last_run) <= 2) || (b === 'you' && ui.state && ui.state.pending);
    svg += `<path class="f-edge ${hot ? 'hot' : ''}" d="M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2 - 3},${y2}" marker-end="url(#arr2)"/>`;
  }
  for (const [k, n] of Object.entries(NODES)) {
    const a = byKey[n.agent];
    let sub = n.sub || '';
    let hot = false, badge = 0;
    if (a) {
      const v = a.versions && a.versions[a.versions.length - 1];
      sub = a.last_run ? `checked ${a.last_run}` : 'not run yet';
      hot = now - toMin(a.last_run) <= 2; badge = a.waiting_on_human;
    }
    if (k === 'orchestrator') { sub = `${flow.orchestrator?.open_incidents || 0} incident open, ${flow.orchestrator?.queue || 0} queued`; hot = (flow.orchestrator?.queue || 0) > 0; }
    if (k === 'you') { sub = `${ui.state?.pending || 0} waiting for you`; hot = (ui.state?.pending || 0) > 0; badge = 0; }
    const ic = n.agent || (k === 'orchestrator' ? 'orchestrator' : null);
    svg += `<g class="f-node ${hot ? 'hot' : ''} ${k === 'you' ? 'you' : ''} ${k === 'shared' ? 'shared' : ''}" transform="translate(${n.x},${n.y})">
      <rect width="${n.w}" height="${H}" rx="12"/>
      ${ic ? `<svg x="12" y="${H / 2 - 11}" width="22" height="22" viewBox="0 0 24 24" class="f-ico">${icon(ic).replace(/^<svg[^>]*>|<\/svg>$/g, '')}</svg>` : ''}
      <text x="${ic ? 42 : 14}" y="27" class="f-label">${esc(n.label)}</text>
      <text x="${ic ? 42 : 14}" y="46" class="f-sub">${esc(sub)}</text>
      ${badge ? `<g transform="translate(${n.w - 10},-6)"><circle r="11" class="f-badge"/><text class="f-badge-t" text-anchor="middle" y="4">${badge}</text></g>` : ''}
    </g>`;
  }
  $('sysflow').innerHTML = svg;
  const who = (ref) => { const k = String(ref).split(':')[0]; return k === 'feed' ? 'the outside feeds' : agentName(k); };
  const edges = [...(flow.edges || [])].reverse().slice(0, 12);
  $('ticker').innerHTML = edges.length ? edges.map((e) => `<li><span class="t">${esc(e.t)}</span> ${esc(who(e.to))} updated its picture, using what ${esc(who(e.from))} shared</li>`).join('')
    : '<li class="muted">Nothing handed over yet.</li>';
}



// ---------------------------------------------------------------- messages going out (bottom-right)
ui.sentSince = 0; ui.popups = true;
try { ui.popups = localStorage.getItem('gsc-popups') !== 'off'; } catch (e) {}
const CHANNEL_WORDS = { sms: 'text message', text: 'text message', app: 'app', email: 'email', staff_note: 'staff note', crew: 'crew message', 'app + text': 'app and text' };
const cap = (x) => x.charAt(0).toUpperCase() + x.slice(1);
async function refreshSent() {
  if (!ui.api.sent || ui.sentOff) return;
  let rows;
  try { rows = await ui.api.sent(ui.run, ui.sentSince); } catch (e) { if (e.status === 404) ui.sentOff = true; return; }
  const first = !ui.sentPrimed; ui.sentPrimed = true;
  if (!rows || !rows.length) return;
  ui.sentSince = Math.max(ui.sentSince, ...rows.map((r) => r.minute || 0));
  const ICON = { refund: '✓', hotel: '🏨', rebook: '✈', message: '✉' };
  ui.sentSeen ||= new Set();
  const fresh = rows.filter((r) => { const k = `${r.minute}|${r.to}|${r.kind}|${r.text}`; if (ui.sentSeen.has(k)) return false; ui.sentSeen.add(k); return true; });
  if (first || !ui.popups) return;
  for (const r of fresh.slice(-4)) {
    const el = document.createElement('div');
    el.className = `sent-pop ${r.kind || 'message'}`;
    el.innerHTML = `<span class="sp-head">${ICON[r.kind] || '✉'} ${r.channel ? `${esc(cap(CHANNEL_WORDS[r.channel] || plain(r.channel)))} to ${esc(r.to)}` : `${esc({ refund: 'Refund', hotel: 'Hotel', rebook: 'Rebooking' }[r.kind] || 'Update')} for ${esc(r.to)}`}, ${esc(r.t)}</span><span class="sp-text">"${esc(String(r.text).replace(/\s*\([^)]*legal-floor[^)]*\)/i, ''))}"</span>`;
    if (r.customer_id) { el.classList.add('link'); el.tabIndex = 0; el.title = 'Open this Customer'; el.addEventListener('click', () => { setTab('customers'); showCustomer(r.customer_id); }); }
    $('sent-stack').appendChild(el);
    setTimeout(() => el.classList.add('out'), 7000);
    setTimeout(() => el.remove(), 7600);
  }
  while ($('sent-stack').children.length > 4) $('sent-stack').firstChild.remove();
}
function popupLabel() { $('popups-btn').textContent = ui.popups ? 'Message pop-ups: on' : 'Message pop-ups: off'; $('popups-btn').setAttribute('aria-pressed', ui.popups); }
$('popups-btn').addEventListener('click', () => { ui.popups = !ui.popups; try { localStorage.setItem('gsc-popups', ui.popups ? 'on' : 'off'); } catch (e) {} popupLabel(); });
popupLabel();

// ---------------------------------------------------------------- polling
async function refreshQueue() {
  const q = await ui.api.queue(ui.run, []).catch(() => null);
  if (!q) return;
  const before = new Set(ui.items.map((x) => x.id));
  ui.items = q.items;
  const fresh = visible().filter((x) => !before.has(x.id));
  if (fresh.length && ui.started) { const urgent = fresh.find((x) => x.urgent); toast(urgent ? `Urgent: ${headline(urgent)}` : `New: ${headline(fresh[0])}`, !!urgent); }
  ui.started = true;
  renderQueue();
}
async function refreshFlow() {
  const f = await ui.api.flow(ui.run, Math.max(0, (ui.state?.minute || 0) - 90)).catch(() => null);
  if (f) drawFlow(f);
}
let ticks = 0;
async function loop() {
  try {
    ui.state = await ui.api.state(ui.run);
    renderState();
    await refreshQueue();
    if (ui.tab === 'ops') await ui.ops.update(ui.state.minute);
    if (ticks % 2 === 0) await refreshSent();
    if (ui.tab === 'decisions' && ticks % 2 === 0) await refreshTrace();
    if (ui.tab === 'flow' && ticks % 2 === 0) await refreshFlow();
    if (ui.tab === 'clocks' && ticks % 3 === 0) await refreshClocks();
    if (ui.tab === 'summary' && ticks % 5 === 0) await refreshSummary();
    if (ui.tab === 'customers' && ticks % 5 === 0) { await refreshOod(); if (ui.custSel && !document.activeElement?.closest?.('#cust-card select')) await showCustomer(ui.custSel); }
  } catch (e) { $('status').textContent = `Lost the connection: ${e.message}`; }
  ticks++;
  setTimeout(loop, 1000);
}

// ---------------------------------------------------------------- boot
(async () => {
  const startBtn = document.querySelector('#start-form button[type="submit"]');
  startBtn.disabled = true; startBtn.textContent = 'Connecting to the database…';
  for (let attempt = 1; ; attempt++) {
    try { ui.api = await pickApi(); break; }
    catch (e) {
      startBtn.textContent = `Waiting for the database (try ${attempt})…`;
      if (attempt >= 6) { startBtn.textContent = 'Could not reach the database'; toast(`The server could not reach the database: ${e.message}. Check the terminal, then reload.`, true); return; }
      await new Promise((r) => setTimeout(r, 2500));
    }
  }
  startBtn.disabled = false; startBtn.textContent = 'Start the day';
  $('practice').hidden = !ui.api.practice;
  const params = new URLSearchParams(location.search);
  if (params.get('run') && !ui.api.practice) open(params.get('run'));
  else showPast();
  const auto = params.get('autostart');                 // demos and screenshots: practice only
  if (auto && ui.api.practice) {
    const r = await ui.api.start({ seed: 7, scenario: 'a31_storm', roles: Object.keys(ROLE_TITLES) });
    if (auto !== '1') { ui.api.s.minute = Number(auto); for (let m = 661; m <= Number(auto); m++) ui.api._minute(m); }
    open(r.run_id);
    if (params.get('tab')) setTab(params.get('tab'));
    if (params.get('role')) { $('role').value = params.get('role'); $('role').dispatchEvent(new Event('change')); }
  }
})();
