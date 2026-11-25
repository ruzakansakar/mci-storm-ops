// Live control room: the user is the human in the loop for every decision the agents cannot make alone.
import { pickApi, ROLE_TITLES } from './live-api.js';
import { icon } from './icons.js';
import { drawTrace } from './graph.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (n) => Number(n || 0).toLocaleString();
const CHANNELS = { app: 'App', sms: 'SMS', gate_screen: 'Gate screen', call_center: 'Call center' };
const ACTION_WORD = { approved: 'Approved', rejected: 'Rejected', waited: 'Waiting', 'handed off': 'Handed off' };

const ui = { api: null, run: null, state: null, items: [], selected: null, roles: Object.keys(ROLE_TITLES), view: new Set(Object.keys(ROLE_TITLES)),
  tab: 'decisions', series: [], traceFor: null, rejecting: false, busy: false };

// ---------------------------------------------------------------- theme
function theme() { return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
function themeLabel() { $('theme').textContent = theme() === 'dark' ? 'Light mode' : 'Dark mode'; }
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

// ---------------------------------------------------------------- start screen
$('role-picks').innerHTML = Object.entries(ROLE_TITLES).map(([k, v]) =>
  `<label class="role-pick"><input type="checkbox" name="role" value="${k}" checked><span>${esc(v)}</span></label>`).join('');

async function showPast() {
  const list = await ui.api.sessions().catch(() => []);
  $('past').innerHTML = list.length ? list.map((s) => `<li><button class="link" data-run="${esc(s.run_id)}" type="button">${esc(s.run_id)}</button>
    <span class="muted">${esc(s.status)}${s.started_at ? ', started ' + new Date(s.started_at).toLocaleString() : ''}</span></li>`).join('') : '<li class="muted">None yet.</li>';
  $('past').querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => open(b.dataset.run, [])));
}

$('start-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const roles = fd.getAll('role');
  const random = fd.get('day') === 'random';
  const body = { seed: random ? Math.floor(Math.random() * 9000) + 100 : 7, scenario: random ? 'random' : 'a31_storm', roles: roles.length ? roles : Object.keys(ROLE_TITLES) };
  try {
    const r = await ui.api.start(body);
    open(r.run_id, body.roles);
  } catch (err) { toast(`Could not start: ${err.message}`, true); }
});

function open(runId, roles) {
  ui.run = runId;
  if (roles.length) ui.view = new Set(roles);
  $('start').hidden = true; $('live').hidden = false;
  history.replaceState(null, '', `?run=${encodeURIComponent(runId)}${ui.api.practice ? '&mock=1' : ''}`);
  renderRoleFilter();
  loop();
}

// ---------------------------------------------------------------- session controls
$('pause').addEventListener('click', async () => {
  const action = ui.state && ui.state.status === 'running' ? 'pause' : 'resume';
  ui.state = await ui.api.control(ui.run, { action });
  renderState();
});
for (const id of ['speed-idle', 'speed-pending']) {
  $(id).addEventListener('change', async () => {
    ui.state = await ui.api.control(ui.run, { action: 'speed', speed_idle: Number($('speed-idle').value), speed_pending: Number($('speed-pending').value) });
  });
}
function renderRoleFilter() {
  $('role-filter').innerHTML = Object.entries(ROLE_TITLES).map(([k, v]) =>
    `<button type="button" class="btn chip ${ui.view.has(k) ? 'active' : ''}" data-role="${k}" aria-pressed="${ui.view.has(k)}">${esc(v)}</button>`).join('');
  $('role-filter').querySelectorAll('[data-role]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.role;
    ui.view.has(k) ? ui.view.delete(k) : ui.view.add(k);
    if (!ui.view.size) ui.view.add(k);
    renderRoleFilter(); refreshQueue();
  }));
}
for (const t of document.querySelectorAll('[data-tab]')) {
  t.addEventListener('click', () => {
    ui.tab = t.dataset.tab;
    document.querySelectorAll('[data-tab]').forEach((x) => { x.classList.toggle('active', x === t); x.setAttribute('aria-selected', x === t); });
    for (const p of ['decisions', 'flow', 'history', 'results']) $(`tab-${p}`).hidden = p !== ui.tab;
    if (ui.tab === 'history') refreshHistory();
    if (ui.tab === 'results') refreshResults();
    if (ui.tab === 'flow') refreshFlow();
  });
}

