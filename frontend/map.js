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
    this.reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this._bind();
    this.resize();
    new ResizeObserver(() => this.resize()).observe(canvas);
  }

  readColors() {
    const css = getComputedStyle(document.documentElement);
    const v = (n) => css.getPropertyValue(n).trim();
    this.colors = { bg: v('--map-bg'), dot: v('--map-dot'), route: v('--map-route'), out: v('--map-out'), inn: v('--map-in'),
      label: v('--map-label'), ink: v('--ink'), surface: v('--surface'), accent: v('--accent') };
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

  setPlanes(planes, labels) {
    this.planes = planes;
    this.labels = labels;
  }

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

    ctx.strokeStyle = colors.route; ctx.lineWidth = 1;
    for (const [from, to] of this.routes) {
      const a = this.byCode[from], b = this.byCode[to];
      if (!a || !b) continue;
      ctx.beginPath();
      for (let i = 0; i <= 40; i++) {
        const q = this.arcPoint(a, b, i / 40), s = this.project(q.x, q.y, q.z);
        if (!s) continue;
        i ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y);
      }
      ctx.stroke();
    }

    for (const a of this.airports) {
      const s = this.project(a.x, 0, a.z);
      if (!s) continue;
      const isMci = a.code === 'MCI';
      ctx.fillStyle = isMci ? colors.accent : colors.label;
      ctx.beginPath(); ctx.arc(s.x, s.y, isMci ? 5 : 2.4, 0, Math.PI * 2); ctx.fill();
      if (isMci) {
        const pulse = this.reduceMotion ? 0.5 : (now / 1600) % 1;
        ctx.strokeStyle = colors.accent; ctx.globalAlpha = 1 - pulse; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.ellipse(s.x, s.y, 8 + pulse * 26, (8 + pulse * 26) * Math.cos(this.tilt) * 0.9 + 2, 0, 0, Math.PI * 2); ctx.stroke();
        ctx.globalAlpha = 1;
      }
      if (isMci || a.base || this.labels.has(a.code) || (this.hover && this.hover.codes.includes(a.code))) {
        ctx.fillStyle = colors.ink;
        ctx.font = `${isMci ? 700 : 400} ${isMci ? 13 : 11}px 'Atkinson Hyperlegible', system-ui, sans-serif`;
        ctx.fillText(a.code, s.x + 7, s.y - 6);
      }
    }

    this.hits = [];
    for (const p of this.planes) {
      const a = this.byCode[p.from], b = this.byCode[p.to];
      if (!a || !b) continue;
      const highlight = this.hover && this.hover.id === p.id;
      if (highlight) {
        ctx.strokeStyle = p.dir === 'out' ? colors.out : colors.inn; ctx.lineWidth = 2; ctx.beginPath();
        for (let i = 0; i <= 40; i++) {
          const q = this.arcPoint(a, b, i / 40), s = this.project(q.x, q.y, q.z);
          if (s) (i ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y));
        }
        ctx.stroke();
      }
      const q = this.arcPoint(a, b, p.progress), q2 = this.arcPoint(a, b, Math.min(1, p.progress + 0.02));
      const s = this.project(q.x, q.y, q.z), s2 = this.project(q2.x, q2.y, q2.z);
      if (!s || !s2) continue;
      const ang = Math.atan2(s2.y - s.y, s2.x - s.x);
      const size = Math.max(5, Math.min(11, s.s * 0.32));
      ctx.save(); ctx.translate(s.x, s.y); ctx.rotate(ang);
      ctx.fillStyle = p.dir === 'out' ? colors.out : colors.inn;
      ctx.beginPath();
      ctx.moveTo(size, 0); ctx.lineTo(-size * 0.7, size * 0.55); ctx.lineTo(-size * 0.35, 0); ctx.lineTo(-size * 0.7, -size * 0.55);
      ctx.closePath(); ctx.fill();
      ctx.restore();
      this.hits.push({ x: s.x, y: s.y, plane: p });
    }
  }

  _bind() {
    let drag = null;
    this.canvas.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY, yaw: this.yaw, tilt: this.tilt }; this.canvas.setPointerCapture(e.pointerId); });
    this.canvas.addEventListener('pointerup', () => { drag = null; });
    this.canvas.addEventListener('pointermove', (e) => {
      if (drag) {
        this.yaw = drag.yaw + (e.clientX - drag.x) * 0.005;
        this.tilt = Math.max(0.55, Math.min(1.45, drag.tilt - (e.clientY - drag.y) * 0.004));
        return;
      }
      const r = this.canvas.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      let best = null, bd = 14;
      for (const h of this.hits || []) { const d = Math.hypot(h.x - mx, h.y - my); if (d < bd) { bd = d; best = h; } }
      if (best) {
        const p = best.plane;
        this.hover = { id: p.id, codes: [p.from, p.to] };
        this.tooltip.hidden = false;
        this.tooltip.innerHTML = `<b>${p.id}</b><br>${p.fromName} to ${p.toName}<br>${p.status}`;
        this.tooltip.style.left = Math.min(mx + 14, this.w - 250) + 'px';
        this.tooltip.style.top = (my + 14) + 'px';
      } else {
        this.hover = null; this.tooltip.hidden = true;
      }
    });
    this.canvas.addEventListener('pointerleave', () => { this.hover = null; this.tooltip.hidden = true; });
    this.canvas.addEventListener('wheel', (e) => { e.preventDefault(); this.dist = Math.max(28, Math.min(120, this.dist + e.deltaY * 0.04)); }, { passive: false });
  }
}
