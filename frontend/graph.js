// Draws a decision's trace (or any small flow graph) as an SVG flowchart: left to right on wide screens,
// top to bottom on narrow ones. Columns follow the path information takes through the co-pilot.
import { ICONS } from './icons.js';

const RANK = { feed: 0, situation: 1, network_recovery: 2, passenger_care: 2, station_staff: 2, communication: 3, rule_check: 4, orchestrator: 5, question: 6, human: 7 };
const KIND_ICON = { situation: 'situation', network_recovery: 'network_recovery', passenger_care: 'passenger_care', station_staff: 'station_staff',
  communication: 'communication', orchestrator: 'orchestrator' };
const EXTRA = {
  feed: '<path d="M4 4h16v3H4zm0 6.5h16v3H4zM4 17h10v3H4z"/>',
  rule_check: '<path d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5zm-1.3 14.2-4-4 1.4-1.4 2.6 2.6 5.6-5.6 1.4 1.4z"/>',
  question: '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 17h-2v-2h2zm2.1-7.7-.9.9C13.4 13 13 13.5 13 15h-2v-.5c0-1.1.4-2.1 1.2-2.8l1.2-1.3A2 2 0 1 0 10 9H8a4 4 0 1 1 7.1 2.3z"/>',
  human: '<path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 2c-2.7 0-8 1.3-8 4v2h16v-2c0-2.7-5.3-4-8-4z"/>',
};
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function wrap(text, max) {
  const words = String(text).split(/\s+/), lines = [];
  let line = '';
  for (const w of words) {
    if ((line + ' ' + w).trim().length > max && line) { lines.push(line); line = w; } else line = (line + ' ' + w).trim();
  }
  if (line) lines.push(line);
  if (lines.length > 2) { lines.length = 2; lines[1] = lines[1].replace(/.{0,2}$/, '') + '…'; }
  return lines;
}

export function drawTrace(container, graph, { onNode } = {}) {
  const vertical = container.clientWidth < 720;
  const NW = 160, NH = 64, GX = vertical ? 30 : 46, GY = 18;
  const cols = {};
  for (const n of graph.nodes) (cols[RANK[n.kind] ?? 3] ||= []).push(n);
  const ranks = Object.keys(cols).map(Number).sort((a, b) => a - b);
  const pos = {};
  const maxInCol = Math.max(...ranks.map((r) => cols[r].length));
  ranks.forEach((r, ci) => {
    const list = cols[r];
    list.forEach((n, i) => {
      const off = (maxInCol - list.length) / 2;
      pos[n.id] = vertical
        ? { x: (i + off) * (NW + GY), y: ci * (NH + GX) }
        : { x: ci * (NW + GX), y: (i + off) * (NH + GY) };
    });
  });
  const W = vertical ? maxInCol * (NW + GY) - GY : ranks.length * (NW + GX) - GX;
  const H = vertical ? ranks.length * (NH + GX) - GX : maxInCol * (NH + GY) - GY;
  const pad = 8;
  let edges = '';
  for (const e of graph.edges) {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) continue;
    let d, lx, ly;
    if (vertical) {
      const x1 = a.x + NW / 2, y1 = a.y + NH, x2 = b.x + NW / 2, y2 = b.y;
      d = `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2 - 4}`; lx = (x1 + x2) / 2 + 6; ly = (y1 + y2) / 2;
    } else {
      const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
      d = `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2 - 4},${y2}`; lx = (x1 + x2) / 2; ly = (y1 + y2) / 2 - 6;
    }
    edges += `<path class="g-edge" d="${d}" marker-end="url(#arrow)"/><text class="g-elabel" x="${lx}" y="${ly}" text-anchor="${vertical ? 'start' : 'middle'}">${esc(e.label)}</text>`;
  }
  let nodes = '';
  for (const n of graph.nodes) {
    const p = pos[n.id];
    const icon = KIND_ICON[n.kind] ? ICONS[KIND_ICON[n.kind]] : EXTRA[n.kind] || '';
    const lines = wrap(n.label, 17);
    nodes += `<g class="g-node k-${n.kind}" transform="translate(${p.x},${p.y})" tabindex="0" role="button" data-id="${esc(n.id)}" aria-label="${esc(n.label)} at ${esc(n.t)}">
      <rect width="${NW}" height="${NH}" rx="10"/>
      <circle cx="22" cy="${NH / 2}" r="14" class="g-dot"/>
      <svg x="12" y="${NH / 2 - 10}" width="20" height="20" viewBox="0 0 24 24" class="g-ico">${icon}</svg>
      ${lines.map((l, i) => `<text x="44" y="${NH / 2 - (lines.length - 1) * 8 + i * 16 + 1}" class="g-label">${esc(l)}</text>`).join('')}
      <text x="${NW - 8}" y="14" class="g-time" text-anchor="end">${esc(n.t || '')}</text>
    </g>`;
  }
  const fit = Math.min(1, (container.clientWidth - 20) / (W + pad * 2));
  const scale = fit >= 0.9 ? fit : 1;            // shrink a little to fit; beyond that, scroll sideways instead
  container.innerHTML = `<svg class="graph" viewBox="${-pad} ${-pad} ${W + pad * 2} ${H + pad * 2}" width="${(W + pad * 2) * scale}" height="${(H + pad * 2) * scale}" role="img" aria-label="How this decision reached you">
    <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="g-arrow"/></marker></defs>
    ${edges}${nodes}</svg>`;
  if (onNode) {
    container.querySelectorAll('.g-node').forEach((g) => {
      const fire = () => onNode(graph.nodes.find((n) => n.id === g.dataset.id));
      g.addEventListener('click', fire);
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); } });
    });
  }
}

