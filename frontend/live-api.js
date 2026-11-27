// Client for the live human-in-the-loop API (docs/live_api.md on branch live-hitl).
// If the live backend is not running yet (404) or ?mock=1 is set, a practice session runs in the browser instead,
// with the same JSON shapes and a scripted version of the seed 7 storm. Practice data is always labelled on screen.

const hm = (m) => { const v = Math.max(0, Math.round(m)); return `${String(Math.floor(v / 60) % 24).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`; };

async function http(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const d = (await r.json()).detail;
      if (Array.isArray(d)) detail = d.map((x) => `${(x.loc || []).slice(-1)[0] || 'value'}: ${x.msg}`).join('; ');
      else if (d && typeof d === 'object') detail = d.message || d.reason || JSON.stringify(d);
      else if (d) detail = d;
    } catch (e) {}
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
  flight(id, f) { return http('GET', `/api/live/${enc(id)}/flights/${enc(f)}`); }
  addNote(id, f, body) { return http('POST', `/api/live/${enc(id)}/flights/${enc(f)}/notes`, body); }
  report(id, body) { return http('POST', `/api/live/${enc(id)}/reports`, body); }
  customers(id, q) { return http('GET', `/api/live/${enc(id)}/customers?q=${enc(q)}`); }
  customer(id, c) { return http('GET', `/api/live/${enc(id)}/customers/${enc(c)}`); }
  rebook(id, c, body) { return http('POST', `/api/live/${enc(id)}/customers/${enc(c)}/rebook`, body); }
  refund(id, c) { return http('POST', `/api/live/${enc(id)}/customers/${enc(c)}/refund`, {}); }
  station(id) { return http('GET', `/api/live/${enc(id)}/station`); }
  clocks(id) { return http('GET', `/api/live/${enc(id)}/clocks`); }
  handover(id, since) { return http('GET', `/api/live/${enc(id)}/handover?since=${enc(since)}`); }
  sent(id, since) { return http('GET', `/api/live/${enc(id)}/sent?since=${since}`); }
  outOfDate(id) { return http('GET', `/api/live/${enc(id)}/out_of_date`); }
  rebookOptions(id, c) { return http('GET', `/api/live/${enc(id)}/customers/${enc(c)}/rebooking?reason=customer_request`); }
  flightCustomers(id, f) { return http('GET', `/api/live/${enc(id)}/flights/${enc(f)}/customers`); }
  flightRebook(id, f, assignments) { return http('POST', `/api/live/${enc(id)}/flights/${enc(f)}/rebook`, { assignments }); }
}
const enc = encodeURIComponent;

export const FRONTLINE = { counter_gate_agent: 'Counter and gate agent' };
export const ROLE_TITLES = {
  station_manager: 'Station manager', network_operations_control: 'Network operations control', crew_scheduling: 'Crew scheduling',
  customer_service_supervisor: 'Customer service supervisor', ramp_supervisor: 'Ramp supervisor',
};

