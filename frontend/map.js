// A tilted, simulated 3D map of today's network around Kansas City, drawn on a canvas.
// No map tiles: the land is a dot field inside a simplified outline of the lower 48 states.

const MCI = { lat: 39.30, lon: -94.71 };
const COS_LAT = Math.cos(MCI.lat * Math.PI / 180);

// Simplified outline of the contiguous US (lat, lon). Stylised, not survey-accurate.
const US = [
  [48.4,-124.7],[49.0,-123.0],[49.0,-95.2],[48.0,-89.6],[46.8,-84.6],[45.8,-83.4],[43.1,-82.4],[42.0,-83.1],[41.7,-82.0],
  [42.3,-79.8],[43.3,-79.0],[43.6,-76.2],[45.0,-74.7],[45.0,-71.5],[47.4,-69.2],[47.0,-67.8],[44.8,-66.9],[43.6,-70.2],
  [42.6,-70.7],[41.7,-70.0],[41.3,-71.9],[40.6,-73.9],[39.4,-74.4],[38.9,-74.9],[38.3,-75.1],[37.0,-76.0],[35.9,-75.6],
  [35.2,-75.5],[33.9,-78.0],[32.0,-80.9],[30.4,-81.4],[28.5,-80.6],[26.8,-80.0],[25.2,-80.4],[25.3,-81.1],[26.6,-82.0],
  [27.9,-82.8],[29.1,-83.1],[30.0,-84.3],[29.7,-85.4],[30.4,-87.2],[30.3,-88.5],[29.2,-89.3],[29.5,-90.5],[29.6,-92.3],
  [29.7,-93.8],[28.8,-95.3],[27.8,-97.3],[26.0,-97.2],[26.4,-99.1],[27.9,-99.9],[29.8,-101.4],[29.0,-103.1],[29.8,-104.6],
  [31.8,-106.5],[31.3,-108.2],[31.3,-111.0],[32.5,-114.8],[32.5,-117.1],[33.7,-118.3],[34.0,-118.8],[34.5,-120.6],[35.6,-121.3],
  [36.6,-121.9],[37.8,-122.5],[38.3,-123.0],[40.4,-124.4],[42.0,-124.3],[43.6,-124.2],[46.2,-124.0],[47.9,-124.6],[48.4,-124.7],
];

function inside(lat, lon, poly) {
  let ok = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [yi, xi] = poly[i], [yj, xj] = poly[j];
    if ((yi > lat) !== (yj > lat) && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) ok = !ok;
  }
  return ok;
}

export function toWorld(lat, lon) {
  return { x: (lon - MCI.lon) * COS_LAT, z: -(lat - MCI.lat) };
}

const LAND = (() => {
  const pts = [];
  for (let lat = 24.6; lat <= 49.2; lat += 0.62) {
    for (let lon = -125; lon <= -66.5; lon += 0.62 / COS_LAT) {
      if (inside(lat, lon, US)) pts.push(toWorld(lat, lon));
    }
  }
  return pts;
})();

const TAU = Math.PI * 2;

export class NetworkMap {
  constructor(canvas, tooltip) {
    this.canvas = canvas;
    this.tooltip = tooltip;
    this.ctx = canvas.getContext('2d');
    this.yaw = -0.10;
    this.tilt = 0.98;       // camera elevation in radians (pi/2 = straight down)
    this.dist = 62;
    this.airports = [];
    this.routes = [];
    this.planes = [];
    this.labels = new Set();
    this.colors = {};
    this.hover = null;
    this.selection = null;  // { id, legs: [{from, to, p}], planned: {from, to} | null }
    this.weather = null;    // { cell, wind, lightning, alert }
    this.onSelect = null;
    this.reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this._bind();
    this.resize();
    new ResizeObserver(() => this.resize()).observe(canvas);
  }

  readColors() {
    const css = getComputedStyle(document.documentElement);
    const v = (n) => css.getPropertyValue(n).trim();
    this.colors = { bg: v('--map-bg'), dot: v('--map-dot'), route: v('--map-route'), out: v('--map-out'), inn: v('--map-in'),
      label: v('--map-label'), ink: v('--ink'), surface: v('--surface'), accent: v('--accent'), warn: v('--warn'), bad: v('--bad'),
      wx1: v('--wx-1'), wx2: v('--wx-2'), wx3: v('--wx-3'), wind: v('--wx-wind'), bolt: v('--wx-bolt') };
  }

