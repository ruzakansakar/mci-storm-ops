// Simulated weather at MCI. The simulator has no weather readings, so this is a picture only:
// it is shaped by the day's real events (ground stop, ramp closure for lightning) and nothing else.
// No agent sees these numbers; they exist to make the replay readable.

const MCI = { lat: 39.30, lon: -94.71 };
const smooth = (a, b, x) => { const k = Math.max(0, Math.min(1, (x - a) / (b - a))); return k * k * (3 - 2 * k); };
const lerp = (a, b, k) => a + (b - a) * k;
const angLerp = (a, b, k) => { const d = ((b - a + 540) % 360) - 180; return (a + d * k + 360) % 360; };
const POINTS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
export const compass = (deg) => POINTS[Math.round(deg / 22.5) % 16];

// When the storm is around, taken from the day's world events. Null on a day without one.
export function stormWindow(world) {
  const start = world.find((w) => w.type === 'ground_stop_started' && /storm|weather|lightning/i.test(w.reason || ''));
  if (!start) return null;
  const end = world.find((w) => w.type === 'ground_stop_ended');
  const rc = world.find((w) => w.type === 'ramp_closed');
  const ro = world.find((w) => w.type === 'ramp_reopened');
  return { start: start.t, end: end ? end.t : start.t + 150, light0: rc ? rc.t : null, light1: ro ? ro.t : (rc ? rc.t + 35 : null) };
}

export function weatherAt(t, sw) {
  const temp0 = 52 + 17 * Math.max(0, Math.sin(Math.PI * (t - 360) / 900));
  if (!sw) {
    return { dir: 190, speed: 8, gust: null, temp: temp0, vis: 10, base: 6500, intensity: 0, lightning: false,
      cond: 'Clear', trend: 'Steady' };
  }
  const build = smooth(sw.start - 75, sw.start, t);
  const clear = smooth(sw.end, sw.end + 80, t);
  const I = build * (1 - clear);
  const lightning = sw.light0 != null && t >= sw.light0 && t < sw.light1;
  const dir = angLerp(angLerp(190, 250, build), 320, clear);
  const speed = lerp(lerp(8, 22, build), 11, clear) + (lightning ? 5 : 0);
  const gust = I > 0.15 || lightning ? speed + lerp(5, 16, I) + (lightning ? 7 : 0) : null;
  let cond = 'Clear', trend = 'Steady';
  if (t < sw.start && t >= sw.start - 120 && build <= 0.05) { cond = 'Clear, storms far to the southwest'; trend = 'Watch the radar'; }
  if (t < sw.start && build > 0.05) { cond = 'Storms building to the southwest'; trend = 'Getting worse'; }
  else if (lightning) { cond = 'Thunderstorms, lightning near the field'; trend = 'Worst now'; }
  else if (t >= sw.start && t < sw.end) { cond = 'Thunderstorms over the airport'; trend = t < (sw.light0 ?? sw.start) ? 'Getting worse' : 'Slowly easing'; }
  else if (t >= sw.end && clear < 0.85) { cond = 'Storms moving off to the northeast'; trend = 'Improving'; }
  else if (t >= sw.end) { cond = 'Clear behind the storm'; trend = 'Steady'; }
  return { dir, speed, gust, temp: temp0 - 12 * I, vis: lerp(10, lightning ? 1.5 : 3, I), base: lerp(6500, 1400, I),
    intensity: I, lightning, cond, trend };
}

// Where the storm cell is on the map: it tracks south-west to north-east across Kansas City.
export function stormCell(t, sw) {
  if (!sw) return null;
  const path = [
    [sw.start - 120, 37.7, -98.6], [sw.start, MCI.lat - 0.15, MCI.lon - 0.35],
    [sw.end, MCI.lat + 0.25, MCI.lon + 0.45], [sw.end + 110, 40.9, -91.4],
  ];
  if (t <= path[0][0] || t >= path[3][0]) return null;
  let i = 0;
  while (i < path.length - 2 && t > path[i + 1][0]) i++;
  const [t0, la0, lo0] = path[i], [t1, la1, lo1] = path[i + 1];
  const k = (t - t0) / (t1 - t0);
  const show = smooth(sw.start - 120, sw.start - 70, t) * (1 - smooth(sw.end + 40, sw.end + 110, t));
  const peak = smooth(sw.start - 60, sw.start, t) * (1 - smooth(sw.end - 20, sw.end + 40, t));
  return { lat: lerp(la0, la1, k), lon: lerp(lo0, lo1, k), show, core: 0.45 + 0.55 * peak };
}