export async function pickApi() {
  const forced = new URLSearchParams(location.search).get('mock') === '1';
  if (!forced) {
    try { await http('GET', '/api/live/sessions'); return new RealApi(); }
    catch (e) { if (e.status !== 404) throw e; }      // only a missing live backend means practice; other errors are shown
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
    options: [['Reserve crew', 'No reserve pilot is legal before 14:05'], ['Swap with another crew', 'No crew at MCI with enough duty time left'], ['Deadhead a crew in', 'No seats on inbound flights before 15:00']], headline: 'Keep WN1031 to Phoenix flying today', optLabel: 'Extend crew C31 by up to 2 h (captain agrees)', connections: 12,
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
  { at: 700, key: 'care1032', agent: 'passenger_care', action: 'rebook_customers', role: 'customer_service_supervisor', urgent: true, safety: false,
    title: 'Rebook 4 Customers from cancelled WN1032 to Dallas', headline: 'Rebook 4 Customers from cancelled WN1032 to Dallas', target: { flight_id: 'WN1032' }, pax: 4, wrongInfo: 0,
    plan: 'WN1032 to Dallas is cancelled. Put each Customer on the best flight still open today, or tomorrow with a hotel when nothing works tonight.',
    trigger: [['11:30', 'network_recovery', 'WN1032 cancelled: no legal crew before the stop ends'], ['11:39', 'passenger_care', '4 Customers on WN1032 need a new way to Dallas']],
    inputs: [['care_plan', 204, '11:39'], ['recovery_plan', 98, '11:30']], options: [], rule: [true, [], 'Within the hotel budget; no Customer left without a plan'],
    whyHuman: 'Rebooking a whole flight goes to the customer service supervisor.', reject: { last: true, consequence: 'The 4 Customers stay without a flight until an agent rebooks them at the counter.' },
    care: { customers: [
      { id: 'PAX0102', name: 'Daniel Okafor', action: 'rebook', detail: { flights: [{ flight_id: 'WN1046', dep: '16:10', arr: '17:45' }] }, why: 'Earliest seat to Dallas today; wheelchair booked at both ends' },
      { id: 'PAX0144', name: 'Priya Nair', action: 'rebook', detail: { flights: [{ flight_id: 'WN1046', dep: '16:10', arr: '17:45' }] }, why: 'Same flight, keeps her meeting tonight' },
      { id: 'PAX0310', name: 'Luis Romero', action: 'hotel', detail: { flights: [{ flight_id: 'WN1050', dep: '07:05', arr: '08:40' }], hotel: { name: 'Hilton KCI Airport', price: 139, shuttle: 'every 20 min, door 4' } }, why: 'Tonight is full; first flight tomorrow plus a hotel' },
      { id: 'PAX0311', name: 'Ana Romero', action: 'hotel', detail: { flights: [{ flight_id: 'WN1050', dep: '07:05', arr: '08:40' }], hotel: { name: 'Hilton KCI Airport', price: 0, shuttle: 'every 20 min, door 4' } }, why: 'Travelling with Luis; same room' } ],
      message_text: 'Your flight WN1032 to Dallas was cancelled because of the thunderstorms. We have booked you on a new flight; your new trip is in the app and we have texted the details.',
      auto_done: [{ action: 'refund', count: 2, total: 318 }] } },
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
    options: [['Divert to St. Louis', 'Also reachable (leave by 12:12), but St. Louis is filling up with diversions'], ['Keep holding', 'Fuel runs out before the worst-case end of the stop']], optionsArePicks: true, optLabel: 'Divert to Des Moines now',
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
    options: [['Wait for reserve crew', 'Earliest reserve reaches MCI at 16:40: WN1033 leaves about 17:20'], ['Cancel WN1033', '143 Customers stranded overnight, including 12 connections']], optionsArePicks: true, optLabel: 'Swap in crew C35, leave 15:14', connections: 12,
    rule: [true, ['CREW-01', 'CREW-04'], 'C35 stays within duty limits with the 15:14 departure'], whyHuman: 'Crew scheduling approves crew swaps.',
    reject: { last: true, consequence: 'WN1033 will be cancelled: 143 Customers, including Maya Torres, stranded overnight' } },
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


// What each kind of decision changes, and who is told, in the practice session.
const CHANGES = {
  rebook_customers: () => ['Daniel Okafor and Priya Nair on WN1046 today 16:10', 'Luis and Ana Romero on WN1050 tomorrow 07:05, hotel booked'],
  extend_crew_duty: (q) => [`Crew ${q.target.crew_id} may work until 16:05`, `${q.target.flight_id} keeps its slot after the stop`],
  delay_flight: (q) => [`${q.target.flight_id} now leaves 13:43`, 'Gate B57 held until 14:00'],
  assign_reserve_crew: (q) => [`Reserve crew ${q.target.crew_id} on ${q.target.flight_id}`, 'Original crew released to rest'],
  send_mass_message: () => ['Status for 19 flights: ground stop, next update 12:30'],
  move_gate_agents: (q) => [`Gate agent moved for ${q.target.flight_id}`],
  divert_flight: (q) => [`${q.target.flight_id} now landing in Des Moines`, 'New arrival time 12:40 (Des Moines)', 'Onward plan: back to MCI after the stop'],
  pause_ramp_tasks: () => ['Ramp closed: 12 workers inside', 'Fuelling and loading paused'],
  send_correction: (q) => [`${q.target.flight_id} status: diverted to St. Louis`, '158 trips updated'],
  swap_crew: (q) => [`Crew ${q.target.crew_id} on ${q.target.flight_id}`, `${q.target.flight_id} leaves 15:14 from gate B58`, '143 trips updated, including Maya Torres'],
  send_status_update: (q) => [`${q.target.flight_id} passengers: 15:14, gate B58`],
  hold_hotel_rooms: () => ['40 rooms held at 2 partner hotels', 'Shuttle every 20 min from door 4'],
};
const D = {
  app: ['app', 'Passengers: app', 'app', 1], sms: ['sms', 'Passengers: SMS', 'sms', 1], gate: ['gate_screen', 'Gate screens', 'gate_screen', 2],
  cc: ['call_center', 'Call center screens', 'call_center', 2], counter: ['counter', 'Ticket counter agents', 'staff', 3], gates: ['gate_agents', 'Gate agents', 'staff', 3],
  crew: ['crew', 'Crew', 'crew', 3], ramp: ['ramp', 'Ramp crew radios', 'staff', 1], hotel: ['hotel_desk', 'Hotel desks', 'staff', 4], ccagents: ['call_center_agents', 'Call center agents', 'call_center', 3],
};
const DESTS = {
  rebook_customers: ['app', 'sms', 'counter', 'ccagents', 'hotel'],
  extend_crew_duty: ['crew', 'gates', 'counter', 'ccagents'], delay_flight: ['app', 'sms', 'gate', 'cc', 'counter', 'gates', 'crew', 'ccagents'],
  assign_reserve_crew: ['crew', 'gates', 'ccagents'], send_mass_message: ['app', 'sms', 'gate', 'cc', 'counter', 'gates', 'ccagents'],
  move_gate_agents: ['gates'], divert_flight: ['app', 'sms', 'gate', 'cc', 'crew', 'ccagents'], pause_ramp_tasks: ['ramp', 'gates', 'crew'],
  send_correction: ['app', 'sms', 'gate', 'cc', 'counter', 'gates', 'ccagents'], swap_crew: ['crew', 'app', 'sms', 'gate', 'cc', 'counter', 'gates', 'ccagents'],
  send_status_update: ['app', 'sms', 'gate', 'cc', 'counter', 'gates', 'ccagents'], hold_hotel_rooms: ['app', 'sms', 'counter', 'ccagents', 'hotel'],
};

const AGENT_NAMES = { situation: 'Situation', network_recovery: 'Network Recovery', passenger_care: 'Passenger Care', station_staff: 'Station & Staff', communication: 'Communication' };

class PracticeApi {
  constructor() { this.practice = true; this.sessionsList = []; }

  async sessions() { return this.sessionsList.map((s) => ({ run_id: s.run_id, started_at: s.started_at, status: s.status, roles: s.roles, minute: Math.floor(s.minute) })); }

  async start(body) {
    const run_id = `${body.scenario || 'a31_storm'}-s${body.seed || 7}-live-practice-${this.sessionsList.length + 1}`;
    this.s = { run_id, roles: body.roles || [], status: 'running', minute: 660, speed_idle: 5, speed_pending: 1 / 6, started_at: new Date().toISOString(),
      questions: [], nextId: 1, scriptIdx: 0, decisions: [], approvedKeys: new Set(), wrongTotal: 0, todayTotal: 0, beat: performance.now(), lastReal: performance.now(),
      versions: { situation: 1, network_recovery: 96, passenger_care: 200, station_staff: 40, communication: 300 }, lastRun: {}, edges: [],
      notes: {}, reports: [], closedGates: new Set(), rebooked: {}, refunded: new Set(), sentLog: [] };
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
    (s.wrongHist ||= []).push(s.wrongNow); s.wrongAgo = s.wrongHist[Math.max(0, s.wrongHist.length - 11)];
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
    q.status = status; q.answeredMin = m; q.answeredBy = by; q.reason = reason;
    const s = this.s;
    s.decisions.push({ id: q.id, title: q.title, role: ROLE_TITLES[q.role] || q.role, action: q.action, answer: status, reason,
      asked: hm(q.askedMin), answered: hm(m), waited_min: Math.round(m - q.askedMin), by });
    if (status === 'approved' && q.key) s.approvedKeys.add(q.key);
    if (status === 'approved') this._sendFor(q, m);
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
      headline: q.headline || q.title, story: q.trigger.map(([t, , text]) => ({ t, text })),
      recommendation: q.plan,
      options: [{ id: 'opt1', label: q.optLabel || q.title, why: q.plan, recommended: true, effect: q.pax ? `${q.pax} Customers affected` : '' },
        ...q.options.filter(() => q.optionsArePicks).map(([option, why_not], i) => ({ id: `opt${i + 2}`, label: option, why: why_not, recommended: false, effect: '' }))],
      effect: { customers: q.pax, connections: q.connections || 0, flights: q.target.flight_id ? 1 : 0, text: q.pax ? `${q.pax.toLocaleString()} Customers` : '' },
      if_declined: q.lastOption || q.reject.last ? q.reject.consequence : 'The co-pilot comes back with a different plan.',
      bundle: q.askedMin >= 690 ? { id: 'b1', title: 'Ground stop at MCI, 11:30', started: '11:30' } : { id: 'b0', title: 'Normal operations', started: '05:00' },
      section: q.urgent || q.safety ? 'urgent' : 'routine',
      care: q.care || null,
      allowed_changes: q.action === 'delay_flight' ? { delay_min: [83, 100, 130] } : q.action === 'move_gate_agents' ? { gate: ['B53', 'B55', 'B57', 'B60'] } : null,
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
      if (body.action === 'approve' || body.action === 'approve_option') this._answer(q, 'approved', m, q.role, body.option_id && body.option_id !== 'opt1' ? `Chose: ${body.option_id}` : '');
      if (body.action === 'approve_with_change') this._answer(q, 'approved', m, q.role, `With a change: ${Object.entries(body.change || {}).map(([k, v]) => `${k.replace('_', ' ')} ${v}`).join(', ')}`);
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
    const text = q.msg ? q.plan.replace(/^.*?"(.*)".*$/, '$1') : `${q.title}. Same update for everyone.`;
    const dests = (DESTS[q.action] || ['app', 'sms', 'gate', 'cc']).map((k) => D[k]);
    const changes = (CHANGES[q.action] || (() => [q.title]))(q);
    const preview = { changes: changes.map((label) => ({ label })), destinations: dests.map(([key, label, icon]) => ({ key, label, icon })) };
    const final = ['approved', 'rejected'].includes(q.status);
    if (final) {
      const h = add({ id: 'h', kind: 'human', label: q.answeredBy === 'director_of_operations' ? `Duty manager: ${q.status}` : `You: ${q.status}`, t: hm(q.answeredMin), detail: { text: q.reason || '' } });
      edges.push({ from: qq, to: h, label: 'answered', t: hm(q.answeredMin) });
      if (q.status === 'approved') {
        const m = Math.floor(this.s.minute);
        changes.forEach((label, i) => { add({ id: `x${i}`, kind: 'change', label, t: hm(q.answeredMin), detail: { text: label } }); edges.push({ from: h, to: `x${i}`, label: 'changed', t: hm(q.answeredMin) }); });
        const c2 = add({ id: 'c2', kind: 'communication', label: `Communication v${300 + q.id * 3}`, t: hm(q.answeredMin + 1), detail: { text } });
        dests.forEach(([key, label, icon, delay], i) => {
          const at = q.answeredMin + delay;
          const status = m >= at ? 'delivered' : 'waiting';
          add({ id: `d${i}`, kind: ['app', 'sms', 'gate_screen', 'call_center'].includes(key) ? 'channel' : 'staff', label, t: status === 'delivered' ? hm(at) : '', detail: { status, text, key, icon } });
          edges.push({ from: c2, to: `d${i}`, label: status === 'delivered' ? 'confirmed' : 'sent to', t: hm(at) });
        });
      }
    }
    return { question_id: qid, snapshot: final ? 'answered' : 'asked', nodes, edges, preview: final ? null : preview };
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


  // ---- frontline views (practice)
  _flightInfo(fid) {
    const s = this.s, m = Math.floor(s.minute);
    const swapOk = s.approvedKeys.has('swap');
    const F = {
      WN1031: { from: 'MCI', to: 'PHX', sched_dep: '12:05', sched_arr: '13:20', gate: 'B57', crew: { id: 'C31', legal_until: '14:05' }, customers: 143, connections: 12 },
      WN1033: { from: 'MCI', to: 'BWI', sched_dep: '12:40', sched_arr: '16:05', gate: 'B58', crew: { id: swapOk ? 'C35' : 'C33', legal_until: swapOk ? '19:30' : '15:30' }, customers: 143, connections: 12 },
      WN1034: { from: 'MCI', to: 'SAN', sched_dep: '12:20', sched_arr: '13:25', gate: 'B60', crew: { id: 'C34', legal_until: '13:44' }, customers: 137, connections: 9 },
      WN2031: { from: 'DEN', to: 'MCI', sched_dep: '09:55', sched_arr: '11:45', gate: 'B55', crew: { id: 'C35', legal_until: '19:30' }, customers: 158, connections: 41 },
    }[fid] || { from: null, to: null, sched_dep: null, sched_arr: null, gate: null, crew: null, customers: null, connections: null };
    const stop = m >= 690 && m < 840;
    const status = fid === 'WN2031' ? (m < 735 ? 'Holding' : m < 850 ? 'Diverted to St. Louis' : 'Landed') : fid === 'WN1033' && m >= 855 ? (swapOk ? 'Leaves 15:14' : 'Delayed') : stop ? 'Held: ground stop' : 'On time';
    const script = fid === 'WN2031' && m >= 735 && m < 850 ? 'WN2031 from Denver landed in St. Louis because of thunderstorms at Kansas City. It will fly on to Kansas City when the airport reopens, expected around 14:00.'
      : fid === 'WN1033' && swapOk ? 'WN1033 to Baltimore now leaves at 15:14 from gate B58, with a fresh crew. Boarding starts 14:44.'
      : stop ? `Thunderstorms have stopped all departures at Kansas City. We expect to restart around ${m < 780 ? '13:00' : '14:00'}. Next update by ${hm(Math.ceil((m + 1) / 30) * 30)}.` : 'On time.';
    const told = (key) => s.questions.some((q) => q.key === key && q.status === 'approved');
    const lagging = (fid === 'WN2031' && m >= 735 && m < 850 && !told('maya-div')) || (fid === 'WN1033' && swapOk && !s.questions.some((q) => q.maya === 'swapmsg' && q.status === 'approved'));
    const since = hm(fid === 'WN2031' ? 735 : 870);
    const old = fid === 'WN2031' ? 'WN2031 from Denver, landing 13:02' : 'WN1033 delayed, new time to come';
    const one_story = [['app', lagging ? old : script, !lagging], ['sms', lagging ? old : script, !lagging], ['gate_screen', lagging ? old : status, !lagging],
      ['call_center', lagging ? old : script, !lagging], ['counter', stop || swapOk ? (lagging ? old : script) : null, stop || swapOk ? !lagging : null],
      ['gate_agents', stop || swapOk ? (lagging ? old : script) : null, stop || swapOk ? !lagging : null], ['crew', F.crew ? `${F.crew.id}: ${status}` : null, F.crew ? true : null]]
      .map(([place, says, matches]) => ({ place, says, matches, since: matches === false ? since : hm(Math.max(660, m - 3)) }));
    return { id: fid, ...F, truth: script === 'On time.' ? `${fid} is on time.` : script, one_story, est_dep: F.sched_dep, est_arr: F.sched_arr, status, script: { text: script, updated: hm(Math.max(660, m - 3)) },
      notes: (s.notes[fid] || []), on_screens: { app: script, sms: script, gate_screen: status, call_center: script } };
  }

  async outOfDate() {
    const st = await this.state(); const m = Math.floor(this.s.minute);
    const total = st.truth.wrong_now;
    const flights = total ? [
      { flight_id: 'WN2031', label: 'WN2031 from Denver', customers: Math.round(total * 0.5), channels: [{ channel: 'gate_screen', since: '12:15' }, { channel: 'call_center', since: '12:15' }], truth: 'Diverted to St. Louis' },
      { flight_id: 'WN1033', label: 'WN1033 to Baltimore', customers: Math.round(total * 0.3), channels: [{ channel: 'call_center', since: '11:30' }], truth: 'Delayed by the ground stop' },
      { flight_id: 'WN1034', label: 'WN1034 to San Diego', customers: total - Math.round(total * 0.5) - Math.round(total * 0.3), channels: [{ channel: 'app', since: '11:31' }], truth: 'Delayed to 13:43' },
    ].filter((f) => f.customers > 0) : [];
    return { total, ten_min_ago: this.s.wrongAgo ?? total, flights, minute: m };
  }
  _options(cid) {
    const to = 'BWI';
    return [
      { option_id: 'r1', rank: 1, label: 'Best option', flights: [{ flight_id: 'WN1041', from: 'MCI', to, dep: '17:20', arr: '21:05' }], arrives: '21:05', seats_left: 3, why: 'Arrives first tonight, same airport, no extra connection', recommended: true },
      { option_id: 'r2', rank: 2, label: 'Next best', flights: [{ flight_id: 'WN1055', from: 'MCI', to: 'MDW', dep: '18:35', arr: '20:10' }, { flight_id: 'WN2290', from: 'MDW', to, dep: '21:00', arr: '23:40' }], arrives: '23:40', seats_left: 7, why: 'Still tonight, but adds a connection in Chicago', recommended: false },
      { option_id: 'r3', rank: 3, label: 'Third choice', flights: [{ flight_id: 'WN3005', from: 'MCI', to, dep: '06:30', arr: '10:15' }], arrives: 'tomorrow 10:15', seats_left: 22, why: 'Nonstop but overnight: hotel included', recommended: false },
    ];
  }
  async rebookOptions(id, cid) { return this._options(cid); }
  async flightCustomers(id, fid) {
    const s = this.s, base = this._customers().filter((c) => c.flight_id === fid);
    const extra = Array.from({ length: 8 }, (_, i) => ({ id: `PAXF${fid}${i}`, name: ['Ava Brooks', 'Noah Kim', 'Grace Lee', 'Omar Haddad', 'Lena Fischer', 'Ravi Patel', 'Chloe Martin', 'Ethan Wright'][i], booking_ref: `ZZ${i}${fid.slice(-2)}`, flight_id: fid, status: 'Delayed', needs: i === 2 ? ['wheelchair'] : [] }));
    const list = [...base, ...extra];
    return { flight: this._flightInfo(fid), customers: list.map((c, i) => ({ id: c.id, name: c.name, status: s.rebooked[c.id] ? 'Rebooked' : c.status, needs: c.needs, international: i === 5,
      connection: i % 3 === 0 ? { to_flight: 'WN2290', minutes: 45, at_risk: true } : null, best_option: s.rebooked[c.id] ? null : this._options(c.id)[0], rebooked: !!s.rebooked[c.id] })) };
  }
  async flightRebook(id, fid, assignments) {
    const results = [];
    for (const a of assignments) {
      const opt = this._options(a.customer_id).find((o) => o.option_id === a.option_id);
      this.s.rebooked[a.customer_id] = opt;
      const name = (await this.flightCustomers(id, fid)).customers.find((c) => c.id === a.customer_id)?.name || a.customer_id;
      this._send(Math.floor(this.s.minute), name, 'sms', 'rebook', `Your trip has been changed: ${opt.flights.map((f) => `${f.flight_id} at ${f.dep}`).join(' then ')}, arriving ${opt.arrives}.`, 1, a.customer_id);
      results.push({ customer_id: a.customer_id, ok: true, error: null });
    }
    return { results };
  }

  async flight(id, fid) { return this._flightInfo(fid); }
  async addNote(id, fid, body) { const s = this.s; (s.notes[fid] ||= []).push({ t: hm(s.minute), role: body.role, text: body.text }); return s.notes[fid]; }
  async report(id, body) { const s = this.s; s.reports.push({ t: hm(s.minute), ...body }); if (body.kind === 'gate_unavailable' && body.gate) s.closedGates.add(body.gate); return { ok: true }; }
  _customers() {
    const swapOk = this.s.approvedKeys.has('swap');
    const rejectedSwap = this.s.questions.some((q) => q.key === 'swap' && q.lastOption && q.status === 'rejected');
    return [
      { id: 'PAX0001', name: 'Maya Torres', booking_ref: 'QX7K2M', flight_id: 'WN1033', status: rejectedSwap ? 'Needs rebooking' : swapOk ? 'Confirmed, leaves 15:14' : 'Delayed', needs: [] },
      { id: 'PAX0102', name: 'Daniel Okafor', booking_ref: 'LM4P9R', flight_id: 'WN1032', status: 'Flight cancelled', needs: ['wheelchair'] },
      { id: 'PAX0144', name: 'Priya Nair', booking_ref: 'ZT8D3W', flight_id: 'WN1032', status: 'Flight cancelled', needs: [] },
      { id: 'PAX0207', name: 'Sam Whitfield (age 11)', booking_ref: 'UM2B6K', flight_id: 'WN1033', status: 'Delayed', needs: ['unaccompanied_minor'] },
    ];
  }
  async customers(id, q) { const k = String(q || '').toLowerCase(); return this._customers().filter((c) => !k || c.name.toLowerCase().includes(k) || c.booking_ref.toLowerCase().includes(k) || c.flight_id.toLowerCase().includes(k)); }
  async customer(id, cid) {
    const c = this._customers().find((x) => x.id === cid);
    if (!c) { const e = new Error('No such Customer'); e.status = 404; throw e; }
    const s = this.s, m = Math.floor(s.minute);
    const rebooked = s.rebooked[cid];
    const maya = cid === 'PAX0001';
    const trip = rebooked ? rebooked.flights.map((f) => ({ ...f, status: 'Confirmed' }))
      : maya ? [{ flight_id: 'WN2031', from: 'DEN', to: 'MCI', dep: '09:55', arr: '11:45', sched_dep: '09:55', sched_arr: '11:45', est_arr: m < 850 ? '14:10' : '14:10', gate: 'B55', status: m < 735 ? 'Holding' : m < 850 ? 'Diverted to STL' : 'Landed', connection: { to_flight: 'WN1033', minutes: 55, at_risk: true } },
          { flight_id: 'WN1033', from: 'MCI', to: 'BWI', dep: '12:40', arr: '16:05', sched_dep: '12:40', sched_arr: '16:05', est_dep: s.approvedKeys.has('swap') ? '15:14' : 'not known', gate: 'B58', status: c.status, connection: null }]
      : [{ flight_id: c.flight_id, from: 'MCI', to: this._flightInfo(c.flight_id).to, dep: this._flightInfo(c.flight_id).sched_dep, arr: this._flightInfo(c.flight_id).sched_arr, status: c.status }];
    const to = trip[trip.length - 1].to;
    const rebooking = rebooked ? [] : [
      { option_id: 'r1', flights: [{ flight_id: 'WN1041', from: 'MCI', to, dep: '17:20', arr: '21:05' }], arrives: '21:05', seats_left: 3, why: 'Earliest arrival tonight, same airport, no extra connection', recommended: true },
      { option_id: 'r2', flights: [{ flight_id: 'WN1055', from: 'MCI', to: 'MDW', dep: '18:35', arr: '20:10' }, { flight_id: 'WN2290', from: 'MDW', to, dep: '21:00', arr: '23:40' }], arrives: '23:40', seats_left: 7, why: 'Later, and adds a connection in Chicago', recommended: false },
      { option_id: 'r3', flights: [{ flight_id: 'WN3005', from: 'MCI', to, dep: '06:30', arr: '10:15' }], arrives: 'tomorrow 10:15', seats_left: 22, why: 'Nonstop, but overnight: a hotel is booked automatically', recommended: false },
    ];
    const focus = maya ? (await this.state()).focus : null;
    const messages = s.sentLog.filter((x) => x.to === c.name || (x.count > 1 && c.flight_id && x.text.includes(c.flight_id))).map((x) => ({ t: x.t, channel: x.channel, text: x.text }));
    const refunds = s.refunded.has(cid) ? [{ amount: 189, status: 'issued', t: (s.sentLog.find((x) => x.to === c.name && x.kind === 'refund') || {}).t || '' }] : [];
    return { messages, refunds, id: c.id, name: c.name, trip, true_status: rebooked ? `Rebooked on ${rebooked.flights.map((f) => f.flight_id).join(' + ')}` : focus ? focus.true_status : c.status,
      screens: focus ? focus.screens : [], needs: c.needs, rebooking, hotel: rebooked && rebooked.option_id === 'r3' ? { name: 'Hilton KCI Airport', price: 139, shuttle: 'every 20 min, door 4', status: 'booked automatically' } : null, refund_due: s.refunded.has(cid) ? false : c.status.includes('ancel') };
  }
  async rebook(id, cid, body) {
    const c = await this.customer(id, cid);
    const opt = c.rebooking.find((o) => o.option_id === body.option_id) || this._options(cid).find((o) => o.option_id === body.option_id);
    this.s.rebooked[cid] = opt;
    this._send(Math.floor(this.s.minute), c.name, 'sms', 'rebook', `Your trip has been changed. You're now on ${opt.flights.map((f) => `${f.flight_id} at ${f.dep}`).join(' then ')}, arriving ${opt.arrives}.`, 1, cid);
    this.s.decisions.push({ id: 900 + this.s.decisions.length, title: `Rebooked ${c.name} on ${opt.flights.map((f) => f.flight_id).join(' + ')}`, role: 'counter_gate_agent', action: 'rebook', answer: 'approved', reason: '', asked: hm(this.s.minute), answered: hm(this.s.minute), waited_min: 0 });
    return this.customer(id, cid);
  }
  async refund(id, cid) { this.s.refunded.add(cid); const c = this._customers().find((x) => x.id === cid); this._send(Math.floor(this.s.minute), c ? c.name : cid, 'email', 'refund', 'Your refund of $189 has been issued to your original payment. It can take 5 to 7 business days to appear.'); return { ok: true }; }
  async station() {
    const s = this.s, m = Math.floor(s.minute);
    const gates = ['B50', 'B52', 'B53', 'B54', 'B55', 'B56', 'B57', 'B58', 'B59', 'B60'].map((g, i) => {
      if (s.closedGates.has(g)) return { gate: g, status: 'closed', flight_id: null, staffed: false, note: 'Reported unavailable from the floor' };
      const fl = ['WN1027', null, 'WN1028', 'WN1029', 'WN2031', null, 'WN1031', 'WN1033', null, 'WN1034'][i];
      return { gate: g, status: fl ? 'in_use' : 'open', flight_id: fl, staffed: !fl || !(m >= 690 && i % 3 === 0 && !s.decisions.some((d) => d.action === 'move_gate_agents')), note: '' };
    });
    return { ramp: m >= 730 && m < 765 ? { open: false, reason: 'lightning within 5 miles', since: '12:10' } : { open: true, reason: '', since: '' }, gates,
      staffing_gaps: m >= 690 && m < 900 && !s.decisions.some((d) => d.action === 'move_gate_agents') ? [{ role: 'gate_agent', from: '12:00', to: '14:30', short_by: 3 }] : [] };
  }
  async clocks() {
    const s = this.s, m = Math.floor(s.minute);
    return {
      tarmac: m >= 812 && m < 850 ? [{ flight_id: 'WN2031 (at STL)', minutes: m - 812, limit: 180 }] : [],
      crew: [{ crew_id: 'C31', flight_id: 'WN1031', legal_until: '14:05', minutes_left: 845 - m }, { crew_id: 'C34', flight_id: 'WN1034', legal_until: '13:44', minutes_left: 824 - m },
        { crew_id: 'C33', flight_id: 'WN1033', legal_until: '15:30', minutes_left: 930 - m }].filter((c) => c.minutes_left > -30),
      refunds: [...s.refunded].map((cid) => ({ customer_id: cid, flight_id: '-', due_by: '-', status: 'auto_issued' })),
      special_assistance: m >= 690 ? [{ customer_id: 'Daniel Okafor', need: 'wheelchair', flight_id: 'WN1031', waiting_min: m - 690 }, { customer_id: 'Sam Whitfield (11)', need: 'unaccompanied_minor', flight_id: 'WN1033', waiting_min: m - 700 }] : [],
    };
  }
  async handover(id, since) {
    const s = this.s, m = Math.floor(s.minute);
    const open = s.questions.filter((q) => ['waiting', 'snoozed', 'escalated'].includes(q.status));
    const watch = [];
    if (m >= 690 && m < 840) watch.push(`Ground stop still on; likely end ${m < 780 ? '13:00' : '14:00'}.`);
    if (m < 855) watch.push('WN1033 to Baltimore: flight attendants run out of duty time at 15:30.');
    if (s.closedGates.size) watch.push(`Gates closed from the floor: ${[...s.closedGates].join(', ')}.`);
    const n = s.decisions.length;
    return { since, decisions: n, open: open.map((q) => ({ id: q.id, title: q.headline || q.title })), watch, auto_done: s.refunded.size,
      text: `Since ${since}: ${n} decisions made, ${open.length} still open. ${watch.join(' ')}` };
  }


  _send(m, to, channel, kind, text, count = 1, customer_id = null) { this.s.sentLog.push({ t: hm(m + 1), minute: m + 1, to, channel, kind, text, count, customer_id }); }
  _sendFor(q, m) {
    if (q.msg) this._send(m, `${q.pax.toLocaleString()} Customers`, 'app + text', 'message', q.plan.includes('"') ? q.plan.replace(/^.*?"(.*)".*$/, '$1') : q.plan, q.pax);
    if (q.key === 'swap') { this._send(m, 'Crew C35', 'crew', 'message', 'You are assigned to WN1033 MCI to BWI, departing 15:14 from gate B58. Report by 14:30.'); this._send(m, 'Gate agents', 'staff_note', 'message', 'WN1033 now leaves 15:14 from B58 with crew C35. Boarding 14:44.'); }
    if (q.care) {
      for (const c of q.care.customers) this._send(m, c.name, 'text', c.action === 'hotel' ? 'hotel' : 'rebook', c.action === 'hotel'
        ? `Your new flight is ${c.detail.flights[0].flight_id} tomorrow at ${c.detail.flights[0].dep}. Tonight you're booked at the ${c.detail.hotel.name}; shuttle ${c.detail.hotel.shuttle}.`
        : `You're now on ${c.detail.flights[0].flight_id} to Dallas today at ${c.detail.flights[0].dep}, arriving ${c.detail.flights[0].arr}.`);
      for (const a of q.care.auto_done || []) this._send(m, `${a.count} Customers`, 'email', 'refund', `Refunds issued automatically for unused fares: $${a.total} in total.`, a.count);
    }
  }
  async sent(id, since) { return this.s.sentLog.filter((x) => x.minute > since); }

  async history() { return { decisions: [...this.s.decisions].reverse() }; }

  async results() {
    const s = this.s;
    const count = (a) => s.decisions.filter((d) => d.answer === a).length;
    const waits = s.decisions.filter((d) => d.answer === 'approved' || d.answer === 'rejected').map((d) => d.waited_min).sort((a, b) => a - b);
    const swapOk = s.approvedKeys.has('swap');
    return {
      you: { wrong_screen_passenger_minutes: Math.round(s.wrongTotal), cancelled: swapOk ? 3 : 4, stranded_overnight: swapOk ? 290 : 433, focus_final_trip: this._maya(Math.floor(s.minute)).true_status },
      today: { wrong_screen_passenger_minutes: Math.round(s.todayTotal), cancelled: 5, stranded_overnight: 559, focus_final_trip: 'WN1033 cancelled. Rebooked WN3005, 06:30 tomorrow' },
      orchestrated: { wrong_screen_passenger_minutes: 6080, cancelled: 3, stranded_overnight: 290, focus_final_trip: 'WN1033 to Baltimore left 15:14' },
      decisions: { approved: count('approved'), rejected: count('rejected'), waited: count('waited'), handed_off: count('handed off'),
        escalated: s.decisions.filter((d) => d.by === 'director_of_operations').length, median_answer_min: waits.length ? waits[Math.floor(waits.length / 2)] : 0 },
    };
  }
}
const toMin = (s) => { const [h, m] = String(s).split(':').map(Number); return h * 60 + m; };