// ---------------------------------------------------------------- state and the "same story" panel
function renderState() {
  const s = ui.state;
  if (!s) return;
  $('clock').textContent = s.clock;
  $('status').textContent = s.status === 'running' ? (s.pending ? `Running slowly: ${s.pending} waiting for you` : 'Running') :
    s.status === 'paused' ? 'Paused' : s.status === 'finished' ? 'Day finished' : s.status;
  $('status').className = `status-line ${s.status}`;
  $('pause').textContent = s.status === 'running' ? 'Pause' : 'Resume';
  $('pause').disabled = !['running', 'paused'].includes(s.status);
  $('speed-idle').value = String(s.speed_idle);
  const sp = [...$('speed-pending').options].map((o) => Number(o.value)).reduce((a, b) => Math.abs(b - s.speed_pending) < Math.abs(a - s.speed_pending) ? b : a);
  $('speed-pending').value = String(sp);
  const w = s.world || {};
  $('world').innerHTML = [
    `<span class="pill"><span class="dot ${w.ground_stop ? 'bad' : 'ok'}"></span>${w.ground_stop ? `Ground stop, likely end ${esc(w.stop_expected_end || '?')}` : 'Operations normal'}</span>`,
    `<span class="pill"><span class="dot ${w.ramp_open ? 'ok' : 'bad'}"></span>Ramp ${w.ramp_open ? 'open' : 'closed'}</span>`,
    ...(s.incidents || []).filter((i) => i.status === 'open').map((i) => `<span class="pill"><span class="dot warn"></span>Incident ${esc(i.id)}: ${esc(i.title)}</span>`),
  ].join('');
  $('count').textContent = s.pending;
  $('count').classList.toggle('hot', s.urgent_pending > 0);

  const t = s.truth || {};
  $('wrong-now').textContent = num(t.wrong_now);
  const diffNow = (t.today_wrong_now || 0) - (t.wrong_now || 0);
  $('wrong-now-sub').innerHTML = `Today's process: <b>${num(t.today_wrong_now)}</b>${diffNow > 0 ? `, <span class="good">${num(diffNow)} fewer</span>` : diffNow < 0 ? `, <span class="badtxt">${num(-diffNow)} more</span>` : ''}`;
  $('wrong-total').textContent = num(t.wrong_minutes_total);
  const pct = t.today_wrong_minutes_total ? Math.round(100 * (1 - t.wrong_minutes_total / t.today_wrong_minutes_total)) : 0;
  $('wrong-total-sub').innerHTML = `Today's process: <b>${num(t.today_wrong_minutes_total)}</b>${pct > 0 ? `, <span class="good">${pct}% less</span>` : pct < 0 ? `, <span class="badtxt">${-pct}% more</span>` : ''}`;
  const last = ui.series[ui.series.length - 1];
  if (!last || last.m !== s.minute) ui.series.push({ m: s.minute, you: t.wrong_now || 0, today: t.today_wrong_now || 0 });
  if (ui.series.length > 600) ui.series.shift();
  drawSpark();

  const f = s.focus;
  if (f) {
    $('focus-name').textContent = f.name || f.passenger_id;
    $('focus-truth').textContent = `True status: ${f.true_status}`;
    $('screens').innerHTML = f.screens.map((sc) => `<div class="screen ${sc.wrong ? 'wrong' : ''}">
      <span class="screen-ch">${esc(CHANNELS[sc.channel] || sc.channel)}</span>
      <span class="screen-shows">${esc(sc.shows)}</span>
      <span class="screen-state">${sc.wrong ? `Wrong since ${esc(sc.since)}` : `Right since ${esc(sc.since)}`}</span></div>`).join('');
  }
}

function drawSpark() {
  const pts = ui.series;
  if (pts.length < 2) { $('spark').innerHTML = ''; return; }
  const m0 = pts[0].m, m1 = Math.max(pts[pts.length - 1].m, m0 + 60);
  const max = Math.max(10, ...pts.map((p) => Math.max(p.you, p.today))) * 1.1;
  const xy = (p, k) => `${((p.m - m0) / (m1 - m0) * 300).toFixed(1)},${(70 - p[k] / max * 66).toFixed(1)}`;
  $('spark').innerHTML = `<polyline class="sp-today" points="${pts.map((p) => xy(p, 'today')).join(' ')}"/>
    <polyline class="sp-you" points="${pts.map((p) => xy(p, 'you')).join(' ')}"/>`;
}

// ---------------------------------------------------------------- the queue
function itemGroup(it) { return ui.items.filter((x) => x.group_key === it.group_key); }

