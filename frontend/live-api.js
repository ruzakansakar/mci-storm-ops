// Client for the live human-in-the-loop API (docs/live_api.md on branch live-hitl).
// If the live backend is not running yet (404) or ?mock=1 is set, a practice session runs in the browser instead,
// with the same JSON shapes and a scripted version of the seed 7 storm. Practice data is always labelled on screen.

const hm = (m) => { const v = Math.max(0, Math.round(m)); return `${String(Math.floor(v / 60) % 24).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`; };

async function http(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    const err = new Error(detail); err.status = r.status; throw err;
  }
  return r.json();
}

class RealApi {
  constructor() { this.practice = false; }
  start(body) { return http('POST', '/api/live/start', body); }
  control(id, body) { return http('POST', `/api/live/${enc(id)}/control`, body); }
  state(id) { return http('GET', `/api/live/${enc(id)}/state`); }
  queue(id, roles) { return http('GET', `/api/live/${enc(id)}/queue?roles=${roles.join(',')}`); }
  trace(id, qid) { return http('GET', `/api/live/${enc(id)}/questions/${qid}/trace`); }
  decide(id, body) { return http('POST', `/api/live/${enc(id)}/decide`, body); }
  flow(id, since) { return http('GET', `/api/live/${enc(id)}/flow?since=${since}`); }
  history(id) { return http('GET', `/api/live/${enc(id)}/history`); }
  sessions() { return http('GET', '/api/live/sessions'); }
  results(id) { return http('GET', `/api/live/${enc(id)}/results`); }
}
const enc = encodeURIComponent;

export const ROLE_TITLES = {
  station_manager: 'Station manager', network_operations_control: 'Network operations control', crew_scheduling: 'Crew scheduling',
  customer_service_supervisor: 'Customer service supervisor', ramp_supervisor: 'Ramp supervisor',
};

export async function pickApi() {
  const forced = new URLSearchParams(location.search).get('mock') === '1';
  if (!forced) {
    try { await http('GET', '/api/live/sessions'); return new RealApi(); } catch (e) { /* not there yet */ }
  }
  return new PracticeApi();
}

