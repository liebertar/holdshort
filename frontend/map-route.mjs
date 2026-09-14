// Display geometry only. Runtime checks and simulator motion use the original legs.
const xy = p => [p.lon, p.lat];
const same = (a, b) => a.lat === b.lat && a.lon === b.lon && a.alt_m === b.alt_m;
const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);

export function isRemainingRoute(previous, next) {
  return next.length <= previous.length && next.every((p, i) =>
    same(p, previous[previous.length - next.length + i]));
}

export function makeCurve(position, route, steps = 16) {
  const waypoints = [position, ...route].filter((p, i, all) =>
    !i || p.lon !== all[i - 1].lon || p.lat !== all[i - 1].lat);
  const points = waypoints.map(xy);
  const altitudes = waypoints.map(p => Number(p.alt_m ?? 0));
  const coordinates = [], progress = [], lengths = [0];
  // Local longitude scale keeps distances appropriate for Manhattan.
  const scale = Math.cos(position.lat * Math.PI / 180);
  for (let i = 1; i < points.length; i++) {
    lengths.push(lengths[i - 1] + Math.hypot(
      (points[i][0] - points[i - 1][0]) * scale, points[i][1] - points[i - 1][1]));
  }
  for (let i = 0; i < points.length - 1; i++) {
    const b = points[i], c = points[i + 1];
    for (let j = 0; j < steps; j++) {
      const t = j / steps;
      // Approved legs are joined with straight lines. A Catmull-Rom smoothing used to cut the
      // corners of the 50 m grid detours between buildings, so the line seemed to go through
      // them. The line on screen must be the line that was judged.
      coordinates.push(mix(b, c, t));
      progress.push(lengths[i] + (lengths[i + 1] - lengths[i]) * t);
    }
  }
  coordinates.push(points.at(-1));
  progress.push(lengths.at(-1));
  return {points, coordinates, progress, lengths, scale, altitudes};
}

export function routeProgress(curve, position, minimum = 0) {
  const p = xy(position);
  let best = minimum, distance = Infinity;
  for (let i = 0; i < curve.points.length - 1; i++) {
    if (curve.lengths[i + 1] < minimum) continue;
    const a = curve.points[i], b = curve.points[i + 1];
    const dx = (b[0] - a[0]) * curve.scale, dy = b[1] - a[1];
    const t = Math.max(0, Math.min(1,
      (((p[0] - a[0]) * curve.scale) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)));
    const at = mix(a, b, t);
    const error = Math.hypot((p[0] - at[0]) * curve.scale, p[1] - at[1]);
    if (error < distance) {
      distance = error;
      best = Math.max(minimum, curve.lengths[i] + t * (curve.lengths[i + 1] - curve.lengths[i]));
    }
  }
  return best;
}

export function pointOnCurve(curve, progress) {
  const i = curve.progress.findIndex(p => p > progress);
  if (i < 0) return curve.coordinates.at(-1);
  if (!i) return curve.coordinates[0];
  return mix(curve.coordinates[i - 1], curve.coordinates[i],
    (progress - curve.progress[i - 1]) / (curve.progress[i] - curve.progress[i - 1]));
}

export function motionPoint(motion, now, duration) {
  const t = Math.max(0, Math.min(1, (now - motion.at) / duration));
  return motion.curve
    ? pointOnCurve(motion.curve, motion.from + (motion.to - motion.from) * t)
    : mix(motion.start, motion.end, t);
}

// Negotiation replay. Filing → refusal → rewrite → approval all finish between two 0.5 s polls,
// so left alone the screen would show only the outcome. This replays the routes that were
// actually exchanged, slowly. Every coordinate comes from the ledger or the simulator; no new
// route is made here. Timings were halved (from 3.6/1.0/2.6/1.6/2.2): standing 15 s with a full
// load was too long. Change them together with CLEARANCE_TICKS in sim/world.py and
// REDRAW_DELAY_S in drone/agent/loop.py.
export const GROW_MS = 2400;    // the route being planned grows forward
export const CHECK_MS = 600;    // the drawn route waits for its verdict
export const HOLD_MS = 1600;    // time to read what blocked it
export const FADE_MS = 1000;    // a refused route fades out
export const APPROVED_HOLD_MS = 1400;   // the approval stays on screen