function renderQueue() {
  const seen = new Set();
  const rows = [];
  for (const it of ui.items) {
    if (it.group_size > 1) { if (seen.has(it.group_key)) continue; seen.add(it.group_key); }
    rows.push(it);
  }
  if (!rows.length) {
    $('qlist').innerHTML = `<li class="empty">${ui.state && ui.state.status === 'finished' ? 'The day is over. See Results.' : 'Nothing waiting for you. The clock runs at full speed until something comes in.'}</li>`;
  } else {
    $('qlist').innerHTML = rows.map((it) => {
      const n = it.group_size > 1 ? itemGroup(it).length : 1;
      const sel = ui.selected && (ui.selected === it.id || (n > 1 && itemGroup(it).some((x) => x.id === ui.selected)));
      const late = it.minutes_left <= 3;
      return `<li><button type="button" class="qitem ${sel ? 'selected' : ''} ${it.safety_legal ? 'safety' : ''}" data-id="${it.id}" aria-pressed="${sel}">
        <span class="q-top">
          <span class="q-icon">${icon(it.agent)}</span>
          <span class="q-title">${esc(n > 1 ? `${it.title.replace(/ to gate .*/, '')}: ${n} items` : it.title)}</span>
          <span class="q-time ${late ? 'late' : ''}">${it.status === 'escalated' ? 'Escalated' : `${it.minutes_left} min`}</span>
        </span>
        <span class="q-tags">
          ${it.safety_legal ? '<span class="tagx bad">Safety or legal</span>' : ''}
          ${it.urgent ? '<span class="tagx warn">Urgent</span>' : ''}
          ${it.passengers_seeing_wrong_info_now ? `<span class="tagx focus">${num(it.passengers_seeing_wrong_info_now)} seeing wrong info</span>` : ''}
          ${it.status === 'snoozed' ? `<span class="tagx quiet">Waiting until ${esc(it.snoozed_until)}</span>` : ''}
          ${it.last_option ? '<span class="tagx bad">Last option</span>' : ''}
          <span class="tagx quiet">${esc(it.role_title)}</span>
        </span></button></li>`;
    }).join('');
    $('qlist').querySelectorAll('[data-id]').forEach((b) => b.addEventListener('click', () => select(Number(b.dataset.id))));
  }
  if (!ui.items.some((x) => x.id === ui.selected)) select(rows[0] ? rows[0].id : null, true);
  else updateDetail();
}

// Re-draw the decision only when something about it changed, so reading and typing are never interrupted.
function detailSig(it) {
  return it ? [it.id, it.status, it.group_size, itemGroup(it).length, it.can_wait, it.last_option, it.consequence_if_rejected, ui.rejecting].join('|') : 'none';
}
function updateDetail() {
  const it = ui.items.find((x) => x.id === ui.selected);
  if (detailSig(it) !== ui.detailSig) { renderDetail(); return; }
  if (!it) return;
  const left = $('detail').querySelector('.d-left');
  if (left) left.textContent = it.status === 'escalated' ? 'Escalated' : `${it.minutes_left} min`;
  $('detail').querySelector('.d-clock')?.classList.toggle('late', it.minutes_left <= 3);
  const wi = $('detail').querySelector('.d-tags .focus');
  if (wi) wi.textContent = `${num(it.passengers_seeing_wrong_info_now)} passengers see wrong info until you approve this`;
}

function select(id, quiet = false) {
  if (ui.selected !== id) { ui.rejecting = false; ui.traceFor = null; }
  ui.selected = id;
  if (!quiet) renderQueue(); else renderDetail();
}

