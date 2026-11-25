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