/** How long one stage lives on screen. This decides when the next stage may start. */
export function stageLife(kind) {
  return kind === "approved"
    ? GROW_MS + CHECK_MS + APPROVED_HOLD_MS
    : GROW_MS + CHECK_MS + HOLD_MS + FADE_MS;
}

const ease = t => 1 - (1 - t) ** 3;

/** Cut the curve between two progress values. Both ends land exactly on those points. */
export function sliceCurve(curve, from, to) {
  const total = curve.progress.at(-1);
  const start = Math.max(0, Math.min(total, from));
  const end = Math.max(start, Math.min(total, to));
  const out = [pointOnCurve(curve, start)];
  for (let i = 0; i < curve.progress.length; i++)
    if (curve.progress[i] > start && curve.progress[i] < end) out.push(curve.coordinates[i]);
  out.push(pointOnCurve(curve, end));
  return out;
}

/**
 * How much of the curve a stage has drawn so far, or null once it is over.
 * An approval grows and ends (the usual approved-route display takes over from there).
 * A refusal grows, holds, and fades.
 */
export function stageWindow(kind, elapsed) {
  if (elapsed < 0) return null;
  if (elapsed < GROW_MS) return [0, ease(elapsed / GROW_MS)];
  return elapsed < stageLife(kind) ? [0, 1] : null;
}

/**
 * How strongly the stage shows right now.
 * Rewinding the line toward the drone looked like it was being snatched back, so it fades in place.
 */
export function stageFade(kind, elapsed) {
  const after = elapsed - GROW_MS - CHECK_MS;
  if (after < 0) return 1;
  if (kind === "approved") return Math.max(0, 1 - after / APPROVED_HOLD_MS);
  return after < HOLD_MS ? 1 : Math.max(0, 1 - (after - HOLD_MS) / FADE_MS);
}

/** Which phase the stage is in. This decides what the screen says. */
export function stagePhase(kind, elapsed) {
  if (elapsed < GROW_MS) return "drawing";
  // The moment the verdict comes down after the route is drawn. Without it the colour changes
  // as soon as drawing ends, and nobody can see who decided what.
  if (elapsed < GROW_MS + CHECK_MS) return "checking";
  return kind === "approved" ? "approved" : "refused";
}

/** Where the label sits. Following the growing head keeps the text moving and hard to read. */
export function labelAnchor(curve) {
  return curve.coordinates[0];
}

// Geometry that makes altitude visible. MapLibre 5 has no floating lines (line-z-offset is not
// in the bundle at all). Instead a thin ribbon is raised to its real altitude with
// fill-extrusion — the slab between base and height is that leg's altitude.
const METRES_PER_DEG_LAT = 110_570;
// Display width, not the conflict-check width. At 44 m the ribbon was wider than the streets and
// you could not tell where it went between buildings. 18 m is 7 px at zoom 14.5 — visible and
// still reads as a path.
const RIBBON_HALF_M = 9;      // half width → an 18 m corridor
const RIBBON_THICK_M = 3;     // ribbon thickness (top to bottom)
// The corridor sits just below the aircraft. A thick slab at exactly the aircraft's altitude
// buried the aircraft half inside it. A few metres lower is invisible on screen and keeps the
// aircraft in view.
const RIBBON_DROP_M = 4.5;    // the ribbon's top is this far below the aircraft's altitude
// One dash and one gap. A dash longer than the ribbon is wide reads as a line and shows direction.
const DASH_M = 36;
const GAP_M = 14;

/**
 * One rectangle per leg. Each leg has its own approved altitude, so each slab floats on its
 * own — one merged shape would hide the difference.
 * offsetM moves the slab sideways from the centreline (left of travel is +); 0 is on the centreline.
 */
