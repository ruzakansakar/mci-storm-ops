// Live control room: the user is the human in the loop for every decision the agents cannot make alone.
import { pickApi, ROLE_TITLES } from './live-api.js';
import { icon } from './icons.js';
import { drawDecisionFlow } from './graph.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (n) => Number(n || 0).toLocaleString();
const CHANNELS = { app: 'App', sms: 'SMS', gate_screen: 'Gate screen', call_center: 'Call center' };
const ACTION_WORD = { approved: 'Approved', rejected: 'Rejected', waited: 'Waiting', 'handed off': 'Handed off' };

const ui = { api: null, run: null, state: null, items: [], selected: null, roles: Object.keys(ROLE_TITLES), view: new Set(Object.keys(ROLE_TITLES)),
  tab: 'decisions', series: [], traceFor: null, rejecting: false, busy: false, decided: new Map() };

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
  $('wrong-now-sub').textContent = t.wrong_now ? 'until the co-pilot\'s fixes reach their screens' : 'every screen matches the truth';
  $('wrong-total').textContent = num(t.wrong_minutes_total);
  $('wrong-total-sub').textContent = 'one passenger on one wrong screen for one minute = 1';
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
  const max = Math.max(10, ...pts.map((p) => p.you)) * 1.1;
  const xy = (p, k) => `${((p.m - m0) / (m1 - m0) * 300).toFixed(1)},${(70 - p[k] / max * 66).toFixed(1)}`;
  $('spark').innerHTML = `<polyline class="sp-you" points="${pts.map((p) => xy(p, 'you')).join(' ')}"/>`;
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
  if (!ui.items.some((x) => x.id === ui.selected) && !ui.decided.has(ui.selected)) select(rows[0] ? rows[0].id : null, true);
  else updateDetail();
}

// the decision on screen: still waiting (from the queue) or already answered (kept so you can watch it go out)
function current() {
  const live = ui.items.find((x) => x.id === ui.selected);
  if (live) return { it: live, rec: null };
  const rec = ui.decided.get(ui.selected);
  return rec ? { it: rec.item, rec } : { it: null, rec: null };
}