// ------------------------------------------------------------------ practice session (in the browser)
// A scripted copy of the seed 7 storm: the questions a person was really asked, with their reasons, deadlines and links.
const SCRIPT = [
  { at: 690, agent: 'network_recovery', action: 'extend_crew_duty', role: 'crew_scheduling', urgent: true, safety: true,
    title: 'Extend WN1031 crew duty by up to 2 h', target: { flight_id: 'WN1031', crew_id: 'C31' }, pax: 143, wrongInfo: 0,
    plan: 'Keep crew C31 on WN1031 (MCI to Phoenix) and let them work up to 2 hours past their limit so the flight can leave after the ground stop.',
    trigger: [['11:30', 'faa_feed', 'FAA ground stop at MCI for thunderstorms, expected end 13:00'], ['11:30', 'crew_feed', 'C31 becomes illegal at 14:05 if WN1031 leaves after 13:00']],
    inputs: [['situation', 1, '11:30'], ['recovery_plan', 96, '11:30']],
    options: [['Reserve crew', 'No reserve pilot is legal before 14:05'], ['Swap with another crew', 'No crew at MCI with enough duty time left'], ['Deadhead a crew in', 'No seats on inbound flights before 15:00']],
    rule: [true, ['CREW-07'], 'An extension is legal only with the captain\'s agreement and crew scheduling\'s approval'],
    whyHuman: 'Human only: a duty extension needs the captain and crew scheduling. The co-pilot may not decide it.',
    reject: { last: true, consequence: 'WN1031 will be cancelled and 143 passengers rebooked' } },
  { at: 691, key: 'delay1034', agent: 'network_recovery', action: 'delay_flight', role: 'network_operations_control', urgent: false, safety: false,
    title: 'Delay WN1034 to 13:43', target: { flight_id: 'WN1034' }, pax: 137, wrongInfo: 0,
    plan: 'Move WN1034 (MCI to San Diego) from 12:20 to 13:43, the first slot after the expected end of the ground stop.',
    trigger: [['11:30', 'faa_feed', 'Ground stop at MCI until about 13:00']], inputs: [['situation', 1, '11:30'], ['recovery_plan', 97, '11:31']],
    options: [['Cancel WN1034', '137 passengers stranded; the aircraft is needed tonight'], ['Delay to 13:15', 'Inside the stop\'s likely range, high chance of a second delay']],
    rule: [true, ['NET-02', 'CREW-01'], 'Crew stays legal with the new time'],
    whyHuman: 'Network operations control approves schedule changes.',
    reject: { alt: { title: 'Delay WN1034 to 14:30 instead', plan: 'Move WN1034 to 14:30, after the latest expected end of the stop, to avoid a second delay.' } } },
  { at: 692, dependsOn: 'delay1034', agent: 'network_recovery', action: 'assign_reserve_crew', role: 'crew_scheduling', urgent: false, safety: true,
    title: 'Put reserve crew R04 on WN1034', target: { flight_id: 'WN1034', crew_id: 'R04' }, pax: 137, wrongInfo: 0,
    plan: 'The planned crew would run out of duty time with the new departure. Reserve crew R04 takes WN1034.',
    trigger: [['11:31', 'network_recovery', 'WN1034 proposed for 13:43']], inputs: [['recovery_plan', 97, '11:31'], ['situation', 1, '11:30']],
    options: [['Extend the planned crew', 'Would need 40 min past their limit']], rule: [true, ['CREW-01', 'CREW-03'], 'R04 has 10 h rest and enough duty time'],
    whyHuman: 'Crew scheduling assigns reserves.', reject: { last: true, consequence: 'WN1034 will be delayed until its crew is legal again (about 16:10)' } },
  { at: 693, agent: 'communication', action: 'send_mass_message', role: 'customer_service_supervisor', urgent: true, safety: false,
    title: 'Tell 1,575 passengers about the ground stop', target: { flight_id: null }, pax: 1575, wrongInfo: 410, msg: true,
    plan: 'Send one message to everyone on affected flights by app and SMS: "Thunderstorms have stopped departures at Kansas City. We expect to restart around 13:00. We will update you by 12:30."',
    trigger: [['11:30', 'faa_feed', 'Ground stop at MCI'], ['11:31', 'channel_feed', 'Gate screens and the app still show on time']],
    inputs: [['situation', 1, '11:30'], ['recovery_plan', 97, '11:31']],
    options: [['Send flight by flight', 'Slower: 19 separate approvals'], ['Wait for the FAA end time', 'Passengers would see on time for another 30+ min']],
    rule: [true, ['MSG-01', 'MSG-04'], 'States only confirmed facts; promises a next update time it can keep'],
    whyHuman: 'Mass messages go to the customer service supervisor.',
    reject: { alt: { title: 'Tell passengers flight by flight about the ground stop', plan: 'Send a separate message per flight, each with its own expected time.' } } },
  ...[1, 2, 3, 4].map((i) => ({ at: 695, group: 'station_staff:move_gate_agents:695', agent: 'station_staff', action: 'move_gate_agents', role: 'station_manager',
    urgent: false, safety: false, title: `Move gate agent to gate ${['B53', 'B55', 'B57', 'B60'][i - 1]}`, target: { flight_id: ['WN1027', 'WN1028', 'WN1029', 'WN1030'][i - 1] }, pax: 120, wrongInfo: 0,
    plan: `Move one gate agent to gate ${['B53', 'B55', 'B57', 'B60'][i - 1]}, where delayed passengers are gathering.`,
    trigger: [['11:34', 'station_feed', 'Gate queues growing at B53 to B60']], inputs: [['station_plan', 40 + i, '11:34']],
    options: [], rule: [true, ['STA-02'], 'Breaks still covered'], whyHuman: 'The station manager moves staff.', reject: { last: true, consequence: 'Queues at that gate stay unstaffed' } })),
  { at: 722, agent: 'network_recovery', action: 'divert_flight', role: 'network_operations_control', urgent: true, safety: true,
    title: 'Divert WN2030 to Des Moines', target: { flight_id: 'WN2030' }, pax: 135, wrongInfo: 0,
    plan: 'WN2030 from Boston has been holding since 11:30. Its spare fuel is too low to keep holding: send it to Des Moines (25 min away) now.',
    trigger: [['11:30', 'flight_ops', 'WN2030 holding over MCI'], ['12:00', 'fuel_feed', '67 min of spare fuel left, must leave by 12:12']],
    inputs: [['situation', 2, '12:02'], ['recovery_plan', 101, '12:02']],
    options: [['St. Louis', 'Leave by 12:12, but St. Louis is filling up with diversions'], ['Omaha', 'Leave by 12:02: already too late'], ['Keep holding', 'Fuel runs out before the worst-case end of the stop']],
    rule: [true, ['FUEL-01', 'DIV-02'], 'Reaches Des Moines with legal reserve fuel'], whyHuman: 'A diversion needs network operations control and the dispatcher.',
    reject: { alt: { title: 'Divert WN2030 to St. Louis', plan: 'Send WN2030 to St. Louis instead (15 min away), leaving by 12:12.' } } },
  { at: 730, agent: 'station_staff', action: 'pause_ramp_tasks', role: 'ramp_supervisor', urgent: true, safety: true,
    title: 'Clear the ramp: lightning within 5 miles', target: { flight_id: null }, pax: 0, wrongInfo: 0,
    plan: 'Bring all 12 ramp workers inside now and stop fuelling and loading until the lightning moves away.',
    trigger: [['12:10', 'weather_feed', 'Lightning 4 miles from the field']], inputs: [['situation', 3, '12:10'], ['station_plan', 52, '12:10']],
    options: [], rule: [true, ['SAFE-01'], 'Lightning within 5 miles: ramp must close'], whyHuman: 'The ramp supervisor closes the ramp.',
    reject: { last: true, consequence: 'Ramp staff stay outside in lightning (not allowed: this will be escalated)' } },
  { at: 736, key: 'maya-div', agent: 'communication', action: 'send_correction', role: 'customer_service_supervisor', urgent: true, safety: false,
    title: 'Correct A31 passengers: diverted to St. Louis', target: { flight_id: 'WN2031', passenger_ids: ['PAX0001'] }, pax: 158, wrongInfo: 158, msg: true, maya: 'div',
    plan: 'Tell the 158 people on WN2031 (and those meeting them) that the flight is landing in St. Louis, not Kansas City, and will continue when the airport reopens.',
    trigger: [['12:15', 'flight_ops', 'WN2031 diverted to STL'], ['12:16', 'channel_feed', 'Gate screen still says "landing 13:02"']],
    inputs: [['situation', 4, '12:15'], ['recovery_plan', 104, '12:15']],
    options: [['Wait until it lands in St. Louis', 'Screens would be wrong for 25+ min']], rule: [true, ['MSG-01', 'MSG-06'], 'Corrects a wrong screen; no promise about the onward time'],
    whyHuman: 'Corrections go to the customer service supervisor.', reject: { last: true, consequence: '158 passengers keep seeing the wrong arrival until the gate screen catches up (about 30 min)' } },
  { at: 785, agent: 'communication', action: 'send_mass_message', role: 'customer_service_supervisor', urgent: false, safety: false,
    title: 'Tell everyone the stop is extended to 14:00', target: { flight_id: null }, pax: 2700, wrongInfo: 520, msg: true,
    plan: 'The FAA extended the ground stop to 14:00. Update every affected passenger and say when the next update comes (13:30).',
    trigger: [['13:00', 'faa_feed', 'Ground stop extended to 14:00']], inputs: [['situation', 9, '13:00']],
    options: [], rule: [true, ['MSG-01', 'MSG-04'], 'States only confirmed facts'], whyHuman: 'Mass messages go to the customer service supervisor.',
    reject: { last: true, consequence: '520 passengers keep seeing 13:00 on their screens' } },
  { at: 855, key: 'swap', agent: 'network_recovery', action: 'swap_crew', role: 'crew_scheduling', urgent: true, safety: true, maya: 'swap',
    title: 'Swap crew C35 onto WN1033 to Baltimore', target: { flight_id: 'WN1033', crew_id: 'C35', passenger_ids: ['PAX0001'] }, pax: 143, wrongInfo: 0,
    plan: 'WN1033\'s flight attendants run out of duty time. Crew C35 (arrived 14:10 on WN2031) is legal and free: put them on WN1033 so it leaves at 15:14.',
    trigger: [['14:10', 'flight_ops', 'WN2031 landed at MCI with crew C35'], ['14:12', 'crew_feed', 'WN1033 flight attendants illegal after 15:30']],
    inputs: [['situation', 17, '14:05'], ['recovery_plan', 131, '14:13']],
    options: [['Cancel WN1033', '143 passengers stranded overnight, including 12 connections'], ['Reserve crew', 'Earliest reserve reaches MCI at 16:40']],
    rule: [true, ['CREW-01', 'CREW-04'], 'C35 stays within duty limits with the 15:14 departure'], whyHuman: 'Crew scheduling approves crew swaps.',
    reject: { last: true, consequence: 'WN1033 will be cancelled: 143 passengers, including Maya Torres, stranded overnight' } },
  { at: 860, dependsOn: 'swap', agent: 'communication', action: 'send_status_update', role: 'customer_service_supervisor', urgent: true, safety: false, maya: 'swapmsg',
    title: 'Tell WN1033 passengers it leaves at 15:14', target: { flight_id: 'WN1033', passenger_ids: ['PAX0001'] }, pax: 143, wrongInfo: 143, msg: true,
    plan: 'Once the crew swap is approved, tell WN1033 passengers their flight now leaves at 15:14 from gate B58.',
    trigger: [['14:15', 'network_recovery', 'Crew swap proposed for WN1033']], inputs: [['recovery_plan', 131, '14:13']],
    options: [], rule: [true, ['MSG-01'], 'Only after the crew swap is approved'], whyHuman: 'Customer service supervisor approves options messages.',
    reject: { last: true, consequence: '143 passengers keep seeing "delayed, time to be confirmed"' } },
  { at: 917, agent: 'passenger_care', action: 'hold_hotel_rooms', role: 'customer_service_supervisor', urgent: false, safety: false,
    title: 'Hold 40 hotel rooms for stranded passengers', target: { flight_id: null }, pax: 62, wrongInfo: 0,
    plan: 'Hold 40 rooms at the two nearest partner hotels (shuttle every 20 min) for passengers stranded overnight.',
    trigger: [['15:15', 'passenger_care', '62 passengers have no flight until tomorrow']], inputs: [['care_plan', 210, '15:15'], ['situation', 17, '14:05']],
    options: [['Vouchers only', 'Leaves families to find rooms themselves at night']], rule: [true, ['CARE-02'], 'Within the station\'s hotel budget'],
    whyHuman: 'Hotel spending needs the customer service supervisor.', reject: { last: true, consequence: '62 passengers get vouchers and find their own rooms' } },
];