export function ribbon(points, halfWidthM = RIBBON_HALF_M, thicknessM = RIBBON_THICK_M, offsetM = 0) {
  const out = [];
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    const scale = Math.cos(a.lat * Math.PI / 180) || 1;
    const dLat = b.lat - a.lat, dLon = (b.lon - a.lon) * scale;
    const length = Math.hypot(dLat, dLon);
    if (!(length > 0)) continue;
    // A 1 m normal to the direction of travel, converted from metres to degrees of latitude.
    const unitLat = -dLon / length / METRES_PER_DEG_LAT, unitLon = dLat / length / METRES_PER_DEG_LAT / scale;
    const outer = offsetM + halfWidthM, inner = offsetM - halfWidthM;
    const altitude = Number(b.alt_m ?? a.alt_m ?? 0);
    const top = Math.max(thicknessM, altitude - RIBBON_DROP_M);
    out.push({
      polygon: [
        [a.lon + unitLon * outer, a.lat + unitLat * outer], [b.lon + unitLon * outer, b.lat + unitLat * outer],
        [b.lon + unitLon * inner, b.lat + unitLat * inner], [a.lon + unitLon * inner, a.lat + unitLat * inner],
        [a.lon + unitLon * outer, a.lat + unitLat * outer],
      ],
      base: Math.max(0, top - thicknessM),
      height: top,
    });
  }
  return out;
}

// The vertical part at a vertex where the altitude changes. The aircraft climbs or descends in
// place at a vertex (sim _advance), so a vertical dotted column stands there to join the two
// slabs; without it the corridor looked broken. It is the corridor dash stood on end: 18 m wide,
// 3 m thick slabs in 36 m dashes with 14 m gaps, the width across the direction of travel like
// the corridor. Cube-shaped dashes read as a different object.

/** A vertical dotted column at one vertex, from altitude a to b. Same reference as the corridor
 * top (aircraft altitude − DROP). headingLonLat is the direction (degree deltas) of the leg that
 * leaves this vertex. */
function altitudeColumn(lat, lon, fromAltM, toAltM, headingLonLat = [0, 1]) {
  const low = Math.min(fromAltM, toAltM) - RIBBON_DROP_M;
  const high = Math.max(fromAltM, toAltM) - RIBBON_DROP_M;
  const scale = Math.cos(lat * Math.PI / 180) || 1;
  let dLon = headingLonLat[0] * scale, dLat = headingLonLat[1];
  const len = Math.hypot(dLon, dLat) || 1;
  dLon /= len; dLat /= len;
  const half = RIBBON_HALF_M / METRES_PER_DEG_LAT, thin = RIBBON_THICK_M / 2 / METRES_PER_DEG_LAT;
  const nLat = -dLon * half, nLon = dLat * half / scale;   // across the direction of travel
  const tLat = dLat * thin, tLon = dLon * thin / scale;    // along the direction of travel
  const polygon = [
    [lon + nLon + tLon, lat + nLat + tLat], [lon - nLon + tLon, lat - nLat + tLat],
    [lon - nLon - tLon, lat - nLat - tLat], [lon + nLon - tLon, lat + nLat - tLat],
    [lon + nLon + tLon, lat + nLat + tLat],
  ];
  const out = [];
  for (let z = low; z < high; z += DASH_M + GAP_M) {
    const top = Math.min(z + DASH_M, high);
    if (top <= 0.5) continue;
    out.push({polygon, base: Math.max(0, z), height: top, column: true});
  }
  return out;
}

/** The same curve as a floating dotted ribbon. Dash positions are fixed from the start of the
 * route, so erasing the flown part does not shift what remains. Altitude follows each filed leg. */
export function curveRibbon(curve, from, to) {
  const dash = DASH_M / METRES_PER_DEG_LAT;
  const period = (DASH_M + GAP_M) / METRES_PER_DEG_LAT;
  const out = [];
  for (let leg = 0; leg < curve.lengths.length - 1; leg++) {
    const start = Math.max(from, curve.lengths[leg]);
    const end = Math.min(to, curve.lengths[leg + 1]);
    for (let at = Math.floor(start / period) * period; at < end; at += period) {
      const left = Math.max(start, at), right = Math.min(end, at + dash);
      if (right <= left) continue;
      out.push(...ribbon(sliceCurve(curve, left, right).map(([lon, lat]) =>
        ({lon, lat, alt_m:curve.altitudes[leg + 1]}))));
    }
  }
  return out;
}