// ------------------------------------------------------------------ one decision, start to finish
// Row 1: what happened -> every agent's input -> the shared state -> rule check and orchestrator -> you.
// Row 2: what your answer changed -> one truth -> sent to every screen and every person at once.
const AGENT_ORDER = ['situation', 'network_recovery', 'passenger_care', 'station_staff', 'communication'];
const DEST_ICON = {
  app: '<path d="M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm0 3v13h10V5zm5 15.5a1 1 0 1 0 0-2 1 1 0 0 0 0 2z"/>',
  sms: ICONS.communication,
  gate_screen: '<path d="M3 4h18a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1h-7v2h3v2H7v-2h3v-2H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zm2 3v2h9V7zm0 4v2h6v-2z"/>',
  call_center: '<path d="M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25 11.4 11.4 0 0 0 3.6.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.25.2 2.45.57 3.57a1 1 0 0 1-.25 1z"/>',
  staff: ICONS.passenger_care,
  crew: ICONS.network_recovery,
};

export function drawDecisionFlow(container, m, { onNode } = {}) {
  const W = 900, AH = 38, AG = 8;
  const H1 = AGENT_ORDER.length * (AH + AG) - AG;
  const R2 = H1 + 86;
  const DS = 42;
  const H2 = Math.max(m.dests.length * DS - 8, Math.min(4, m.changes.length) * 58 - 10, 150);
  const H = R2 + H2;
  const info = [];
  const box = (x, y, w, h, cls, iconSvg, lines, sub, data) => {
    const i = info.push(data) - 1;
    const tx = iconSvg ? 40 : 12;
    const ly = sub ? h / 2 - 3 - (lines.length - 1) * 8 : h / 2 + 5 - (lines.length - 1) * 8;
    return `<g class="df ${cls}" transform="translate(${x},${y})" tabindex="0" role="button" data-i="${i}" aria-label="${esc(lines.join(' '))}">
      <rect width="${w}" height="${h}" rx="9"/>
      ${iconSvg ? `<svg x="11" y="${h / 2 - 10}" width="20" height="20" viewBox="0 0 24 24" class="df-ico">${iconSvg}</svg>` : ''}
      ${lines.map((l, k) => `<text x="${tx}" y="${ly + k * 16}" class="df-label">${esc(l)}</text>`).join('')}
      ${sub ? `<text x="${tx}" y="${ly + (lines.length - 1) * 16 + 16}" class="df-sub">${esc(sub)}</text>` : ''}
    </g>`;
  };
  const curve = (x1, y1, x2, y2, cls = '') => `<path class="df-edge ${cls}" d="M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2 - 3},${y2}" marker-end="url(#dfa)"/>`;
  let g = '', e = '';

  // column headings
  const heads = [[0, 'What happened'], [180, "The co-pilot's agents"], [404, 'Shared picture'], [530, 'Safety rules'], [730, 'You']];
  const heads2 = [[0, m.preview ? 'What changes if you approve' : 'What changed'], [300, 'One update'], [470, m.preview ? 'Who will be told, at the same time' : 'Everyone told, at the same time']];
  for (const [x, t] of heads) g += `<text x="${x}" y="-12" class="df-head">${esc(t)}</text>`;
  for (const [x, t] of heads2) g += `<text x="${x}" y="${R2 - 12}" class="df-head">${esc(t)}</text>`;

  // what happened
  const feeds = m.feeds.slice(0, 4);
  const fh = 54, fstep = feeds.length ? Math.min(fh + 10, H1 / feeds.length) : 0;
  const f0 = (H1 - (feeds.length * fstep - (fstep - fh))) / 2;
  feeds.forEach((f, k) => {
    const y = f0 + k * fstep;
    g += box(0, y, 150, fh, 'feed', EXTRA.feed, wrap(f.label, 15), f.t, f);
    e += curve(150, y + fh / 2, 174, y + fh / 2);
  });

  // agents
  m.agents.forEach((a, k) => {
    const y = k * (AH + AG);
    g += box(176, y, 200, AH, a.involved ? 'agent on' : 'agent off', ICONS[a.key], [a.name], null, a);
    if (a.involved) g += `<text x="${176 + 194}" y="${y + 24}" class="df-ver" text-anchor="end">${esc(a.short || '')}</text>`;
    e += curve(376, y + AH / 2, 404, H1 / 2, a.involved ? 'on' : 'off');
  });

  // shared state
  g += box(404, 0, 96, H1, 'shared', null, ['Shared', 'picture'], 'one truth', { label: 'Shared state', text: 'Every agent writes versioned facts and plans here and reads the others\' from here. Nobody passes notes on the side.' });
  e += curve(500, H1 / 2, 530, H1 / 2 - 34);

  // rule check and orchestrator
  g += box(530, H1 / 2 - 60, 160, 50, `rule ${m.rule.ok ? 'ok' : 'bad'}`, EXTRA.rule_check, [m.rule.ok ? 'Safety rules met' : 'Blocked by a rule'], '', m.rule);
  g += box(530, H1 / 2 + 10, 160, 50, 'orch', ICONS.orchestrator, ['Orchestrator'], 'sends it to you', m.orch);
  e += `<path class="df-edge" d="M610,${H1 / 2 - 10} L610,${H1 / 2 + 7}" marker-end="url(#dfa)"/>`;
  e += curve(690, H1 / 2 + 35, 730, H1 / 2);

  // you
  const youCls = { approved: 'ok', rejected: 'bad', escalated: 'warn', 'handed off': 'warn', waiting: 'wait', snoozed: 'wait' }[m.you.state] || 'wait';
  g += box(730, H1 / 2 - 44, 170, 88, `you ${youCls}`, EXTRA.human, wrap(m.you.label, 15), m.you.sub, m.you);

  // connector from you to row 2
  const ccls = m.preview ? 'preview' : 'on';
  e += `<path class="df-edge ${ccls}" d="M815,${H1 / 2 + 44} L815,${H1 + 40} L190,${H1 + 40} L190,${R2 - 3}" marker-end="url(#dfa)"/>`;

  // what changed
  if (m.rejected) {
    g += box(0, R2, 260, 70, 'replan', ICONS[m.rejected.agent] || ICONS.orchestrator, wrap(m.rejected.label, 30), m.rejected.sub, m.rejected);
  } else {
    m.changes.slice(0, 4).forEach((c, k) => {
      const lines = wrap(c.label, 33);
      const sub = lines.length === 1 ? (m.preview ? 'if approved' : c.t || '') : null;
      g += box(0, R2 + k * 58, 260, 48, `change ${m.preview ? 'preview' : 'on'}`, null, lines, sub, c);
      e += curve(260, R2 + k * 58 + 24, 300, R2 + H2 / 2, m.preview ? 'preview' : 'on');
    });
    // one truth hub
    g += box(300, R2, 120, H2, `hub ${m.preview ? 'preview' : 'on'}`, ICONS.communication, ['One update'], m.hub.sub, m.hub);
    // destinations
    if (!m.dests.length) {
      g += `<text x="470" y="${R2 + 22}" class="df-sub">${m.preview ? 'Who is told shows up here once you answer.' : 'No one needed telling, or the messages are still being written.'}</text>`;
    }
    if (!m.changes.length) g += `<text x="0" y="${R2 + 22}" class="df-sub">${m.preview ? 'The agent did not say in advance what will change.' : 'No change recorded yet.'}</text>`;
    m.dests.forEach((d, k) => {
      const x = 470, y = R2 + k * DS;
      const st = m.preview ? 'preview' : d.status;
      const mark = st === 'delivered' ? `✓ ${d.t || ''}` : st === 'failed' ? 'failed, retrying' : st === 'waiting' ? 'sending…' : 'will be told';
      g += box(x, y, 300, DS - 8, `dest ${st}`, DEST_ICON[d.icon] || DEST_ICON.staff, [d.label], null, d);
      g += `<text x="${x + 290}" y="${y + (DS - 8) / 2 + 4}" class="df-mark ${st}" text-anchor="end">${esc(mark)}</text>`;
      e += `<path class="df-edge ${st === 'waiting' ? 'sending' : st === 'preview' ? 'preview' : st === 'delivered' ? 'on' : 'bad'}" d="M420,${R2 + H2 / 2} C445,${R2 + H2 / 2} 445,${y + (DS - 8) / 2} ${x - 3},${y + (DS - 8) / 2}" marker-end="url(#dfa)"/>`;
    });
  }

  container.innerHTML = `<svg class="dflow" viewBox="-6 -30 ${W + 12} ${H + 40}" role="img" aria-label="How this decision came about and where it goes">
    <defs><marker id="dfa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" class="g-arrow"/></marker></defs>
    ${e}${g}</svg>`;
  if (onNode) {
    container.querySelectorAll('.df[data-i]').forEach((el) => {
      const fire = () => onNode(info[Number(el.dataset.i)]);
      el.addEventListener('click', fire);
      el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fire(); } });
    });
  }
}