const AGENT_NAMES = { situation: 'Situation', network_recovery: 'Network Recovery', passenger_care: 'Passenger Care', station_staff: 'Station & Staff', communication: 'Communication' };

class PracticeApi {
  constructor() { this.practice = true; this.sessionsList = []; }

  async sessions() { return this.sessionsList.map((s) => ({ run_id: s.run_id, started_at: s.started_at, status: s.status, roles: s.roles, minute: Math.floor(s.minute) })); }

  async start(body) {
    const run_id = `${body.scenario || 'a31_storm'}-s${body.seed || 7}-live-practice-${this.sessionsList.length + 1}`;
    this.s = { run_id, roles: body.roles || [], status: 'running', minute: 660, speed_idle: 5, speed_pending: 1 / 6, started_at: new Date().toISOString(),
      questions: [], nextId: 1, scriptIdx: 0, decisions: [], approvedKeys: new Set(), wrongTotal: 0, todayTotal: 0, beat: performance.now(), lastReal: performance.now(),
      versions: { situation: 1, network_recovery: 96, passenger_care: 200, station_staff: 40, communication: 300 }, lastRun: {}, edges: [] };
    this.sessionsList.push(this.s);
    this.timer = setInterval(() => this._tick(), 250);
    return { run_id };
  }

  _tick() {
    const s = this.s;
    if (!s) return;
    const now = performance.now();
    const dt = (now - s.lastReal) / 1000; s.lastReal = now;
    if (s.status === 'running' && now - s.beat > 20000) s.status = 'paused';     // no heartbeat: auto-pause
    if (s.status !== 'running') return;
    const pending = s.questions.some((q) => ['waiting', 'snoozed'].includes(q.status));
    const before = s.minute;
    s.minute = Math.min(1439, s.minute + dt * (pending ? s.speed_pending : s.speed_idle));
    if (s.minute >= 1439) s.status = 'finished';
    for (let m = Math.floor(before) + 1; m <= Math.floor(s.minute); m++) this._minute(m);
  }