/** The corridor outline. It pulses around the corridor of an aircraft whose link is lost — the
 * dashes stay (the space is still reserved) and thin rails (railM) run padM outside the corridor
 * at its four edges: both sides × top and bottom. Blinking the dashes themselves read as the
 * corridor disappearing, the opposite of "still reserved"; covering the corridor with a
 * translucent slab mixed with the green dashes into one muddy bar. */
export function curveShell(curve, from, to, padM = 5, railM = 1.6) {
  const out = [];
  const side = RIBBON_HALF_M + padM;
  for (let leg = 0; leg < curve.lengths.length - 1; leg++) {
    const start = Math.max(from, curve.lengths[leg]);
    const end = Math.min(to, curve.lengths[leg + 1]);
    if (end <= start) continue;
    const altitude = curve.altitudes[leg + 1];
    const at = alt => sliceCurve(curve, start, end).map(([lon, lat]) => ({lon, lat, alt_m:alt}));
    // The ribbon's top is alt − DROP. Choose altitudes so the upper rail's top is the corridor
    // top + padM and the lower rail's bottom is the corridor bottom − padM.
    const upper = at(altitude + padM), lower = at(altitude - RIBBON_THICK_M - padM + railM);
    for (const offset of [side, -side])
      out.push(...ribbon(upper, railM / 2, railM, offset), ...ribbon(lower, railM / 2, railM, offset));
  }
  return out;
}

/** A vertical dotted column at every corridor vertex where the altitude changes. Vertex v has the
 * leg altitude altitudes[v] before it and altitudes[v+1] after it. The start (v=0) is the takeoff
 * column from the ground to the first leg. Vertices already flown (before from) and not yet drawn
 * (after to) are outside the window, like the corridor. */
export function curveColumns(curve, from, to) {
  const out = [];
  for (let v = 0; v < curve.lengths.length - 1; v++) {
    const at = curve.lengths[v];
    if (at < from || at > to) continue;
    const before = curve.altitudes[v], after = curve.altitudes[v + 1];
    if (!(Math.abs(after - before) > 1)) continue;
    const [lon, lat] = curve.points[v];
    const [lon2, lat2] = curve.points[v + 1];
    out.push(...altitudeColumn(lat, lon, before, after, [lon2 - lon, lat2 - lat]));
  }
  return out;
}

/**
 * A hexagonal block at altitude. MapLibre cannot raise symbols, so aircraft and cargo are drawn
 * with these; an icon stuck to the ground does not read as flying.
 */
export function hex(lat, lon, radiusM, base, thicknessM) {
  const r = radiusM / METRES_PER_DEG_LAT;
  const rLon = r / (Math.cos(lat * Math.PI / 180) || 1);
  const polygon = [];
  for (let i = 0; i <= 6; i++) {
    const angle = (i / 6) * 2 * Math.PI + Math.PI / 6;
    polygon.push([lon + rLon * Math.cos(angle), lat + r * Math.sin(angle)]);
  }
  return {polygon, base: Math.max(0, base), height: Math.max(0.5, base + thicknessM)};
}

/**
 * One quadcopter: a body and four rotors at altitude. A single hexagon did not read as anything,
 * so the arms and rotors stand separately. Much larger than the real thing (about 1 m) — at real
 * size it would be less than a pixel at zoom 14.5.
 */
// scale is for the PX4 ghost: it flies the same spot as the blue aircraft and should show one
// size larger, like an outline.
export function droneBody(lat, lon, altitude, heading = 0, scale = 1) {
  const base = Math.max(0, altitude - 1);
  const parts = [hex(lat, lon, 5 * scale, base, 3.5 * scale)];
  const turn = (heading * Math.PI) / 180;
  const armM = 11 * scale;
  for (let i = 0; i < 4; i++) {
    const angle = turn + Math.PI / 4 + (i / 4) * 2 * Math.PI;
    const dLat = (armM * Math.cos(angle)) / METRES_PER_DEG_LAT;
    const dLon = (armM * Math.sin(angle)) / METRES_PER_DEG_LAT
      / (Math.cos(lat * Math.PI / 180) || 1);
    parts.push(hex(lat + dLat, lon + dLon, 4.5 * scale, base + 1, 1.6 * scale));
  }
  return parts;
}