  resize() {
    const r = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.w = Math.max(10, r.width); this.h = Math.max(10, r.height);
    this.canvas.width = Math.round(this.w * dpr); this.canvas.height = Math.round(this.h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.focal = Math.min(this.w * 1.32, this.h * 2.1);
  }

  setNetwork(airports, routes) {
    this.airports = airports.map((a) => ({ ...a, ...toWorld(a.lat, a.lon) }));
    this.byCode = Object.fromEntries(this.airports.map((a) => [a.code, a]));
    this.routes = routes;
  }

  setPlanes(planes, labels) { this.planes = planes; this.labels = labels; }
  setSelection(sel) { this.selection = sel; }
  setWeather(wx) { this.weather = wx; }

  project(x, y, z) {
    const cy = Math.cos(this.yaw), sy = Math.sin(this.yaw);
    const x1 = x * cy - z * sy, z1 = x * sy + z * cy;
    const ca = Math.cos(this.tilt), sa = Math.sin(this.tilt);
    const ys = y * ca - (z1 + 3) * sa;      // +3 nudges the view so the US sits centred
    const zs = this.dist - y * sa - (z1 + 3) * ca;
    if (zs < 1) return null;
    return { x: this.w / 2 + this.focal * x1 / zs, y: this.h * 0.36 - this.focal * ys / zs, s: this.focal / zs };
  }

  arcPoint(a, b, t) {
    const d = Math.hypot(b.x - a.x, b.z - a.z);
    return { x: a.x + (b.x - a.x) * t, y: Math.sin(Math.PI * t) * d * 0.16, z: a.z + (b.z - a.z) * t };
  }

  _arcPath(a, b, t0, t1, steps = 48) {
    const { ctx } = this;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i <= steps; i++) {
      const q = this.arcPoint(a, b, t0 + (t1 - t0) * i / steps), s = this.project(q.x, q.y, q.z);
      if (!s) continue;
      started ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y);
      started = true;
    }
  }

  _groundShape(cx, cz, radius, wobble, phase) {
    const { ctx } = this;
    ctx.beginPath();
    for (let i = 0; i <= 64; i++) {
      const a = i / 64 * TAU;
      const r = radius * (1 + wobble * (0.55 * Math.sin(3 * a + phase) + 0.3 * Math.sin(5 * a - phase * 1.3) + 0.15 * Math.sin(8 * a + 2)));
      const s = this.project(cx + Math.cos(a) * r * 1.25, 0, cz + Math.sin(a) * r * 0.85);
      if (!s) continue;
      i ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y);
    }
    ctx.closePath();
  }

  // Where a plane is in world space, and a point a little ahead of it for its heading.
  _planeAt(p, now) {
    if (p.kind === 'orbit') {
      const a = p.orbit.ang, r = p.orbit.r;
      const pos = { x: Math.cos(a) * r * 1.3, y: p.orbit.alt, z: Math.sin(a) * r };
      const ahead = { x: Math.cos(a + 0.15) * r * 1.3, y: p.orbit.alt, z: Math.sin(a + 0.15) * r };
      return [pos, ahead];
    }
    const a = this.byCode[p.from], b = this.byCode[p.to];
    if (!a || !b) return null;
    if (p.kind === 'ground') {      // parked at `from`, nose towards `to`
      const ahead = this.arcPoint(a, b, 0.03);
      return [{ x: a.x, y: 0, z: a.z }, { x: ahead.x, y: 0, z: ahead.z }];
    }
    return [this.arcPoint(a, b, p.progress), this.arcPoint(a, b, Math.min(1, p.progress + 0.02))];
  }

  _drawWeather(now) {
    const wx = this.weather;
    if (!wx) return;
    const { ctx, colors } = this;
    const spin = this.reduceMotion ? 0 : now / 9000;

    // wind streaks around Kansas City, moving downwind
    if (wx.wind) {
      const to = (wx.wind.dir + 180) * Math.PI / 180;
      const dx = Math.sin(to), dz = -Math.cos(to);
      const len = 0.35 + wx.wind.speed / 40;
      const drift = this.reduceMotion ? 0.5 : ((now / 1000) * (0.25 + wx.wind.speed / 60)) % 1;
      ctx.strokeStyle = colors.wind; ctx.lineWidth = 1.3; ctx.lineCap = 'round';
      for (let gx = -6; gx <= 6; gx += 1.5) {
        for (let gz = -4.5; gz <= 4.5; gz += 1.5) {
          const jitter = Math.sin(gx * 12.9 + gz * 78.2) * 0.5;
          const fade = Math.max(0, 1 - Math.hypot(gx, gz) / 7);
          if (fade <= 0) continue;
          const k = (drift + (jitter + 1) / 2) % 1;
          const ox = gx + jitter + dx * k * 1.5, oz = gz - jitter * 0.6 + dz * k * 1.5;
          const a = this.project(ox, 0.05, oz), b = this.project(ox + dx * len, 0.05, oz + dz * len);
          if (!a || !b) continue;
          ctx.globalAlpha = fade * Math.sin(Math.PI * k) * 0.9;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
          const ang = Math.atan2(b.y - a.y, b.x - a.x);
          ctx.beginPath(); ctx.moveTo(b.x, b.y);
          ctx.lineTo(b.x - 4 * Math.cos(ang - 0.5), b.y - 4 * Math.sin(ang - 0.5));
          ctx.moveTo(b.x, b.y);
          ctx.lineTo(b.x - 4 * Math.cos(ang + 0.5), b.y - 4 * Math.sin(ang + 0.5));
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
    }

    // the storm cell: radar-style layers, heavier towards the core
    const c = wx.cell;
    if (c && c.show > 0.01) {
      const w = toWorld(c.lat, c.lon);
      ctx.globalAlpha = c.show;
      const layers = [[3.1, colors.wx1, 0.35], [1.95, colors.wx2, 0.3], [1.0 * c.core, colors.wx3, 0.25]];
      for (const [i, [r, col, wob]] of layers.entries()) {
        ctx.fillStyle = col;
        this._groundShape(w.x + i * 0.18, w.z - i * 0.1, r, wob, spin + i);
        ctx.fill();
      }
      // trailing cells to the south-west
      ctx.fillStyle = colors.wx1;
      this._groundShape(w.x - 4.2, w.z + 2.2, 1.2, 0.4, spin * 1.3 + 4); ctx.fill();
      ctx.fillStyle = colors.wx2;
      this._groundShape(w.x - 4.1, w.z + 2.15, 0.55, 0.4, spin * 1.3 + 5); ctx.fill();
      ctx.globalAlpha = 1;

      if (wx.lightning) {
        // ring around MCI while the ramp is closed for lightning
        const ring = 0.75;
        ctx.strokeStyle = colors.bad; ctx.lineWidth = 2; ctx.setLineDash([6, 5]);
        ctx.beginPath();
        for (let i = 0; i <= 64; i++) {
          const a = i / 64 * TAU, s = this.project(Math.cos(a) * ring * 1.3, 0, Math.sin(a) * ring);
          if (s) (i ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y));
        }
        ctx.stroke(); ctx.setLineDash([]);
        // strikes, changing every fraction of a second
        const seed = this.reduceMotion ? 3 : Math.floor(now / 260);
        const rnd = (n) => { const v = Math.sin(seed * 91.7 + n * 12.3) * 43758.5; return v - Math.floor(v); };
        if (this.reduceMotion || rnd(0) > 0.35) {
          for (let k = 0; k < 2; k++) {
            const bx = w.x + (rnd(k + 1) - 0.5) * 2.2, bz = w.z + (rnd(k + 5) - 0.5) * 1.5;
            ctx.strokeStyle = colors.bolt; ctx.lineWidth = 2; ctx.beginPath();
            let px = bx, pz = bz;
            for (let j = 0; j <= 5; j++) {
              const y = 1.6 - j * 0.32;
              const s = this.project(px, y, pz);
              if (s) (j ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y));
              px += (rnd(k * 10 + j) - 0.5) * 0.25; pz += (rnd(k * 20 + j) - 0.5) * 0.15;
            }
            ctx.stroke();
          }
        }
      }
    }
  }

  draw(now) {
    const { ctx, colors } = this;
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.fillStyle = colors.bg; ctx.fillRect(0, 0, this.w, this.h);

    ctx.fillStyle = colors.dot;
    for (const p of LAND) {
      const s = this.project(p.x, 0, p.z);
      if (!s) continue;
      const r = Math.max(0.6, s.s * 0.085);
      ctx.fillRect(s.x - r, s.y - r, r * 2, r * 2);
    }

    this._drawWeather(now);

    const sel = this.selection;
    ctx.strokeStyle = colors.route; ctx.lineWidth = 1;
    ctx.globalAlpha = sel ? 0.5 : 1;
    for (const [from, to] of this.routes) {
      const a = this.byCode[from], b = this.byCode[to];
      if (!a || !b) continue;
      this._arcPath(a, b, 0, 1, 40); ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // the selected flight's path: flown part solid, the rest dashed
    const selCodes = new Set();
    if (sel) {
      const col = sel.tone === 'warn' ? colors.warn : sel.dir === 'out' ? colors.out : colors.inn;
      if (sel.planned) {
        const a = this.byCode[sel.planned.from], b = this.byCode[sel.planned.to];
        if (a && b) {
          ctx.strokeStyle = col; ctx.globalAlpha = 0.45; ctx.lineWidth = 1.5; ctx.setLineDash([3, 5]);
          this._arcPath(a, b, 0, 1); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
          selCodes.add(a.code); selCodes.add(b.code);
        }
      }
      for (const leg of sel.legs) {
        const a = this.byCode[leg.from], b = this.byCode[leg.to];
        if (!a || !b) continue;
        selCodes.add(a.code); selCodes.add(b.code);
        ctx.strokeStyle = col;
        if (leg.p > 0) { ctx.lineWidth = 3.5; this._arcPath(a, b, 0, leg.p); ctx.stroke(); }
        if (leg.p < 1) { ctx.lineWidth = 2; ctx.setLineDash([7, 6]); this._arcPath(a, b, leg.p, 1); ctx.stroke(); ctx.setLineDash([]); }
      }
    }

    for (const a of this.airports) {
      const s = this.project(a.x, 0, a.z);
      if (!s) continue;
      const isMci = a.code === 'MCI';
      const hot = selCodes.has(a.code);
      const alert = isMci && this.weather && this.weather.alert;
      ctx.fillStyle = isMci ? (alert ? colors.bad : colors.accent) : hot ? colors.ink : colors.label;
      ctx.beginPath(); ctx.arc(s.x, s.y, isMci ? 5 : hot ? 4 : 2.4, 0, TAU); ctx.fill();
      if (isMci) {
        const pulse = this.reduceMotion ? 0.5 : (now / (alert ? 1000 : 1600)) % 1;
        ctx.strokeStyle = alert ? colors.bad : colors.accent; ctx.globalAlpha = 1 - pulse; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.ellipse(s.x, s.y, 8 + pulse * 26, (8 + pulse * 26) * Math.cos(this.tilt) * 0.9 + 2, 0, 0, TAU); ctx.stroke();
        ctx.globalAlpha = 1;
      }
      if (isMci || a.base || hot || this.labels.has(a.code) || (this.hover && this.hover.codes.includes(a.code))) {
        ctx.fillStyle = colors.ink;
        ctx.font = `${isMci || hot ? 700 : 400} ${isMci ? 13 : hot ? 13 : 11}px 'Atkinson Hyperlegible', system-ui, sans-serif`;
        ctx.fillText(a.code, s.x + 7, s.y - 6);
      }
    }

    // holding orbits, drawn under the planes
    const orbits = new Map();
    for (const p of this.planes) if (p.kind === 'orbit') orbits.set(`${p.orbit.r}|${p.orbit.alt}`, p.orbit);
    ctx.strokeStyle = colors.warn; ctx.lineWidth = 1; ctx.globalAlpha = 0.55; ctx.setLineDash([2, 4]);
    for (const o of orbits.values()) {
      ctx.beginPath();
      for (let i = 0; i <= 48; i++) {
        const a = i / 48 * TAU, s = this.project(Math.cos(a) * o.r * 1.3, o.alt, Math.sin(a) * o.r);
        if (s) (i ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y));
      }
      ctx.stroke();
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;

    this.hits = [];
    const drawPlane = (p, emphasised) => {
      const at = this._planeAt(p, now);
      if (!at) return;
      const s = this.project(at[0].x, at[0].y, at[0].z), s2 = this.project(at[1].x, at[1].y, at[1].z);
      if (!s || !s2) return;
      const ang = Math.atan2(s2.y - s.y, s2.x - s.x);
      const size = Math.max(5, Math.min(11, s.s * 0.32)) * (emphasised ? 1.45 : 1);
      const col = p.tone === 'warn' ? colors.warn : p.dir === 'out' ? colors.out : colors.inn;
      if (emphasised) {
        ctx.fillStyle = col; ctx.globalAlpha = 0.18;
        ctx.beginPath(); ctx.arc(s.x, s.y, size * 1.9, 0, TAU); ctx.fill(); ctx.globalAlpha = 1;
      }
      ctx.save(); ctx.translate(s.x, s.y); ctx.rotate(ang);
      ctx.globalAlpha = sel && !emphasised ? 0.35 : (p.kind === 'ground' ? 0.8 : 1);
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.moveTo(size, 0); ctx.lineTo(-size * 0.7, size * 0.55); ctx.lineTo(-size * 0.35, 0); ctx.lineTo(-size * 0.7, -size * 0.55);
      ctx.closePath(); ctx.fill();
      if (emphasised) { ctx.strokeStyle = colors.surface; ctx.lineWidth = 1.5; ctx.stroke(); }
      ctx.restore(); ctx.globalAlpha = 1;
      this.hits.push({ x: s.x, y: s.y, plane: p });
    };
    let selected = null;
    for (const p of this.planes) { if (sel && p.id === sel.id) selected = p; else drawPlane(p, this.hover && this.hover.id === p.id); }
    if (selected) drawPlane(selected, true);
    else if (sel && sel.gate) {          // not flying yet: mark it at its gate
      const g = this.byCode[sel.gate];
      const s = g && this.project(g.x, 0, g.z);
      if (s) {
        ctx.strokeStyle = sel.dir === 'out' ? colors.out : colors.inn; ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.arc(s.x, s.y, 9, 0, TAU); ctx.stroke();
      }
    }
  }

  _pick(e) {
    const r = this.canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = null, bd = 14;
    for (const h of this.hits || []) { const d = Math.hypot(h.x - mx, h.y - my); if (d < bd) { bd = d; best = h; } }
    return { best, mx, my };
  }

  _bind() {
    let drag = null;
    this.canvas.addEventListener('pointerdown', (e) => {
      drag = { x: e.clientX, y: e.clientY, yaw: this.yaw, tilt: this.tilt, moved: false };
      this.canvas.setPointerCapture(e.pointerId);
    });
    this.canvas.addEventListener('pointerup', (e) => {
      if (drag && !drag.moved && this.onSelect) {
        const { best } = this._pick(e);
        this.onSelect(best ? best.plane.id : null);
      }
      drag = null;
    });
    this.canvas.addEventListener('pointermove', (e) => {
      if (drag) {
        if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 4) drag.moved = true;
        this.yaw = drag.yaw + (e.clientX - drag.x) * 0.005;
        this.tilt = Math.max(0.55, Math.min(1.45, drag.tilt - (e.clientY - drag.y) * 0.004));
        return;
      }
      const { best, mx, my } = this._pick(e);
      if (best) {
        const p = best.plane;
        this.hover = { id: p.id, codes: [p.from, p.to] };
        this.canvas.style.cursor = 'pointer';
        this.tooltip.hidden = false;
        this.tooltip.innerHTML = `<b>${p.id}</b><br>${p.fromName} to ${p.toName}<br>${p.status}<br><span class="muted">Click to follow</span>`;
        this.tooltip.style.left = Math.min(mx + 14, this.w - 250) + 'px';
        this.tooltip.style.top = (my + 14) + 'px';
      } else {
        this.hover = null; this.tooltip.hidden = true; this.canvas.style.cursor = '';
      }
    });
    this.canvas.addEventListener('pointerleave', () => { this.hover = null; this.tooltip.hidden = true; });
    this.canvas.addEventListener('wheel', (e) => { e.preventDefault(); this.dist = Math.max(28, Math.min(120, this.dist + e.deltaY * 0.04)); }, { passive: false });
  }
}