  _minute(m) {
    const s = this.s;
    while (s.scriptIdx < SCRIPT.length && SCRIPT[s.scriptIdx].at <= m) this._ask(SCRIPT[s.scriptIdx++], m);
    // agents keep working: each runs every few minutes and writes new versions in a storm
    const storm = m >= 690 && m <= 900;
    for (const [i, a] of Object.keys(AGENT_NAMES).entries()) {
      if ((m + i) % (storm ? 2 : 5) === 0) {
        s.lastRun[a] = m;
        if (storm && (m + i) % 6 === 0) {
          s.versions[a]++;
          const from = a === 'situation' ? 'feed:faa_feed' : `situation:${s.versions.situation}`;
          s.edges.push({ from, to: `${a}:${s.versions[a]}`, t: hm(m) });
        }
      }
    }
    for (const q of s.questions) {
      if (q.status === 'snoozed' && m >= q.snoozeEnd) q.status = 'waiting';
      if (['waiting', 'snoozed'].includes(q.status) && m >= q.deadlineMin) { q.status = 'escalated'; q.escalatedAt = m; }
      if (q.status === 'escalated' && m >= q.escalatedAt + 5) this._answer(q, 'approved', m, 'director_of_operations', 'Answered by the duty manager after it escalated');
      if (q.status === 'handed_off' && m >= q.handedAt + 6) this._answer(q, 'approved', m, 'director_of_operations', 'Duty manager approved the hand-off');
    }
    // wrong screens: the today baseline follows the storm; yours is lower when message approvals are quick
    const base = m < 690 ? 15 : m < 900 ? 260 + 120 * Math.sin((m - 690) / 210 * Math.PI) : Math.max(20, 260 - (m - 900) * 1.2);
    const waitingMsgs = s.questions.filter((q) => q.msg && !['approved', 'rejected'].includes(q.status)).reduce((a, q) => a + q.wrongInfo * Math.min(1, (m - q.askedMin) / 20), 0);
    s.todayNow = Math.round(base); s.wrongNow = Math.round(base * 0.38 + waitingMsgs);
    s.todayTotal += s.todayNow; s.wrongTotal += s.wrongNow;
  }