// ---------------------------------------------------------------- one decision
function renderDetail() {
  const box = $('detail');
  const it = ui.items.find((x) => x.id === ui.selected);
  ui.detailSig = detailSig(it);
  if (!it) {
    box.innerHTML = `<div class="detail-empty">${icon('orchestrator', 'ico big')}<p>When an agent needs a person, its plan, the way it got there and your options appear here.</p></div>`;
    return;
  }
  const group = it.group_size > 1 ? itemGroup(it) : [it];
  const w = it.why || {};
  const keepReason = box.querySelector('#reason')?.value || '';
  box.innerHTML = `
    <header class="d-head">
      <div>
        <p class="d-role">${icon(it.agent)} ${esc(agentName(it.agent))} asks <b>${esc(it.role_title)}</b></p>
        <h2>${esc(it.title)}</h2>
      </div>
      <div class="d-clock ${it.minutes_left <= 3 ? 'late' : ''}">
        <span class="d-left">${it.status === 'escalated' ? 'Escalated' : `${it.minutes_left} min`}</span>
        <span class="muted">${it.status === 'escalated' ? 'with the duty manager' : `to decide, by ${esc(it.deadline)}`}</span>
      </div>
    </header>
    <div class="d-tags">
      ${it.safety_legal ? '<span class="tagx bad">Safety or legal</span>' : ''}${it.urgent ? '<span class="tagx warn">Urgent</span>' : ''}
      ${it.passengers_affected ? `<span class="tagx quiet">${num(it.passengers_affected)} passengers affected</span>` : ''}
      ${it.passengers_seeing_wrong_info_now ? `<span class="tagx focus">${num(it.passengers_seeing_wrong_info_now)} passengers see wrong info until you approve this</span>` : ''}
    </div>
    <section class="d-sec"><h3>The plan</h3><p class="d-plan">${esc(w.plan)}</p>
      ${group.length > 1 ? `<ul class="group-list">${group.map((g) => `<li>${esc(g.title)}</li>`).join('')}</ul>` : ''}</section>
    <section class="d-sec"><h3>How it reached you</h3>
      <div class="trace" id="trace"><p class="muted">Loading the path…</p></div>
      <p class="trace-note muted" id="trace-note">Select a box to see what that step did.</p></section>
    <section class="d-sec"><h3>Why a person must decide</h3><p>${esc(w.why_human)}</p></section>
    <div class="d-cols">
      <section class="d-sec"><h3>What it used</h3><ul class="plain">
        ${(w.trigger || []).map((x) => `<li><span class="t">${esc(x.t)}</span> ${esc(x.text)} <span class="muted">(${esc(String(x.source).replace(/_/g, ' '))})</span></li>`).join('')}
        ${(w.inputs || []).map((x) => `<li><span class="t">${esc(x.t)}</span> ${esc(kindName(x.kind))} version ${esc(x.version)}</li>`).join('')}</ul></section>
      <section class="d-sec"><h3>Options it ruled out</h3>${(w.options_considered || []).length
        ? `<ul class="plain">${w.options_considered.map((o) => `<li><b>${esc(o.option)}</b>: ${esc(o.why_not)}</li>`).join('')}</ul>`
        : '<p class="muted">The agent did not record other options for this one.</p>'}</section>
    </div>
    <section class="d-sec"><h3>Rule check</h3><p><span class="status ${w.rule_check?.ok ? 'ok' : 'bad'}">${w.rule_check?.ok ? 'Passed' : 'Blocked'}</span>
      ${esc((w.rule_check?.rules || []).join(', '))}${w.rule_check?.text ? `: ${esc(w.rule_check.text)}` : ''}</p></section>
    ${it.consequence_if_rejected ? `<section class="d-sec warnbox"><h3>${it.last_option ? 'No other option is left' : 'If you reject'}</h3><p>${esc(it.consequence_if_rejected)}</p></section>` : ''}
    <footer class="actions">
      ${ui.rejecting ? `
        <label class="reason"><span>Why are you rejecting it? The agent uses this to find a different plan.</span>
          <textarea id="reason" rows="2" required>${esc(keepReason)}</textarea></label>
        <div class="act-row">
          <button class="btn danger" id="do-reject" type="button">Reject${group.length > 1 ? ` all ${group.length}` : ''}</button>
          <button class="btn" id="cancel-reject" type="button">Back</button>
        </div>` : `
        <div class="act-row">
          <button class="btn primary" id="do-approve" type="button">${group.length > 1 ? `Approve all ${group.length}` : 'Approve'}</button>
          <button class="btn" id="start-reject" type="button">Reject</button>
          <span class="wait-group">
            <button class="btn" data-wait="10" type="button" ${it.can_wait ? '' : 'disabled'}>Wait 10 min</button>
            <button class="btn" data-wait="15" type="button" ${it.can_wait ? '' : 'disabled'}>Wait 15 min</button>
          </span>
          <button class="btn" id="do-handoff" type="button">Hand off to duty manager</button>
        </div>
        ${it.can_wait ? '' : `<p class="blocked">Can't wait: ${esc(it.blocked_by.map((b) => b.title).join(', '))} depends on this.</p>`}`}
    </footer>`;
  const ids = group.map((g) => g.id);
  box.querySelector('#do-approve')?.addEventListener('click', () => decide(ids, { action: 'approve' }, 'Approved'));
  box.querySelector('#start-reject')?.addEventListener('click', () => { ui.rejecting = true; renderDetail(); box.querySelector('#reason').focus(); });
  box.querySelector('#cancel-reject')?.addEventListener('click', () => { ui.rejecting = false; renderDetail(); });
  box.querySelector('#do-reject')?.addEventListener('click', () => {
    const reason = box.querySelector('#reason').value.trim();
    if (!reason) { toast('Write a short reason first: the agent uses it to look for a different plan.', true); return; }
    decide(ids, { action: 'reject', reason }, 'Rejected. The agent will come back with a different plan if one exists.');
  });
  box.querySelectorAll('[data-wait]').forEach((b) => b.addEventListener('click', () => decide([it.id], { action: 'wait', minutes: Number(b.dataset.wait) }, `Waiting ${b.dataset.wait} min. It still escalates at its deadline.`)));
  box.querySelector('#do-handoff')?.addEventListener('click', () => decide(ids, { action: 'hand_off', to_role: 'director_of_operations' }, 'Handed off to the duty manager.'));
  loadTrace(it);
}