// Re-draw the decision only when something about it changed, so reading and typing are never interrupted.
function detailSig() {
  const { it, rec } = current();
  return it ? [it.id, it.status, it.group_size, rec ? `${rec.answer}:${ui.items.length > 0}` : itemGroup(it).length, it.can_wait, it.last_option, it.consequence_if_rejected, ui.rejecting].join('|') : 'none';
}
function updateDetail() {
  const { it, rec } = current();
  if (detailSig() !== ui.detailSig) { renderDetail(); return; }
  if (rec) return;
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
  const { it, rec } = current();
  ui.detailSig = detailSig();
  if (!it) {
    box.innerHTML = `<div class="detail-empty">${icon('orchestrator', 'ico big')}<p>When an agent needs a person, its plan, the way it got there and your options appear here.</p></div>`;
    return;
  }
  const group = rec ? rec.group : it.group_size > 1 ? itemGroup(it) : [it];
  const w = it.why || {};
  const ANSWER = { approved: 'Approved', rejected: 'Rejected', hand_off: 'Handed off to the duty manager' };
  const keepReason = box.querySelector('#reason')?.value || '';
  box.innerHTML = `
    <header class="d-head">
      <div>
        <p class="d-role">${icon(it.agent)} ${esc(agentName(it.agent))} asks <b>${esc(it.role_title)}</b></p>
        <h2>${esc(it.title)}</h2>
      </div>
      ${rec ? `<div class="d-clock done ${rec.answer}"><span class="d-left">${esc(ANSWER[rec.answer] || rec.answer)}</span><span class="muted">at ${esc(rec.at)}</span></div>` : `
      <div class="d-clock ${it.minutes_left <= 3 ? 'late' : ''}">
        <span class="d-left">${it.status === 'escalated' ? 'Escalated' : `${it.minutes_left} min`}</span>
        <span class="muted">${it.status === 'escalated' ? 'with the duty manager' : `to decide, by ${esc(it.deadline)}`}</span>
      </div>`}
    </header>
    <div class="d-tags">
      ${it.safety_legal ? '<span class="tagx bad">Safety or legal</span>' : ''}${it.urgent ? '<span class="tagx warn">Urgent</span>' : ''}
      ${it.passengers_affected ? `<span class="tagx quiet">${num(it.passengers_affected)} passengers affected</span>` : ''}
      ${it.passengers_seeing_wrong_info_now ? `<span class="tagx focus">${num(it.passengers_seeing_wrong_info_now)} passengers see wrong info until you approve this</span>` : ''}
    </div>
    <section class="d-sec"><h3>The plan</h3><p class="d-plan">${esc(w.plan)}</p>
      ${group.length > 1 ? `<ul class="group-list">${group.map((g) => `<li>${esc(g.title)}</li>`).join('')}</ul>` : ''}</section>
    <section class="d-sec"><h3>${rec ? 'From what happened to every screen' : 'How it reached you, and where it goes next'}</h3>
      <div class="trace" id="trace"><p class="muted">Loading the path…</p></div>
      <p class="trace-note muted" id="trace-note">Select any box to see what that step did.</p></section>
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
      ${rec ? `
        <div class="act-row">
          ${ui.items.length ? `<button class="btn primary" id="next" type="button">Go to the next decision (${ui.items.length} waiting)</button>` : '<span class="muted">Nothing else is waiting for you.</span>'}
        </div>` : ui.rejecting ? `
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
  box.querySelector('#next')?.addEventListener('click', () => { ui.selected = null; renderQueue(); });
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
  if (ui.traceFor && ui.traceFor.id === it.id && ui.traceFor.graph) { paintTrace(); return; }
  ui.traceFor = { id: it.id };
  try {
    const g = await ui.api.trace(ui.run, it.id);
    if (ui.selected !== it.id) return;
    ui.traceFor.graph = g;
    paintTrace();
  } catch (e) { el.innerHTML = `<p class="muted">Could not load the path: ${esc(e.message)}</p>`; }
}
async function refreshTrace() {
  const { it, rec } = current();
  if (!it || !rec || !ui.traceFor || ui.traceFor.id !== it.id) return;
  const g = await ui.api.trace(ui.run, it.id).catch(() => null);
  if (!g || ui.selected !== it.id) return;
  const key = JSON.stringify(g.nodes.map((n) => [n.id, n.detail && n.detail.status]));
  if (key === ui.traceFor.key) return;
  ui.traceFor.graph = g; ui.traceFor.key = key;
  paintTrace();
}

const AGENT_KEYS = ['situation', 'network_recovery', 'passenger_care', 'station_staff', 'communication'];
const INPUT_AGENT = { situation: 'situation', recovery_plan: 'network_recovery', care_plan: 'passenger_care', station_plan: 'station_staff', communication: 'communication' };