  _ask(sc, m, override = {}) {
    const s = this.s;
    const dep = sc.dependsOn ? [...s.questions].reverse().find((q) => q.key === sc.dependsOn) : null;
    const q = { ...sc, ...override, id: s.nextId++, askedMin: m, deadlineMin: m + (sc.urgent ? 10 : 15), status: 'waiting', depId: dep ? dep.id : null };
    if (dep && ['rejected'].includes(dep.status)) return;   // its parent was refused: nothing to ask
    s.questions.push(q);
    s.lastRun[q.agent] = m;
  }

  _answer(q, status, m, by, reason = '') {
    q.status = status; q.answeredMin = m;
    const s = this.s;
    s.decisions.push({ id: q.id, title: q.title, role: ROLE_TITLES[q.role] || q.role, action: q.action, answer: status, reason,
      asked: hm(q.askedMin), answered: hm(m), waited_min: Math.round(m - q.askedMin), by });
    if (status === 'approved' && q.key) s.approvedKeys.add(q.key);
    s.versions.communication += q.msg && status === 'approved' ? 1 : 0;
  }

  _shape(q) {
    const s = this.s, m = s.minute;
    const children = s.questions.filter((c) => c.depId === q.id && ['waiting', 'snoozed'].includes(c.status));
    const upcoming = SCRIPT.slice(s.scriptIdx).filter((c) => c.dependsOn && c.dependsOn === q.key);
    const blockers = [...children.map((c) => ({ id: c.id, title: c.title })), ...upcoming.map((c) => ({ id: null, title: c.title + ' (coming up)' }))];
    const safetyW = q.safety ? 60 : 0;
    const left = q.deadlineMin - m;
    return {
      id: q.id, group_key: q.group || `${q.agent}:${q.action}:${q.id}`, group_size: q.group ? s.questions.filter((x) => x.group === q.group && ['waiting', 'snoozed', 'escalated'].includes(x.status)).length : 1,
      agent: q.agent, action: q.action, role: q.role, role_title: ROLE_TITLES[q.role], title: q.title, target: { passenger_ids: [], ...q.target },
      asked: hm(q.askedMin), deadline: hm(q.deadlineMin), minutes_left: Math.max(0, Math.round(left)), urgent: !!q.urgent,
      priority_score: Math.round(safetyW + Math.max(0, 30 - left) + Math.min(10, q.pax / 150)), safety_legal: !!q.safety, passengers_affected: q.pax,
      passengers_seeing_wrong_info_now: q.msg ? Math.round(q.wrongInfo * Math.min(1, (m - q.askedMin) / 20 + 0.3)) : 0,
      status: q.status, snoozed_until: q.status === 'snoozed' ? hm(q.snoozeEnd) : null, can_wait: blockers.length === 0, blocked_by: blockers,
      last_option: !!q.lastOption, consequence_if_rejected: q.lastOption ? q.reject.consequence : (q.reject.last ? q.reject.consequence : ''),
      why: { plan: q.plan, trigger: q.trigger.map(([t, source, text]) => ({ t, source, text })), inputs: q.inputs.map(([kind, version, t]) => ({ kind, version, t })),
        options_considered: q.options.map(([option, why_not]) => ({ option, why_not })), rule_check: { ok: q.rule[0], rules: q.rule[1], text: q.rule[2] }, why_human: q.whyHuman },
    };
  }