async function loadTrace(it) {
  const el = $('trace');
  if (ui.traceFor && ui.traceFor.id === it.id && ui.traceFor.graph) { paintTrace(ui.traceFor.graph); return; }
  ui.traceFor = { id: it.id };
  try {
    const g = await ui.api.trace(ui.run, it.id);
    if (ui.selected !== it.id) return;
    ui.traceFor.graph = g;
    paintTrace(g);
  } catch (e) { el.innerHTML = `<p class="muted">Could not load the path: ${esc(e.message)}</p>`; }
}
function paintTrace(g) {
  const el = $('trace');
  if (!el) return;
  drawTrace(el, g, { onNode: (n) => {
    $('trace-note').innerHTML = `<b>${esc(n.label)}</b> at ${esc(n.t)}${n.detail && n.detail.text ? `: ${esc(n.detail.text)}` : ''}`;
  } });
}

async function decide(ids, body, done) {
  if (ui.busy) return;
  ui.busy = true;
  try {
    const r = await ui.api.decide(ui.run, { question_ids: ids, ...body });
    const errs = r.results.filter((x) => x.error);
    if (errs.length) toast(errs.map((x) => x.error).join('; '), true); else toast(done);
    ui.rejecting = false; ui.traceFor = null;
    await refreshQueue();
  } catch (e) {
    toast(e.status === 409 ? e.message : `Could not send your answer: ${e.message}`, true);
  } finally { ui.busy = false; }
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
      sub = `${a.last_run ? 'checked ' + a.last_run : 'not run yet'}${v ? `, v${v.v}` : ''}`;
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
  const names = { situation: 'Situation', network_recovery: 'Network Recovery', passenger_care: 'Passenger Care', station_staff: 'Station & Staff', communication: 'Communication', feed: 'Feed' };
  const nice = (ref) => { const [k, v] = String(ref).split(':'); return `${names[k] || k} ${/^\d+$/.test(v) ? 'v' + v : v || ''}`.trim(); };
  const edges = [...(flow.edges || [])].reverse().slice(0, 12);
  $('ticker').innerHTML = edges.length ? edges.map((e) => `<li><span class="t">${esc(e.t)}</span> ${esc(nice(e.to))} was built on ${esc(nice(e.from))}</li>`).join('')
    : '<li class="muted">Nothing handed over yet.</li>';
}

// ---------------------------------------------------------------- history and results
async function refreshHistory() {
  const h = await ui.api.history(ui.run).catch(() => ({ decisions: [] }));
  $('history').innerHTML = `<thead><tr><th>Asked</th><th>Answered</th><th>Decision</th><th>Role</th><th>Your answer</th><th>Took</th></tr></thead><tbody>` +
    (h.decisions.length ? h.decisions.map((d) => `<tr><td>${esc(d.asked)}</td><td>${esc(d.answered)}</td><td>${esc(d.title)}</td><td>${esc(d.role)}</td>
      <td><span class="status ${d.answer === 'approved' ? 'ok' : d.answer === 'rejected' ? 'bad' : 'warn'}">${esc(ACTION_WORD[d.answer] || d.answer)}</span>${d.by === 'director_of_operations' ? ' <span class="muted">by duty manager</span>' : ''}${d.reason ? `<br><span class="muted">${esc(d.reason)}</span>` : ''}</td>
      <td>${esc(d.waited_min)} min</td></tr>`).join('') : '<tr><td colspan="6" class="muted">No decisions yet.</td></tr>') + '</tbody>';
}