// Turn the trace into the three parts of the flowchart: how it came about, you, and where it went.
function flowModel(it, rec, g) {
  const nodes = g.nodes || [];
  const qNode = nodes.find((n) => n.kind === 'question');
  const human = nodes.find((n) => n.kind === 'human');
  const after = (n) => human && n.t && n.t >= human.t && n.kind === 'communication' && nodes.indexOf(n) > nodes.indexOf(human);
  const w = it.why || {};
  const feeds = nodes.filter((n) => n.kind === 'feed').map((n) => ({ label: n.label, t: n.t, text: n.detail?.text || '' }));
  if (!feeds.length) for (const x of w.trigger || []) feeds.push({ label: String(x.source).replace(/_/g, ' '), t: x.t, text: x.text });
  const agents = AGENT_KEYS.map((key) => {
    const mine = nodes.filter((n) => n.kind === key && !after(n));
    const used = (w.inputs || []).filter((x) => INPUT_AGENT[x.kind] === key);
    const involved = mine.length > 0 || used.length > 0 || it.agent === key;
    const lastN = mine[mine.length - 1], lastU = used[used.length - 1];
    const short = lastN ? (lastN.label.match(/v\d+/) || [''])[0] : lastU ? `v${lastU.version}` : it.agent === key ? 'asked' : '';
    return { key, name: agentName(key), involved, short: short.trim(),
      label: agentName(key), t: lastN?.t || lastU?.t || '',
      text: involved ? (lastN?.detail?.text || (it.agent === key ? w.plan : `${kindName(lastU?.kind)} version ${lastU?.version}`)) : 'Had nothing to add to this decision.' };
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
  const changes = preview ? (g.preview?.changes?.length ? g.preview.changes : [{ label: it.title }]).map((c) => ({ label: c.label, t: '', text: c.label })) :
    changeNodes.map((n) => ({ label: n.label, t: n.t, text: n.detail?.text || n.label }));
  const comm = nodes.filter((n) => n.kind === 'communication' && after(n)).pop();
  const destNodes = nodes.filter((n) => n.kind === 'channel' || n.kind === 'staff');
  const iconOf = (k) => ({ app: 'app', sms: 'sms', gate_screen: 'gate_screen', call_center: 'call_center', call_center_agents: 'call_center', crew: 'crew' }[k] || 'staff');
  const dests = preview
    ? (g.preview?.destinations?.length ? g.preview.destinations : ['App', 'SMS', 'Gate screens', 'Call center screens'].map((label) => ({ label })))
      .map((d) => ({ label: d.label, icon: d.icon || iconOf(d.key), status: 'preview', text: 'Will get the same update at the same time' }))
    : destNodes.map((n) => ({ label: n.label, icon: n.detail?.icon || iconOf(n.detail?.key || n.label), status: n.detail?.status || 'waiting', t: n.t, text: n.detail?.text || '' }));
  const hub = { label: 'One truth', sub: comm ? (comm.label.match(/v\d+/) || ['sent'])[0] : 'Communication', t: comm?.t,
    text: comm?.detail?.text ? `Sent to everyone: "${comm.detail.text}"` : 'The change is written once to the shared state and Communication sends that one version to every screen and every person at the same time.' };
  const rejected = answer === 'rejected' ? { agent: it.agent, label: `${agentName(it.agent)} is looking for a different plan`, sub: rec.reason ? `Your reason: ${rec.reason}` : '', text: it.consequence_if_rejected || '' } : null;
  return { feeds, agents, rule, orch, you, changes, hub, dests, preview, rejected };
}

function paintTrace() {
  const el = $('trace');
  const { it, rec } = current();
  if (!el || !it || !ui.traceFor?.graph) return;
  drawDecisionFlow(el, flowModel(it, rec, ui.traceFor.graph), { onNode: (n) => {
    $('trace-note').innerHTML = `<b>${esc(n.label || n.name || '')}</b>${n.t ? ` at ${esc(n.t)}` : ''}${n.text ? `: ${esc(n.text)}` : ''}`;
  } });
}

async function decide(ids, body, done) {
  if (ui.busy) return;
  ui.busy = true;
  try {
    const group = ids.map((id) => ui.items.find((x) => x.id === id)).filter(Boolean);
    const r = await ui.api.decide(ui.run, { question_ids: ids, ...body });
    const errs = r.results.filter((x) => x.error);
    if (errs.length) toast(errs.map((x) => x.error).join('; '), true); else toast(done);
    if (body.action !== 'wait' && group.length && !errs.length) {     // keep it on screen to watch it go out
      const rec = { item: group[0], group, answer: body.action === 'approve' ? 'approved' : body.action === 'reject' ? 'rejected' : body.action, reason: body.reason || '', at: ui.state?.clock || '' };
      for (const g of group) ui.decided.set(g.id, rec);
      ui.selected = group[0].id;
    }
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
    (h.decisions.length ? h.decisions.map((d) => `<tr ${ui.decided.has(d.id) ? `data-open="${d.id}" tabindex="0" class="clickable"` : ''}><td>${esc(d.asked)}</td><td>${esc(d.answered)}</td><td>${esc(d.title)}</td><td>${esc(d.role)}</td>
      <td><span class="status ${d.answer === 'approved' ? 'ok' : d.answer === 'rejected' ? 'bad' : 'warn'}">${esc(ACTION_WORD[d.answer] || d.answer)}</span>${d.by === 'director_of_operations' ? ' <span class="muted">by duty manager</span>' : ''}${d.reason ? `<br><span class="muted">${esc(d.reason)}</span>` : ''}</td>
      <td>${esc(d.waited_min)} min</td></tr>`).join('') : '<tr><td colspan="6" class="muted">No decisions yet.</td></tr>') + '</tbody>';
  $('history').querySelectorAll('[data-open]').forEach((r) => r.addEventListener('click', () => {
    ui.selected = Number(r.dataset.open); ui.traceFor = null;
    document.querySelector('[data-tab="decisions"]').click(); renderDetail();
  }));
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
    if (ui.tab === 'decisions' && ticks % 2 === 0) await refreshTrace();
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