/** A construction crane: a thin mast with a long jib on top. It is a temporary obstacle missing
 * from the building data (OSM), so it must look different from a building. */
export function craneShape(lat, lon, heightM) {
  const scale = Math.cos(lat * Math.PI / 180) || 1;
  const east = m => m / METRES_PER_DEG_LAT / scale, north = m => m / METRES_PER_DEG_LAT;
  const top = Math.max(6, Number(heightM) || 0);
  const jib = [[lon + east(-10), lat - north(1.2)], [lon + east(32), lat - north(1.2)],
               [lon + east(32), lat + north(1.2)], [lon + east(-10), lat + north(1.2)], [lon + east(-10), lat - north(1.2)]];
  return [hex(lat, lon, 2.2, 0, top), {polygon: jib, base: top - 3.2, height: top - 1}];
}

/** One square box. Cargo reads as cargo when it is a box. */
export function box(lat, lon, halfM, base, thicknessM) {
  const r = halfM / METRES_PER_DEG_LAT;
  const rLon = r / (Math.cos(lat * Math.PI / 180) || 1);
  return {polygon: [[lon - rLon, lat - r], [lon + rLon, lat - r], [lon + rLon, lat + r],
                    [lon - rLon, lat + r], [lon - rLon, lat - r]],
          base: Math.max(0, base), height: Math.max(0.5, base + thicknessM)};
}

/** A circle drawn on the ground. A screen-facing circle layer has no depth on a tilted map, so
 * landing and takeoff areas are real-coordinate polygons that tilt with the map. */
export function groundDisc(lat, lon, radiusM, sides = 28) {
  const r = radiusM / METRES_PER_DEG_LAT;
  const rLon = r / (Math.cos(lat * Math.PI / 180) || 1);
  const ring = [];
  for (let i = 0; i <= sides; i++) {
    const angle = (i / sides) * 2 * Math.PI;
    ring.push([lon + rLon * Math.cos(angle), lat + r * Math.sin(angle)]);
  }
  return ring;
}

// The runtime and its links. The sky-net runtime is drawn at a real address: 26 Federal Plaza
// (the Jacob K. Javits Federal Building, federal government offices at Foley Square, Lower
// Manhattan). It does not mean a server stands there. The building is about 240 m tall
// (RUNTIME_ROOF_M), so a thin mast rises from its roof to the link altitude, 600 m, well clear of
// the skyline. The links are the same 3D dashes as a corridor but much thinner — corridors and
// aircraft stay the subject.
export const RUNTIME_SITE = {name:"26 Federal Plaza", lat:40.71537, lon:-74.00421};
export const RUNTIME_ROOF_M = 240;
export const RUNTIME_ALT_M = 600;
// 4.4 m wide. At 3 m a link was barely 2 px at zoom 16 and vanished on the light basemap.
const SIGNAL_DASH_M = 20;
const SIGNAL_GAP_M = 22;
const SIGNAL_HALF_M = 2.2;
// A lost link must look empty in the middle. No dashes are placed in this part of the line.
const SIGNAL_BREAK = [0.36, 0.64];

/** The runtime mast: a thin column from the roof to the link altitude and a thin head plate on top. */
export function runtimeMast(site = RUNTIME_SITE, altM = RUNTIME_ALT_M, roofM = RUNTIME_ROOF_M) {
  const roof = Math.min(Number(roofM) || 0, altM - 26);
  return [hex(site.lat, site.lon, 4.5, roof, altM - roof), hex(site.lat, site.lon, 10, altM, 2.5)];
}

/** The point t (0–1) of the way between two 3D points. Altitude is mixed too. */
function signalPoint(from, to, t) {
  return {lat: from.lat + (to.lat - from.lat) * t, lon: from.lon + (to.lon - from.lon) * t,
          alt_m: Number(from.alt_m) + (Number(to.alt_m) - Number(from.alt_m)) * t};
}

/** The footprint of one dash. A mostly horizontal dash is a thin slab lying along the line; a
 * nearly vertical one (an aircraft close to the mast) is a small square — cutting a steep line
 * into horizontal slabs looked like a staircase. */