  async control(id, body) {
    const s = this.s;
    if (body.action === 'pause') s.status = 'paused';
    if (body.action === 'resume' && s.status === 'paused') { s.status = 'running'; s.beat = performance.now(); }
    if (body.action === 'speed') { if (body.speed_idle) s.speed_idle = body.speed_idle; if (body.speed_pending) s.speed_pending = body.speed_pending; }
    return this.state(id);
  }

  async state() {
    const s = this.s; s.beat = performance.now();
    const m = Math.floor(s.minute);
    const open = s.questions.filter((q) => ['waiting', 'snoozed', 'escalated'].includes(q.status));
    return { run_id: s.run_id, status: s.status, minute: m, clock: hm(m), speed_idle: s.speed_idle, speed_pending: s.speed_pending, roles: s.roles,
      pending: open.length, urgent_pending: open.filter((q) => q.urgent).length,
      incidents: m >= 690 ? [{ id: 'A10', status: 'open', title: 'Thunderstorm ground stop at MCI' }] : [],
      world: { ground_stop: m >= 690 && m < 840, ramp_open: !(m >= 730 && m < 765), stop_expected_end: m < 780 ? '13:00' : '14:00' },
      truth: { wrong_now: s.wrongNow || 0, wrong_minutes_total: Math.round(s.wrongTotal), today_wrong_now: s.todayNow || 0, today_wrong_minutes_total: Math.round(s.todayTotal) },
      focus: this._maya(m) };
  }

  _maya(m) {
    const s = this.s;
    const swapOk = s.approvedKeys.has('swap');
    const swapNo = s.questions.some((q) => q.key === 'swap' && q.lastOption && q.status === 'rejected');
    const truthAt = (t) => t < 705 ? 'WN2031 from Denver, landing 11:45' : t < 735 ? 'WN2031 holding over Kansas City' : t < 810 ? 'WN2031 diverted to St. Louis' :
      t < 850 ? 'WN2031 flying St. Louis to Kansas City' : t < 875 ? 'Landed MCI. WN1033 to Baltimore: delayed' :
      swapOk ? 'WN1033 to Baltimore leaves 15:14, gate B58' : swapNo ? 'WN1033 cancelled. Rebooked WN3005, 06:30 tomorrow' : 'Landed MCI. WN1033 to Baltimore: delayed';
    const told = (key) => { const q = s.questions.find((x) => x.key === key && x.status === 'approved'); return q && q.status === 'approved' ? q.answeredMin : null; };
    const divTold = told('maya-div'), swapTold = s.questions.find((q) => q.maya === 'swapmsg' && q.status === 'approved')?.answeredMin ?? null;
    const lagFor = (ch) => {
      const fast = (ch === 'app' || ch === 'sms');
      if (!fast) return ch === 'gate_screen' ? 9 : 17;
      if (m >= 735 && m < 810) return divTold != null ? Math.max(1, divTold - 735) : 99;
      if (m >= 875) return swapTold != null ? Math.max(1, swapTold - 875) : 99;
      return 3;
    };
    const truth = truthAt(m);
    return { passenger_id: 'PAX0001', name: 'Maya Torres', true_status: truth,
      screens: ['app', 'sms', 'gate_screen', 'call_center'].map((ch) => {
        const lag = lagFor(ch), shown = truthAt(m - lag);
        const wrong = shown !== truth;
        let since = wrong ? m : Math.max(660, m - lag);
        if (wrong) { while (since > 660 && truthAt(since - 1) !== shown) since--; }   // wrong since the truth moved on
        else { while (since > 660 && truthAt(since - 1 - lag) === shown) since--; }
        return { channel: ch, shows: shown, wrong, since: hm(since) };
      }) };
  }