async function refreshResults() {
  const r = await ui.api.results(ui.run).catch((e) => ({ error: e.message }));
  if (r.error) { $('results').innerHTML = `<div class="card"><p class="muted">Results are not ready: ${esc(r.error)}</p></div>`; return; }
  const cols = [['You and the co-pilot', r.you, 'you'], ["Today's process", r.today, 'today'], ['Co-pilot, approved automatically', r.orchestrated, 'auto']].filter((c) => c[1]);
  const row = (label, key, fmt = num) => `<tr><th>${label}</th>${cols.map(([, v, cls]) => `<td class="${cls}">${v[key] != null ? fmt(v[key]) : '-'}</td>`).join('')}</tr>`;
  const d = r.decisions || {};
  $('results').innerHTML = `
    <div class="card results-hero">
      <h2>${ui.state && ui.state.status === 'finished' ? 'How your day went' : 'How your day is going so far'}</h2>
      <p class="muted">The headline is the project's question: did every passenger see the true story on every screen?</p>
      <div class="board-wrap"><table class="board results-table">
        <thead><tr><th></th>${cols.map(([n]) => `<th>${esc(n)}</th>`).join('')}</tr></thead>
        <tbody>${row('Passenger-minutes on a wrong screen', 'wrong_minutes')}${row('Flights cancelled', 'cancelled')}${row('Passengers stranded overnight', 'stranded_overnight')}
        ${row(ui.state && ui.state.status === 'finished' ? `Where ${esc(ui.state.focus?.name || 'the passenger')} ended up` : `Where ${esc(ui.state?.focus?.name || 'the passenger')} is now`, 'focus_final', esc)}</tbody></table></div>
    </div>
    <div class="card"><h2>Your answers</h2><div class="stats five">
      ${[['Approved', d.approved], ['Rejected', d.rejected], ['Waited', d.waited], ['Handed off', d.handed_off], ['Escalated (no answer in time)', d.escalated], ['Median time to answer', `${d.median_answer_min ?? 0} min`]]
        .map(([l, v]) => `<div class="stat"><span class="stat-label">${l}</span><span class="stat-value">${v ?? 0}</span></div>`).join('')}</div></div>`;
}

// ---------------------------------------------------------------- polling
const agentName = (k) => ({ situation: 'Situation', network_recovery: 'Network Recovery', passenger_care: 'Passenger Care', station_staff: 'Station & Staff', communication: 'Communication', orchestrator: 'Orchestrator' }[k] || k);
const kindName = (k) => ({ situation: "Situation's picture", recovery_plan: 'Network Recovery plan', care_plan: 'Passenger Care plan', station_plan: 'Station & Staff plan', communication: 'Communication draft' }[k] || k);

async function refreshQueue() {
  const q = await ui.api.queue(ui.run, [...ui.view]).catch(() => null);
  if (!q) return;
  const before = new Set(ui.items.map((x) => x.id));
  ui.items = q.items;
  const fresh = q.items.filter((x) => !before.has(x.id));
  if (fresh.length && before.size + fresh.length > 0 && ui.started) {
    const urgent = fresh.find((x) => x.urgent);
    toast(urgent ? `Urgent: ${urgent.title}` : `New: ${fresh[0].title}`, !!urgent);
  }
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
    if (ui.tab === 'flow' && ticks % 2 === 0) await refreshFlow();
    if (ui.tab === 'history' && ticks % 3 === 0) await refreshHistory();
    if (ui.tab === 'results' && ticks % 5 === 0) await refreshResults();
  } catch (e) { $('status').textContent = `Lost the connection: ${e.message}`; }
  ticks++;
  setTimeout(loop, 1000);
}

// ---------------------------------------------------------------- boot
(async () => {
  ui.api = await pickApi();
  $('practice').hidden = !ui.api.practice;
  const run = new URLSearchParams(location.search).get('run');
  if (run && !ui.api.practice) open(run, []);
  else showPast();
  const auto = new URLSearchParams(location.search).get('autostart');      // for demos and screenshots
  if (auto && ui.api.practice) {
    const r = await ui.api.start({ seed: 7, scenario: 'a31_storm', roles: Object.keys(ROLE_TITLES) });
    if (auto !== '1') { ui.api.s.minute = Number(auto); for (let m = 661; m <= Number(auto); m++) ui.api._minute(m); }
    open(r.run_id, Object.keys(ROLE_TITLES));
  }
})();