function dashFootprint(a, b, halfM) {
  const scale = Math.cos(a.lat * Math.PI / 180) || 1;
  const dLat = b.lat - a.lat, dLon = (b.lon - a.lon) * scale;
  const flat = Math.hypot(dLat, dLon) * METRES_PER_DEG_LAT;
  if (flat > halfM * 2) return ribbon([a, b], halfM, 1)[0].polygon;
  const r = halfM / METRES_PER_DEG_LAT, rLon = r / scale;
  const lat = (a.lat + b.lat) / 2, lon = (a.lon + b.lon) / 2;
  return [[lon - rLon, lat - r], [lon + rLon, lat - r], [lon + rLon, lat + r], [lon - rLon, lat + r], [lon - rLon, lat - r]];
}

/**
 * The dotted link from an aircraft to the runtime. Dashes are cut by 3D length — cut by
 * horizontal length, a line climbing almost straight up to the mast top would be a single dash.
 * Each dash carries its own altitude band as base–height.
 * broken leaves the middle (SIGNAL_BREAK) empty — a lost link.
 */
export function signalDashes(from, to, broken = false, halfM = SIGNAL_HALF_M) {
  const scale = Math.cos(from.lat * Math.PI / 180) || 1;
  const dx = (to.lon - from.lon) * scale * METRES_PER_DEG_LAT, dy = (to.lat - from.lat) * METRES_PER_DEG_LAT;
  const dz = Number(to.alt_m) - Number(from.alt_m);
  const length = Math.hypot(dx, dy, dz);
  const out = [];
  if (!(length > 1)) return out;
  for (let s = 0; s < length; s += SIGNAL_DASH_M + SIGNAL_GAP_M) {
    const t0 = s / length, t1 = Math.min(s + SIGNAL_DASH_M, length) / length;
    if (broken && t1 > SIGNAL_BREAK[0] && t0 < SIGNAL_BREAK[1]) continue;
    const a = signalPoint(from, to, t0), b = signalPoint(from, to, t1);
    out.push({polygon: dashFootprint(a, b, halfM),
              base: Math.max(0, Math.min(a.alt_m, b.alt_m) - halfM),
              height: Math.max(a.alt_m, b.alt_m) + halfM});
  }
  return out;
}

/** One event travelling along a link (filing, verdict, recall, telemetry): a small hexagonal block at altitude. */
export function signalDot(from, to, t, radiusM = 7) {
  const p = signalPoint(from, to, Math.max(0, Math.min(1, t)));
  return hex(p.lat, p.lon, radiusM, p.alt_m - radiusM, radiusM * 2);
}

/** How many pixels above its ground point an altitude appears on screen (approximate). Labels of
 * raised markers are placed there. MapLibre tiles are 512 px, so one pixel at zoom z is
 * circumference / (512·2^z) metres. Perspective is ignored. */
export function altitudeLift(zoom, pitchDeg, lat, altM) {
  return altM / metresPerPixel(zoom, lat) * Math.sin(pitchDeg * Math.PI / 180);
}

/** Metres on the ground per screen pixel (at the centre of the screen). */
function metresPerPixel(zoom, lat) {
  return 40075016.686 * (Math.cos(lat * Math.PI / 180) || 1) / (512 * 2 ** zoom);
}

/** Half width (m) of a link. Fixed in metres it disappears below zoom 14 and thickens up close.
 * It stays about 2.4 px on screen, and never thinner than SIGNAL_HALF_M up close. */
export function signalHalfWidth(zoom, lat) {
  return Math.max(SIGNAL_HALF_M, metresPerPixel(zoom, lat) * 1.2);
}

/** The load on board, stacked on the aircraft one box per item. It grows while loading and shrinks while unloading. */
export const CARGO_MAX = 6;
export function cargoStack(lat, lon, altitude, count, halfM = 3.5) {
  const boxes = [];
  for (let i = 0; i < Math.min(CARGO_MAX, count); i++)
    boxes.push(box(lat, lon, halfM, altitude + 3 + i * 4.2, 3.6));
  return boxes;
}