  async queue(id, roles) {
    const s = this.s;
    const items = s.questions.filter((q) => ['waiting', 'snoozed', 'escalated'].includes(q.status) && (!roles.length || roles.includes(q.role)))
      .map((q) => this._shape(q)).sort((a, b) => b.priority_score - a.priority_score);
    return { items };
  }

  async decide(id, body) {
    const s = this.s, m = Math.floor(s.minute);
    const results = [];
    for (const qid of body.question_ids) {
      const q = s.questions.find((x) => x.id === qid);
      if (!q || !['waiting', 'snoozed', 'escalated'].includes(q.status)) { results.push({ id: qid, status: q ? q.status : 'unknown', error: 'Already answered' }); continue; }
      if (body.action === 'approve') this._answer(q, 'approved', m, q.role);
      if (body.action === 'reject') {
        if (!body.reason) { results.push({ id: qid, status: q.status, error: 'A reason is needed to reject' }); continue; }
        this._answer(q, 'rejected', m, q.role, body.reason);
        if (q.lastOption) { q.consequenceApplied = true; }
        else if (q.reject.alt) this._ask({ ...q, ...q.reject.alt, at: m, key: q.key, reject: { last: true, consequence: q.reject.altConsequence || 'The original problem stays: today\'s process takes over' } }, m, { lastOption: false });
        else if (q.reject.last) this._ask({ ...q, at: m, title: q.title + ' (no other option)', plan: q.plan }, m, { lastOption: true });
        for (const c of s.questions) if (c.depId === q.id && ['waiting', 'snoozed'].includes(c.status)) { c.status = 'withdrawn'; }
      }
      if (body.action === 'wait') {
        const shaped = this._shape(q);
        if (!shaped.can_wait) { const e = new Error(`Can't wait: ${shaped.blocked_by.map((b) => b.title).join(', ')} depends on this`); e.status = 409; throw e; }
        q.status = 'snoozed'; q.snoozeEnd = m + (body.minutes || 10);
        s.decisions.push({ id: q.id, title: q.title, role: ROLE_TITLES[q.role], action: q.action, answer: 'waited', reason: `${body.minutes || 10} min`, asked: hm(q.askedMin), answered: hm(m), waited_min: Math.round(m - q.askedMin) });
      }
      if (body.action === 'hand_off') {
        q.status = 'handed_off'; q.handedAt = m;
        s.decisions.push({ id: q.id, title: q.title, role: ROLE_TITLES[q.role], action: q.action, answer: 'handed off', reason: 'to the duty manager', asked: hm(q.askedMin), answered: hm(m), waited_min: Math.round(m - q.askedMin) });
      }
      results.push({ id: qid, status: q.status, error: null });
    }
    return { ok: results.every((r) => !r.error), results };
  }

  async trace(id, qid) {
    const q = this.s.questions.find((x) => x.id === qid);
    const nodes = [], edges = [];
    const add = (n) => { nodes.push(n); return n.id; };
    const feeds = q.trigger.filter(([, src]) => !AGENT_NAMES[src]).map(([t, src, text], i) => add({ id: `f${i}`, kind: 'feed', label: src.replace(/_/g, ' '), t, detail: { text } }));
    const sit = q.inputs.find(([k]) => k === 'situation');
    const sitId = sit ? add({ id: 's', kind: 'situation', label: `Situation picture v${sit[1]}`, t: sit[2], detail: { text: 'What is happening, how sure, likely end' } }) : null;
    feeds.forEach((f) => edges.push({ from: f, to: sitId || 'a', label: 'used', t: nodes.find((n) => n.id === f).t }));
    const planKind = { recovery_plan: 'network_recovery', care_plan: 'passenger_care', station_plan: 'station_staff' };
    let last = sitId;
    for (const [k, v, t] of q.inputs) {
      if (!planKind[k]) continue;
      const nid = add({ id: `p${v}`, kind: planKind[k], label: `${AGENT_NAMES[planKind[k]]} plan v${v}`, t, detail: { text: 'Built on the shared state' } });
      if (sitId) edges.push({ from: sitId, to: nid, label: 'used', t });
      last = nid;
    }
    if (q.agent === 'communication') {
      const c = add({ id: 'c', kind: 'communication', label: 'Communication draft', t: hm(q.askedMin), detail: { text: q.plan } });
      if (last) edges.push({ from: last, to: c, label: 'used', t: hm(q.askedMin) });
      last = c;
    }
    if (!last) last = feeds[0];
    const rc = add({ id: 'r', kind: 'rule_check', label: `Rule check: ${q.rule[0] ? 'passed' : 'blocked'}`, t: hm(q.askedMin), detail: { text: `${q.rule[1].join(', ')}: ${q.rule[2]}` } });
    edges.push({ from: last, to: rc, label: 'checked', t: hm(q.askedMin) });
    const orch = add({ id: 'o', kind: 'orchestrator', label: 'Orchestrator: needs a person', t: hm(q.askedMin), detail: { text: q.whyHuman } });
    edges.push({ from: rc, to: orch, label: 'sent to', t: hm(q.askedMin) });
    const qq = add({ id: 'q', kind: 'question', label: q.title, t: hm(q.askedMin), detail: { text: `${ROLE_TITLES[q.role]} must decide by ${hm(q.deadlineMin)}` } });
    edges.push({ from: orch, to: qq, label: 'sent to', t: hm(q.askedMin) });
    if (['approved', 'rejected'].includes(q.status)) {
      const h = add({ id: 'h', kind: 'human', label: `You: ${q.status}`, t: hm(q.answeredMin), detail: {} });
      edges.push({ from: qq, to: h, label: 'answered', t: hm(q.answeredMin) });
    }
    return { question_id: qid, snapshot: ['approved', 'rejected'].includes(q.status) ? 'answered' : 'asked', nodes, edges };
  }

  async flow(id, since) {
    const s = this.s;
    const waiting = (a) => s.questions.filter((q) => q.agent === a && ['waiting', 'snoozed', 'escalated'].includes(q.status)).length;
    return { agents: Object.entries(AGENT_NAMES).map(([key, name]) => ({ key, name, last_run: s.lastRun[key] != null ? hm(s.lastRun[key]) : null,
      versions: [{ v: s.versions[key], t: hm(s.lastRun[key] ?? s.minute), used: key === 'situation' ? [{ kind: 'feed', ref: 'faa_feed' }] : [{ kind: 'situation', ref: `v${s.versions.situation}` }] }],
      waiting_on_human: waiting(key) })),
      orchestrator: { open_incidents: s.minute >= 690 ? 1 : 0, queue: s.questions.filter((q) => ['waiting', 'snoozed', 'escalated'].includes(q.status)).length },
      edges: s.edges.filter((e) => toMin(e.t) >= since).slice(-30) };
  }

  async history() { return { decisions: [...this.s.decisions].reverse() }; }

  async results() {
    const s = this.s;
    const count = (a) => s.decisions.filter((d) => d.answer === a).length;
    const waits = s.decisions.filter((d) => d.answer === 'approved' || d.answer === 'rejected').map((d) => d.waited_min).sort((a, b) => a - b);
    const swapOk = s.approvedKeys.has('swap');
    return {
      you: { wrong_minutes: Math.round(s.wrongTotal), cancelled: swapOk ? 3 : 4, stranded_overnight: swapOk ? 290 : 433, focus_final: this._maya(Math.floor(s.minute)).true_status },
      today: { wrong_minutes: Math.round(s.todayTotal), cancelled: 5, stranded_overnight: 559, focus_final: 'WN1033 cancelled. Rebooked WN3005, 06:30 tomorrow' },
      orchestrated: { wrong_minutes: 6080, cancelled: 3, stranded_overnight: 290, focus_final: 'WN1033 to Baltimore left 15:14' },
      decisions: { approved: count('approved'), rejected: count('rejected'), waited: count('waited'), handed_off: count('handed off'),
        escalated: s.decisions.filter((d) => d.by === 'director_of_operations').length, median_answer_min: waits.length ? waits[Math.floor(waits.length / 2)] : 0 },
    };
  }
}
const toMin = (s) => { const [h, m] = String(s).split(':').map(Number); return h * 60 + m; };
