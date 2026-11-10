import {
  BufferAttribute, BufferGeometry, CapsuleGeometry, CatmullRomCurve3, CylinderGeometry, ExtrudeGeometry,
  Float32BufferAttribute, LatheGeometry, Matrix4, Quaternion, Shape, ShapeGeometry, ShapeUtils, TubeGeometry, Vector2, Vector3,
} from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { mergeGeometries, mergeVertices, toCreasedNormals } from 'three/addons/utils/BufferGeometryUtils.js';

// Model units are 100 mm. Everything here is original procedural geometry built from public product
// dimensions and reference photos; it is not manufacturer CAD and carries no logos or artwork.
// This module must stay free of relative imports so `node --test` can load it directly.

const TAU = Math.PI * 2;
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const smoothstep = (value: number) => { const x = clamp(value, 0, 1); return x * x * (3 - 2 * x); };
const signedPow = (value: number, exponent: number) => Math.sign(value) * Math.abs(value) ** exponent;

// ---------------------------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------------------------

export function roundedBox(width: number, height: number, depth: number, radius = 0.025, segments = 3) {
  return new RoundedBoxGeometry(width, height, depth, segments, Math.min(radius, width / 2, height / 2, depth / 2));
}

/** Counter-clockwise rounded rectangle with a constant point count of 4 * (segments + 1). */
export function roundedRect(halfWidth: number, halfDepth: number, radius: number, segments = 4): Vector2[] {
  const r = clamp(radius, 0.0005, Math.min(halfWidth, halfDepth));
  const cx = halfWidth - r, cy = halfDepth - r;
  const corners: [number, number, number][] = [[cx, cy, 0], [-cx, cy, Math.PI / 2], [-cx, -cy, Math.PI], [cx, -cy, Math.PI * 1.5]];
  const points: Vector2[] = [];
  for (const [x, y, start] of corners) {
    for (let i = 0; i <= segments; i++) {
      const angle = start + (i / segments) * Math.PI / 2;
      points.push(new Vector2(x + r * Math.cos(angle), y + r * Math.sin(angle)));
    }
  }
  return points;
}

function superellipse(halfX: number, halfY: number, exponent: number, count: number): Vector2[] {
  return Array.from({ length: count }, (_, i) => {
    const angle = (i / count) * TAU;
    return new Vector2(halfX * signedPow(Math.cos(angle), 2 / exponent), halfY * signedPow(Math.sin(angle), 2 / exponent));
  });
}

function indexed(positions: number[], indices: number[], uvs?: number[]) {
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
  if (uvs) geometry.setAttribute('uv', new Float32BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  return geometry;
}

/** Signed enclosed volume relative to `origin`; positive when triangles wind outward. */
export function meshVolume(geometry: BufferGeometry, origin = new Vector3()) {
  const position = geometry.getAttribute('position');
  const index = geometry.getIndex();
  const count = index ? index.count : position.count;
  const a = new Vector3(), b = new Vector3(), c = new Vector3();
  let volume = 0;
  for (let i = 0; i < count; i += 3) {
    a.fromBufferAttribute(position, index ? index.getX(i) : i).sub(origin);
    b.fromBufferAttribute(position, index ? index.getX(i + 1) : i + 1).sub(origin);
    c.fromBufferAttribute(position, index ? index.getX(i + 2) : i + 2).sub(origin);
    volume += a.dot(b.cross(c));
  }
  return volume / 6;
}

function flipWinding(geometry: BufferGeometry) {
  const index = geometry.getIndex();
  if (index) {
    for (let i = 0; i < index.count; i += 3) {
      const b = index.getX(i + 1);
      index.setX(i + 1, index.getX(i + 2));
      index.setX(i + 2, b);
    }
    index.needsUpdate = true;
    return;
  }
  for (const name of Object.keys(geometry.attributes)) {
    const attribute = geometry.getAttribute(name) as BufferAttribute;
    const size = attribute.itemSize, array = attribute.array;
    for (let i = 0; i < attribute.count; i += 3) {
      for (let k = 0; k < size; k++) {
        const p = (i + 1) * size + k, q = (i + 2) * size + k;
        const swap = array[p]; array[p] = array[q]; array[q] = swap;
      }
    }
    attribute.needsUpdate = true;
  }
}

/** Closed (or bottom-open, origin on the opening plane) meshes: make triangles face outward. */
function orientOutward(geometry: BufferGeometry, origin = new Vector3()) {
  if (meshVolume(geometry, origin) < 0) flipWinding(geometry);
  geometry.computeVertexNormals();
  return geometry;
}

/** Open dome-like surfaces: outward means away from a horizontal axis through `axisY`. */
function orientAway(geometry: BufferGeometry, reference: (centroid: Vector3) => Vector3) {
  const position = geometry.getAttribute('position');
  const index = geometry.getIndex();
  const count = index ? index.count : position.count;
  const a = new Vector3(), b = new Vector3(), c = new Vector3(), n = new Vector3(), m = new Vector3();
  let score = 0;
  for (let i = 0; i < count; i += 3) {
    a.fromBufferAttribute(position, index ? index.getX(i) : i);
    b.fromBufferAttribute(position, index ? index.getX(i + 1) : i + 1);
    c.fromBufferAttribute(position, index ? index.getX(i + 2) : i + 2);
    m.copy(a).add(b).add(c).divideScalar(3);
    n.subVectors(b, a).cross(c.clone().sub(a));
    score += n.dot(m.clone().sub(reference(m)));
  }
  if (score < 0) flipWinding(geometry);
  geometry.computeVertexNormals();
  return geometry;
}

interface LoftOptions { closed?: boolean; wrap?: boolean; start?: Vector3; end?: Vector3; uvScale?: [number, number] }

/** Connects equally sized rings; optional fan caps and wrap-around for tori. Normals are left to the caller. */
function loft(rings: Vector3[][], { closed = true, wrap = false, start, end, uvScale = [1, 1] }: LoftOptions = {}) {
  const count = rings[0].length;
  const positions: number[] = [], uvs: number[] = [], indices: number[] = [];
  rings.forEach((ring, r) => ring.forEach((point, i) => {
    positions.push(point.x, point.y, point.z);
    uvs.push((i / count) * uvScale[0], (r / Math.max(1, rings.length - 1)) * uvScale[1]);
  }));
  const segments = closed ? count : count - 1;
  const spans = wrap ? rings.length : rings.length - 1;
  for (let r = 0; r < spans; r++) {
    const next = (r + 1) % rings.length;
    for (let i = 0; i < segments; i++) {
      const i2 = (i + 1) % count;
      const a = r * count + i, b = r * count + i2, c = next * count + i2, d = next * count + i;
      indices.push(a, b, c, a, c, d);
    }
  }
  const cap = (center: Vector3, ring: number, reverse: boolean) => {
    const centerIndex = positions.length / 3;
    positions.push(center.x, center.y, center.z);
    uvs.push(0.5 * uvScale[0], ring === 0 ? 0 : uvScale[1]);
    for (let i = 0; i < segments; i++) {
      const a = ring * count + i, b = ring * count + ((i + 1) % count);
      if (reverse) indices.push(centerIndex, b, a); else indices.push(centerIndex, a, b);
    }
  };
  if (start) cap(start, 0, true);
  if (end) cap(end, rings.length - 1, false);
  return indexed(positions, indices, uvs);
}

/** Sweeps a profile along a path lying in the XY plane; profile x = in-plane normal, y = +Z. */
function sweepPlanar(path: Vector3[], profile: (k: number) => Vector2[], uvScale: [number, number] = [1, 1]) {
  const binormal = new Vector3(0, 0, 1);
  const rings = path.map((point, k) => {
    const tangent = path[Math.min(path.length - 1, k + 1)].clone().sub(path[Math.max(0, k - 1)]).normalize();
    const normal = new Vector3().crossVectors(binormal, tangent).normalize();
    return profile(k).map(q => point.clone().addScaledVector(normal, q.x).addScaledVector(binormal, q.y));
  });
  return orientOutward(loft(rings, { start: path[0], end: path[path.length - 1], uvScale }));
}

/** Flat fan facing `facing`. */
function disc(points: Vector3[], center: Vector3, facing: Vector3) {
  const positions: number[] = [center.x, center.y, center.z];
  const uvs: number[] = [0.5, 0.5];
  const indices: number[] = [];
  points.forEach((p, i) => { positions.push(p.x, p.y, p.z); uvs.push(i / points.length, 1); });
  for (let i = 0; i < points.length; i++) indices.push(0, 1 + i, 1 + ((i + 1) % points.length));
  const geometry = indexed(positions, indices, uvs);
  const n = points[0].clone().sub(center).cross(points[1].clone().sub(center));
  if (n.dot(facing) < 0) flipWinding(geometry);
  geometry.computeVertexNormals();
  return geometry;
}

/** ExtrudeGeometry with smooth bevel/side normals but flat caps (avoids shading gradients on large faces). */
function smoothExtrude(geometry: ExtrudeGeometry) {
  const position = geometry.getAttribute('position') as BufferAttribute;
  const part = (materialIndex: number) => {
    const groups = geometry.groups.filter(group => group.materialIndex === materialIndex);
    const values: number[] = [];
    for (const group of groups) for (let i = group.start; i < group.start + group.count; i++) values.push(position.getX(i), position.getY(i), position.getZ(i));
    const result = new BufferGeometry();
    result.setAttribute('position', new Float32BufferAttribute(values, 3));
    return result;
  };
  const caps = part(0);
  caps.computeVertexNormals();
  const welded = mergeVertices(part(1), 1e-5);
  welded.computeVertexNormals();
  const sides = welded.toNonIndexed();
  welded.dispose();
  geometry.dispose();
  return mergeAll([caps, sides]);
}

/** Merges parts with any mix of indexed/non-indexed and differing optional attributes. */
export function mergeAll(parts: BufferGeometry[]) {
  const names = Object.keys(parts[0].attributes).filter(name => parts.every(part => part.getAttribute(name)));
  const mixed = parts.some(part => !part.getIndex()) && parts.some(part => part.getIndex());
  const prepared = parts.map(part => {
    let next = mixed && part.getIndex() ? part.toNonIndexed() : part;
    if (next !== part) part.dispose();
    for (const name of Object.keys(next.attributes)) if (!names.includes(name)) next.deleteAttribute(name);
    next.morphAttributes = {};
    next.clearGroups();
    return next;
  });
  const merged = mergeGeometries(prepared, false);
  prepared.forEach(part => part.dispose());
  if (!merged) throw new Error('Incompatible device geometry');
  merged.computeBoundingBox();
  merged.computeBoundingSphere();
  return merged;
}

function moveTo<T extends BufferGeometry>(geometry: T, point: Vector3) { geometry.translate(point.x, point.y, point.z); return geometry; }

/** Local frame on a surface: y = normal, z = tangent (orthogonalised), origin pushed `offset` along the normal. */
function surfaceMatrix(point: Vector3, normal: Vector3, tangent: Vector3, offset = 0) {
  const t = tangent.clone().addScaledVector(normal, -tangent.dot(normal)).normalize();
  const x = new Vector3().crossVectors(normal, t).normalize();
  return new Matrix4().makeBasis(x, normal, t).setPosition(point.clone().addScaledVector(normal, offset));
}

function placeOnFrame(geometry: BufferGeometry, point: Vector3, normal: Vector3, tangent: Vector3, offset = 0) {
  return geometry.applyMatrix4(surfaceMatrix(point, normal, tangent, offset));
}

// ---------------------------------------------------------------------------------------------
// Logitech MX Keys Mini, Graphite, Turkish Q (TR-Q, ISO) as in the user's reference photo.
// 296 x 132 x 21 mm: wedge deck with a slightly raised rear bar; no numpad or navigation cluster.
// ---------------------------------------------------------------------------------------------

export const KEYBOARD = {
  width: 2.96, depth: 1.32, deckDepth: 1.26, frontHeight: 0.10, rearHeight: 0.158, height: 0.208,
  barDepth: 0.115, barInset: 0.12, capHeight: 0.034, pitch: 0.19, gap: 0.034, functionDepth: 0.125,
  rearBorder: 0.10, cornerRadius: 0.08, edge: 0.028,
} as const;
const DECK_REAR = KEYBOARD.depth / 2 - KEYBOARD.deckDepth; // the deck's rear edge tucks under the raised bar
const DECK_CENTER = DECK_REAR + KEYBOARD.deckDepth / 2;
const DECK_MIDDLE = (KEYBOARD.frontHeight + KEYBOARD.rearHeight) / 2;
const DECK_SLOPE = (KEYBOARD.rearHeight - KEYBOARD.frontHeight) / KEYBOARD.deckDepth;

/** Built-in typing angle of the wedge body; the bottom rests flat on the desk. */
export const KEYBOARD_TILT = Math.atan(DECK_SLOPE);
export const KEYBOARD_LIFT = DECK_MIDDLE * Math.cos(KEYBOARD_TILT) + DECK_CENTER * Math.sin(KEYBOARD_TILT);
/** Local deck plane (y = 0) to desk-space transform; the flat bottom lands on y = 0. */
export function keyboardFrame(target = new Matrix4()) {
  return target.makeRotationX(KEYBOARD_TILT).setPosition(0, KEYBOARD_LIFT, 0);
}
/** Desk plane expressed in the tilted local frame. */
const keyboardFloor = (z: number) => -DECK_MIDDLE + DECK_SLOPE * (z - DECK_CENTER);

export type KeyStyle = 'letter' | 'dual' | 'mod' | 'function' | 'split' | 'arrow' | 'space';
export interface KeySpec {
  /** Primary legend (letter, unshifted symbol, modifier word or digit on Easy-Switch keys). */
  label: string;
  /** Upper (shifted) legend. */
  shift?: string;
  /** Small AltGr legend, bottom right. */
  alt?: string;
  /** Second word of split modifiers ("opt | start") or a leading glyph. */
  second?: string;
  glyph?: string;
  /** F-row icon id and tiny function label. */
  icon?: string;
  sub?: string;
  x: number; z: number; width: number; depth: number; row: number; style: KeyStyle;
  /** Counter-clockwise orthogonal outline relative to (x, z); rectangles have 4 corners, ISO enter 6. */
  outline: [number, number][];
  /** Half of the narrowest cap section (limits the dish). */
  minHalf: number;
}

type KeyEntry = Partial<KeySpec> & { label: string; u: number; style: KeyStyle; skip?: boolean };

const rectOutline = (w: number, d: number): [number, number][] => [[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]];

/** TR-Q ISO layout read from the reference photo (rows: F-row, numbers, Q, A, Z, bottom). */
export function keyboardKeys(): KeySpec[] {
  const { pitch, gap, functionDepth, rearBorder } = KEYBOARD;
  const capDepth = pitch - gap;
  const fTop = DECK_REAR + rearBorder;
  const rowZ = [fTop + functionDepth / 2, ...[0, 1, 2, 3, 4].map(i => fTop + functionDepth + gap + capDepth / 2 + i * pitch)];
  const L = (label: string, alt?: string): KeyEntry => ({ label, u: 1, style: 'letter', alt });
  const D = (label: string, shift?: string, alt?: string): KeyEntry => ({ label, u: 1, style: 'dual', shift, alt });
  const M = (label: string, u = 1, extra: Partial<KeyEntry> = {}): KeyEntry => ({ label, u, style: 'mod', ...extra });
  const F = (icon: string, sub: string, label = ''): KeyEntry => ({ label, u: 1, style: 'function', icon, sub });
  const S = (label: string, second: string, glyph?: string): KeyEntry => ({ label, u: 1, style: 'split', second, glyph });
  const rows: KeyEntry[][] = [
    [M('esc', 1, { icon: 'lock' }), F('switch', 'F1', '1'), F('switch', 'F2', '2'), F('switch', 'F3', '3'), F('backlight-down', 'F4'),
      F('backlight-up', 'F5'), F('dictation', 'F6'), F('emoji', 'F7'), F('screenshot', 'F8'), F('mic-mute', 'F9'), F('play-pause', 'F10'),
      F('mute', 'F11'), F('volume-down', 'F12'), F('volume-up', 'ins'), M('del', 1, { icon: 'lock' })],
    [D('"', 'é'), D('1', '!'), D('2', "'", '£'), D('3', '^', '#'), D('4', '+', '$'), D('5', '%', '½'), D('6', '&'), D('7', '/', '{'),
      D('8', '(', '['), D('9', ')', ']'), D('0', '=', '}'), D('*', '?', '\\'), D('-', '_'), M('backspace', 2)],
    [M('tab', 1.5), L('Q', '@'), L('W'), L('E', '€'), L('R'), L('T'), L('Y'), L('U'), L('I'), L('O'), L('P'), L('Ğ', '¨'), L('Ü', '~'),
      M('enter', 1.5, { skip: true })],
    [M('caps lock', 1.75), L('A', 'æ'), L('S', 'ß'), L('D'), L('F'), L('G'), L('H'), L('J'), L('K'), L('L'), L('Ş', '´'), L('İ'),
      D(',', ';', '`'), M('enter', 1.25, { skip: true })],
    [M('shift', 1.25), D('<', '>', '|'), L('Z'), L('X'), L('C'), L('V'), L('B'), L('N'), L('M'), L('Ö'), L('Ç'), D('.', ':'), M('shift', 2.75)],
    [M('ctrl'), M('fn'), S('opt', 'start'), S('cmd', 'alt', '⌘'), { label: '', u: 6, style: 'space' }, S('cmd', 'alt gr', '⌘'), S('opt', 'ctrl'),
      { label: '←', u: 1, style: 'arrow' }, { label: '↑↓', u: 1, style: 'arrow' }, { label: '→', u: 1, style: 'arrow' }],
  ];
  const keys: KeySpec[] = [];
  rows.forEach((entries, row) => {
    let cursor = -7.5 * pitch;
    const rowDepth = row === 0 ? functionDepth : capDepth;
    for (const entry of entries) {
      const { u, skip, ...rest } = entry;
      const width = u * pitch - gap, x = cursor + u * pitch / 2;
      cursor += u * pitch;
      if (skip) continue;
      if (entry.style === 'arrow') {
        // Inverted T with half-height keys: up/down stacked, left/right in the lower half.
        const half = (rowDepth - 0.01) / 2, upper = rowZ[row] - half / 2 - 0.005, lower = rowZ[row] + half / 2 + 0.005;
        const arrows: [string, number][] = entry.label === '↑↓' ? [['↑', upper], ['↓', lower]] : [[entry.label, lower]];
        for (const [label, z] of arrows) keys.push({ ...rest, label, x, z, width, depth: half, row, outline: rectOutline(width, half), minHalf: half / 2 });
        continue;
      }
      keys.push({ ...rest, x, z: rowZ[row], width, depth: rowDepth, row, outline: rectOutline(width, rowDepth), minHalf: Math.min(width, rowDepth) / 2 });
    }
  });
  // ISO enter spans the Q and A rows: 1.5u on top, 1.25u below, flush right.
  const xRight = 7.5 * pitch - gap / 2, xTop = 6 * pitch + gap / 2, xBottom = 6.25 * pitch + gap / 2;
  const zTop = rowZ[2] - capDepth / 2, zNotch = rowZ[2] + capDepth / 2, zBottom = rowZ[3] + capDepth / 2;
  const cx = (xTop + xRight) / 2, cz = (zTop + zBottom) / 2;
  keys.push({
    label: 'enter', style: 'mod', row: 2, x: cx, z: cz, width: xRight - xTop, depth: zBottom - zTop,
    outline: [[xTop, zTop], [xRight, zTop], [xRight, zBottom], [xBottom, zBottom], [xBottom, zNotch], [xTop, zNotch]].map(([px, pz]) => [px - cx, pz - cz] as [number, number]),
    minHalf: Math.min(capDepth, xRight - xBottom) / 2,
  });
  return keys;
}

const keyRadius = (key: { width: number; depth: number }) => Math.min(0.026, key.width * 0.2, key.depth * 0.22);

/**
 * Rounded contour of a counter-clockwise orthogonal polygon offset inward by `inset` (negative = outward).
 * Every corner gets `segments + 1` points so contours of different insets can be lofted together.
 */
export function orthoContour(polygon: [number, number][], inset: number, convexRadius: number, concaveRadius: number, segments = 3): Vector2[] {
  const n = polygon.length;
  const pts = polygon.map(([x, y], i) => {
    const [px, py] = polygon[(i + n - 1) % n], [nx, ny] = polygon[(i + 1) % n];
    const e1 = new Vector2(x - px, y - py).normalize(), e2 = new Vector2(nx - x, ny - y).normalize();
    return new Vector2(x + (-e1.y - e2.y) * inset, y + (e1.x + e2.x) * inset);
  });
  const out: Vector2[] = [];
  for (let i = 0; i < n; i++) {
    const p = pts[i], prev = pts[(i + n - 1) % n], next = pts[(i + 1) % n];
    const lenIn = p.distanceTo(prev), lenOut = next.distanceTo(p);
    const eIn = p.clone().sub(prev).normalize(), eOut = next.clone().sub(p).normalize();
    const convex = eIn.x * eOut.y - eIn.y * eOut.x > 0;
    const radius = clamp(convex ? convexRadius - inset : concaveRadius + inset, 0.0008, 0.49 * Math.min(lenIn, lenOut));
    const t1 = p.clone().addScaledVector(eIn, -radius), t2 = p.clone().addScaledVector(eOut, radius);
    const c = t1.clone().addScaledVector(eOut, radius);
    const a1 = Math.atan2(t1.y - c.y, t1.x - c.x);
    let delta = Math.atan2(t2.y - c.y, t2.x - c.x) - a1;
    if (delta > Math.PI) delta -= TAU;
    if (delta < -Math.PI) delta += TAU;
    for (let k = 0; k <= segments; k++) {
      const a = a1 + (delta * k) / segments;
      out.push(new Vector2(c.x + radius * Math.cos(a), c.y + radius * Math.sin(a)));
    }
  }
  return out;
}

/** Low-profile chiclet cap: drafted wall, rounded top edge, spherical (cylindrical on wide keys) dish. */
function keycap(key: KeySpec, dish: number) {
  const h = KEYBOARD.capHeight, top = 0.009, radius = keyRadius(key);
  // Dish rings round off as they shrink so square corners never crease the dish.
  const ring = (inset: number, y: number, dished = false) => orthoContour(key.outline, inset, inset + (dished ? Math.max(radius - inset, 0.6 * (key.minHalf - inset)) : radius - inset), 0.007, 3)
    .map(p => new Vector3(p.x, y, p.y));
  const rings = [ring(0, -0.014), ring(0, 0), ring(0.0035, h - 0.009), ring(0.0062, h - 0.0025), ring(top, h)];
  const deepest = key.minHalf - 0.004;
  for (const f of [0.8, 0.58, 0.36, 0.16]) rings.push(ring(top + (deepest - top) * (1 - f), h - dish * (1 - f * f), true));
  const side = loft(rings);
  flipWinding(side); // counter-clockwise rings lofted upward face inward
  const last = rings[rings.length - 1];
  const faces = ShapeUtils.triangulateShape(last.map(p => new Vector2(p.x, p.z)), []);
  const capTop = indexed(last.flatMap(p => [p.x, p.y, p.z]), faces.flat(), last.flatMap(() => [0.5, 0.5]));
  const [a, b, c] = faces[0].map(i => last[i]);
  if (b.clone().sub(a).cross(c.clone().sub(a)).y < 0) flipWinding(capTop);
  side.computeVertexNormals();
  capTop.computeVertexNormals();
  return mergeAll([side, capTop]);
}

function keyRings(keys: KeySpec[], inner: number, outer: number, y: number) {
  const positions: number[] = [], colors: number[] = [], indices: number[] = [];
  for (const key of keys) {
    const r = keyRadius(key);
    const a = orthoContour(key.outline, -inner, r, 0.007, 3), b = orthoContour(key.outline, -outer, r, 0.007, 3);
    const base = positions.length / 3, n = a.length;
    for (const p of a) { positions.push(key.x + p.x, y, key.z + p.y); colors.push(1, 1, 1); }
    for (const p of b) { positions.push(key.x + p.x, y, key.z + p.y); colors.push(0, 0, 0); }
    for (let i = 0; i < n; i++) {
      const i2 = (i + 1) % n;
      indices.push(base + i, base + i2, base + n + i2, base + i, base + n + i2, base + n + i);
    }
  }
  const geometry = indexed(positions, indices);
  geometry.setAttribute('color', new Float32BufferAttribute(colors, 3));
  geometry.computeVertexNormals();
  return geometry;
}

export function keyboardBody() {
  const { width, deckDepth, barDepth, barInset, height, cornerRadius, edge } = KEYBOARD;
  const shape = new Shape(roundedRect(width / 2 - edge, deckDepth / 2 - edge, cornerRadius - edge, 8));
  const deck = new ExtrudeGeometry(shape, { depth: DECK_MIDDLE - 2 * edge, bevelEnabled: true, bevelThickness: edge, bevelSize: edge, bevelSegments: 5, curveSegments: 4 });
  deck.rotateX(-Math.PI / 2);
  deck.translate(0, -(DECK_MIDDLE - edge), DECK_CENTER);
  // Wedge: the deck stays flat at y = 0 while the bottom follows the desk in the tilted frame.
  const position = deck.getAttribute('position');
  for (let i = 0; i < position.count; i++) if (position.getY(i) < -DECK_MIDDLE / 2) position.setY(i, keyboardFloor(position.getZ(i)) + (position.getY(i) + DECK_MIDDLE));
  // Slightly raised rear bar (the tallest part, 21 mm): level in desk space, standing on the desk.
  const barZ = -KEYBOARD.depth / 2 + barDepth / 2, worldY = height / 2 - KEYBOARD_LIFT;
  const bar = roundedBox(width - 2 * barInset, height, barDepth, 0.045, 5);
  bar.rotateX(-KEYBOARD_TILT);
  bar.translate(0, worldY * Math.cos(KEYBOARD_TILT) + barZ * Math.sin(KEYBOARD_TILT), -worldY * Math.sin(KEYBOARD_TILT) + barZ * Math.cos(KEYBOARD_TILT));
  return mergeAll([smoothExtrude(deck), bar]);
}

export function buildKeyboard() {
  const keys = keyboardKeys();
  const { width, depth, rearHeight } = KEYBOARD;
  const caps = mergeAll(keys.map(key => {
    const cap = keycap(key, 0.0042 * Math.min(1, Math.sqrt(0.16 / key.width)));
    cap.translate(key.x, 0, key.z);
    const position = cap.getAttribute('position'), uv = cap.getAttribute('uv');
    for (let i = 0; i < position.count; i++) uv.setXY(i, (position.getX(i) + width / 2) / width, 1 - (position.getZ(i) + depth / 2) / depth);
    return cap;
  }));
  // USB-C port and power switch on the rear face of the deck, under the bar.
  const port = roundedBox(0.095, 0.032, 0.02, 0.012);
  port.translate(0.92, -rearHeight * 0.55, -depth / 2 + 0.004);
  const power = roundedBox(0.075, 0.028, 0.022, 0.01);
  power.translate(1.22, -rearHeight * 0.55, -depth / 2 + 0.002);
  return {
    keys,
    body: keyboardBody(),
    caps,
    /** Additive white light leaking around each cap (vertex color = falloff). */
    glow: keyRings(keys, 0.0015, 0.021, 0.0012),
    /** Thin shadowed gap between each cap and the deck opening. */
    gap: keyRings(keys, 0, 0.0055, 0.0008),
    details: mergeAll([port, power]),
  };
}

// ---------------------------------------------------------------------------------------------
// SteelSeries Aerox 3 Wireless (120.6 x 67.0 x 38.0 mm): perforated ultralight shell.
// Local frame: front (buttons) toward -Z, left side toward -X, bottom at y = 0.
// ---------------------------------------------------------------------------------------------

export const MOUSE = { length: 1.206, width: 0.670, height: 0.380, base: 0.048, shellThickness: 0.011 } as const;
const MOUSE_HALF_LENGTH = MOUSE.length / 2;
const MOUSE_SECTION = 0.84; // superellipse exponent of the cross-section (2 / n)
export const MOUSE_WHEEL = { z: -0.335, radius: 0.098, width: 0.068, protrude: 0.03 } as const;
export const MOUSE_DPI = { z: -0.168, length: 0.085, width: 0.05 } as const;
/**
 * Three RGB zones (front = main buttons, middle, rear = palm) as equal thirds of the length, physically verified
 * on the user's mouse; they glow through the perforations and the base strip is split at the same lines.
 */
export const MOUSE_ZONE_IDS = ['front', 'middle', 'rear'] as const;
export const MOUSE_ZONE_BOUNDS = [-MOUSE.length / 6, MOUSE.length / 6] as const;
export const MOUSE_ZONE_CENTERS = [-MOUSE.length / 3, 0, MOUSE.length / 3] as const;
/** Half width of the soft colour transition at each zone boundary. */
const MOUSE_ZONE_BLEND = 0.03;
/** Large rounded diamond perforations on a 45-degree lattice (spacing = same-row pitch, radius = half diagonal). */
export const MOUSE_HOLES = { spacing: 0.075, radius: 0.027, corner: 0.007 } as const;
/** Parameter t of the transverse seam where the main buttons end. */
export const MOUSE_SEAM_T = -0.07;
/** Light strip around the base perimeter (left side, around the rear, right side). */
export const MOUSE_STRIP = { start: -0.42, bottom: 0.017, top: 0.034 } as const;

function rawMouseHalfWidth(t: number) {
  const core = 0.296 + 0.044 * smoothstep((t + 0.45) / 0.9) - 0.008 * Math.exp(-(((t + 0.12) / 0.24) ** 2));
  const m = t < 0 ? 4.2 : 2.6;
  return core * Math.max(0, 1 - Math.abs(t) ** m) ** (1 / m);
}
function rawMouseRise(t: number) {
  const core = 0.20 + 0.18 * Math.exp(-(((t - 0.18) / 0.68) ** 2)) - MOUSE.base;
  const m = t < 0 ? 6 : 2.3;
  return core * Math.max(0, 1 - Math.abs(t) ** m) ** (1 / m);
}
let mouseScaleCache: { width: number; rise: number } | null = null;
function mouseScales() {
  if (!mouseScaleCache) {
    let width = 0, rise = 0;
    for (let i = 0; i <= 4000; i++) { const t = -1 + i / 2000; width = Math.max(width, rawMouseHalfWidth(t)); rise = Math.max(rise, rawMouseRise(t)); }
    mouseScaleCache = { width: MOUSE.width / 2 / width, rise: (MOUSE.height - MOUSE.base) / rise };
  }
  return mouseScaleCache;
}
export const mouseHalfWidth = (t: number) => rawMouseHalfWidth(clamp(t, -1, 1)) * mouseScales().width;
export const mouseHeight = (t: number) => MOUSE.base + rawMouseRise(clamp(t, -1, 1)) * mouseScales().rise;

/** Shell surface: theta in [-pi/2, pi/2] across (left bottom edge -> top -> right bottom edge), t in [-1, 1] front -> rear. */
export function mousePoint(theta: number, t: number, target = new Vector3()) {
  const tt = clamp(t, -1, 1);
  return target.set(
    mouseHalfWidth(tt) * signedPow(Math.sin(theta), MOUSE_SECTION),
    MOUSE.base + (mouseHeight(tt) - MOUSE.base) * Math.abs(Math.cos(theta)) ** MOUSE_SECTION,
    tt * MOUSE_HALF_LENGTH,
  );
}

interface MouseArcs { t: Float64Array; b: Float64Array; half: Float64Array; theta: Float64Array; norm: Float64Array[] }
const ARC_ROWS = 241, ARC_COLS = 161;
let mouseArcCache: MouseArcs | null = null;

/** Arc-length tables so the perforations can be laid out in physical millimetres on the curved shell. */
function mouseArcs(): MouseArcs {
  if (mouseArcCache) return mouseArcCache;
  const t = new Float64Array(ARC_ROWS), b = new Float64Array(ARC_ROWS), half = new Float64Array(ARC_ROWS), theta = new Float64Array(ARC_COLS);
  const norm: Float64Array[] = [];
  const p = new Vector3(), q = new Vector3();
  const mid = (ARC_COLS - 1) / 2;
  for (let i = 0; i < ARC_COLS; i++) theta[i] = -Math.PI / 2 + Math.PI * i / (ARC_COLS - 1);
  for (let j = 0; j < ARC_ROWS; j++) {
    t[j] = -Math.cos(Math.PI * j / (ARC_ROWS - 1)); // dense near the blunt ends
    const row = new Float64Array(ARC_COLS);
    for (let i = mid + 1; i < ARC_COLS; i++) row[i] = row[i - 1] + mousePoint(theta[i - 1], t[j], p).distanceTo(mousePoint(theta[i], t[j], q));
    for (let i = mid - 1; i >= 0; i--) row[i] = row[i + 1] - mousePoint(theta[i + 1], t[j], p).distanceTo(mousePoint(theta[i], t[j], q));
    half[j] = row[ARC_COLS - 1];
    for (let i = 0; i < ARC_COLS; i++) row[i] = half[j] > 1e-9 ? row[i] / half[j] : (i - mid) / mid;
    norm.push(row);
    if (j > 0) b[j] = b[j - 1] + mousePoint(0, t[j - 1], p).distanceTo(mousePoint(0, t[j], q));
  }
  mouseArcCache = { t, b, half, theta, norm };
  return mouseArcCache;
}

function searchSorted(values: Float64Array, value: number) {
  let lo = 0, hi = values.length - 1;
  if (value <= values[0]) return { i: 0, f: 0 };
  if (value >= values[hi]) return { i: hi - 1, f: 1 };
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (values[m] <= value) lo = m; else hi = m; }
  return { i: lo, f: (value - values[lo]) / Math.max(1e-12, values[lo + 1] - values[lo]) };
}
function thetaInRow(arcs: MouseArcs, row: number, sigma: number) {
  const { i, f } = searchSorted(arcs.norm[row], sigma);
  return lerp(arcs.theta[i], arcs.theta[i + 1], f);
}
function rowAtT(t: number) {
  const x = Math.acos(clamp(-t, -1, 1)) / Math.PI * (ARC_ROWS - 1);
  const j = Math.min(ARC_ROWS - 2, Math.floor(x));
  return { j, f: x - j };
}
/** Centreline arc length (from the front tip) at parameter t. */
export function mouseArcAtT(t: number) {
  const arcs = mouseArcs(), { j, f } = rowAtT(t);
  return lerp(arcs.b[j], arcs.b[j + 1], f);
}
function domainInfo(b: number) {
  const arcs = mouseArcs(), { i, f } = searchSorted(arcs.b, b);
  return { j: i, f, t: lerp(arcs.t[i], arcs.t[i + 1], f), half: lerp(arcs.half[i], arcs.half[i + 1], f) };
}
/** Maps physical shell coordinates (a = signed arc across, b = arc along the top) to the surface. */
export function mouseDomainPoint(a: number, b: number, target = new Vector3()) {
  const info = domainInfo(b);
  const sigma = info.half > 1e-9 ? clamp(a / info.half, -1, 1) : 0;
  const theta = lerp(thetaInRow(mouseArcs(), info.j, sigma), thetaInRow(mouseArcs(), info.j + 1, sigma), info.f);
  return mousePoint(theta, info.t, target);
}

/** Surface point, outward normal and front-to-rear tangent at normalized arc position sigma (-1 left edge .. 1 right edge). */
export function mouseSurfaceFrame(sigma: number, t: number) {
  const { j, f } = rowAtT(t);
  const arcs = mouseArcs();
  const theta = lerp(thetaInRow(arcs, j, sigma), thetaInRow(arcs, j + 1, sigma), f);
  const e = 1e-3;
  const point = mousePoint(theta, t);
  const dTheta = mousePoint(theta + e, t).sub(mousePoint(theta - e, t));
  const dT = mousePoint(theta, t + e).sub(mousePoint(theta, t - e));
  const normal = new Vector3().crossVectors(dT, dTheta).normalize();
  if (normal.dot(point.clone().sub(new Vector3(0, MOUSE.base, point.z))) < 0) normal.negate();
  return { point, normal, tangent: dT.normalize() };
}

/** Partition of unity over the three thirds with short soft transitions at the boundaries. */
export function mouseZoneWeights(z: number, out: number[] = [0, 0, 0]) {
  const a = smoothstep((z - MOUSE_ZONE_BOUNDS[0]) / (2 * MOUSE_ZONE_BLEND) + 0.5);
  const b = smoothstep((z - MOUSE_ZONE_BOUNDS[1]) / (2 * MOUSE_ZONE_BLEND) + 0.5);
  out[0] = 1 - a; out[1] = a - b; out[2] = b;
  return out;
}
/** Per-vertex weights of the three zones; 'shell' fades towards the base and the blunt tips, 'flat' keeps full strength. */
function addZoneAttribute(geometry: BufferGeometry, mode: 'shell' | 'flat' = 'shell') {
  const position = geometry.getAttribute('position');
  const values = new Float32Array(position.count * 3), w = [0, 0, 0];
  for (let i = 0; i < position.count; i++) {
    const z = position.getZ(i);
    mouseZoneWeights(z, w);
    const ends = smoothstep((z + MOUSE_HALF_LENGTH + 0.02) / 0.3) * smoothstep((MOUSE_HALF_LENGTH + 0.03 - z) / 0.22);
    const lift = mode === 'flat' ? 1 : clamp((position.getY(i) - MOUSE.base * 0.5) / 0.2, 0.25, 1) * ends;
    values[i * 3] = w[0] * lift; values[i * 3 + 1] = w[1] * lift; values[i * 3 + 2] = w[2] * lift;
  }
  geometry.setAttribute('zone', new BufferAttribute(values, 3));
  return geometry;
}

interface Opening { a: number; b: number; halfA: number; halfB: number; radius: number }

/**
 * Lawson edge flips towards the constrained Delaunay triangulation. Earcut is valid in 2D but leaves long
 * slivers along collinear rows; mapped onto the curved shell those would cut across the dome.
 * Edges listed in `fixed` (outline and hole loops) are never flipped.
 */
function delaunayFlip(points: Vector2[], faces: number[][], fixed: Set<number>) {
  const key = (a: number, b: number) => (a < b ? a * 1048576 + b : b * 1048576 + a);
  const orient = (a: number, b: number, c: number) => (points[b].x - points[a].x) * (points[c].y - points[a].y) - (points[b].y - points[a].y) * (points[c].x - points[a].x);
  const inCircle = (a: number, b: number, c: number, d: number) => {
    const [pa, pb, pc, pd] = [points[a], points[b], points[c], points[d]];
    const ax = pa.x - pd.x, ay = pa.y - pd.y, bx = pb.x - pd.x, by = pb.y - pd.y, cx = pc.x - pd.x, cy = pc.y - pd.y;
    return (ax * ax + ay * ay) * (bx * cy - cx * by) - (bx * bx + by * by) * (ax * cy - cx * ay) + (cx * cx + cy * cy) * (ax * by - bx * ay) > 1e-14;
  };
  const tris = faces.map(face => (orient(face[0], face[1], face[2]) < 0 ? [face[0], face[2], face[1]] : [...face]));
  const edgeTris = new Map<number, number[]>();
  const link = (t: number) => { const [a, b, c] = tris[t]; for (const k of [key(a, b), key(b, c), key(c, a)]) { const list = edgeTris.get(k); if (list) list.push(t); else edgeTris.set(k, [t]); } };
  const unlink = (t: number) => { const [a, b, c] = tris[t]; for (const k of [key(a, b), key(b, c), key(c, a)]) { const list = edgeTris.get(k); if (list) { const i = list.indexOf(t); if (i >= 0) list.splice(i, 1); } } };
  tris.forEach((_, t) => link(t));
  const stack: [number, number][] = [];
  for (const [a, b, c] of tris) for (const [p, q] of [[a, b], [b, c], [c, a]] as [number, number][]) if (!fixed.has(key(p, q))) stack.push([p, q]);
  for (let guard = 0; stack.length && guard < 400000; guard++) {
    const [a, b] = stack.pop()!;
    const list = edgeTris.get(key(a, b));
    if (!list || list.length !== 2) continue;
    const [t1, t2] = list;
    const c = tris[t1].find(v => v !== a && v !== b)!, d = tris[t2].find(v => v !== a && v !== b)!;
    const [p, q, r] = tris[t1];
    if (!inCircle(p, q, r, d)) continue;
    if (orient(c, d, a) * orient(c, d, b) >= 0) continue; // quad not strictly convex
    unlink(t1); unlink(t2);
    tris[t1] = orient(c, a, d) > 0 ? [c, a, d] : [c, d, a];
    tris[t2] = orient(c, d, b) > 0 ? [c, d, b] : [c, b, d];
    link(t1); link(t2);
    for (const [u, v] of [[a, c], [c, b], [b, d], [d, a]] as [number, number][]) if (!fixed.has(key(u, v))) stack.push([u, v]);
  }
  return tris;
}

/** Hole half-diagonal: slightly smaller towards the front buttons, as on the real shell. */
const holeRadius = (t: number) => MOUSE_HOLES.radius * (0.8 + 0.2 * smoothstep((t + 0.5) / 0.6));

/** Perforated regions read from the reference photos: rear half of each main button and the whole palm/upper flanks. */
function holeAllowed(a: number, b: number, r: number) {
  const { t, half } = domainInfo(b);
  const sigma = a / Math.max(1e-6, half);
  if (Math.abs(a) > half - 0.035 - r || Math.abs(sigma) > 0.74) return false; // lower flanks stay solid
  if (sigma < -0.47 && t > -0.66 && t < 0.24) return false; // solid around the two left side buttons
  if (t >= MOUSE_SEAM_T + 0.075 && t <= 0.9) return true; // palm
  return t >= -0.5 && t <= MOUSE_SEAM_T - 0.055 && Math.abs(a) > 0.104 + r && Math.abs(sigma) < 0.62; // buttons
}

function mouseOpenings(): Opening[] {
  const bWheel0 = mouseArcAtT((MOUSE_WHEEL.z - MOUSE_WHEEL.radius - 0.012) / MOUSE_HALF_LENGTH);
  const bWheel1 = mouseArcAtT((MOUSE_WHEEL.z + MOUSE_WHEEL.radius + 0.012) / MOUSE_HALF_LENGTH);
  const bDpi0 = mouseArcAtT((MOUSE_DPI.z - MOUSE_DPI.length / 2 - 0.004) / MOUSE_HALF_LENGTH);
  const bDpi1 = mouseArcAtT((MOUSE_DPI.z + MOUSE_DPI.length / 2 + 0.004) / MOUSE_HALF_LENGTH);
  return [
    { a: 0, b: (bWheel0 + bWheel1) / 2, halfA: MOUSE_WHEEL.width / 2 + 0.012, halfB: (bWheel1 - bWheel0) / 2, radius: 0.03 },
    { a: 0, b: (bDpi0 + bDpi1) / 2, halfA: MOUSE_DPI.width / 2 + 0.004, halfB: (bDpi1 - bDpi0) / 2, radius: 0.02 },
  ];
}

/** 2D layout of the perforated shell in physical arc coordinates (a across, b along). */
export function mouseShellDomain() {
  const arcs = mouseArcs();
  const last = ARC_ROWS - 1;
  const total = arcs.b[last];
  const { spacing } = MOUSE_HOLES;
  const rowStep = spacing / 2;

  const outline: Vector2[] = [new Vector2(0, 0)];
  for (let j = 1; j < last; j++) outline.push(new Vector2(arcs.half[j], arcs.b[j]));
  outline.push(new Vector2(0, total));
  for (let j = last - 1; j >= 1; j--) outline.push(new Vector2(-arcs.half[j], arcs.b[j]));

  const holes = new Map<string, { a: number; b: number; r: number }>();
  const loops: Vector2[][] = [];
  const centers2d: Vector2[] = [];
  for (let row = 0; row * rowStep < total; row++) {
    const b = row * rowStep, shift = row % 2 ? spacing / 2 : 0, r = holeRadius(domainInfo(b).t);
    for (let col = -9; col <= 9; col++) {
      const a = col * spacing + shift;
      if (!holeAllowed(a, b, r)) continue;
      holes.set(`${row}:${col}`, { a, b, r });
      centers2d.push(new Vector2(a, b));
      loops.push(orthoContour([[r, 0], [0, r], [-r, 0], [0, -r]], 0, MOUSE_HOLES.corner * (r / MOUSE_HOLES.radius), 0, 3).map(p => new Vector2(a + p.x, b + p.y)));
    }
  }
  const hexCount = loops.length;
  const openings = mouseOpenings();
  for (const o of openings) loops.push(roundedRect(o.halfA, o.halfB, o.radius, 5).map(p => new Vector2(o.a + p.x, o.b + p.y)));

  // Interior Steiner points keep triangles small so the curved shell stays smooth between perforations.
  const nearHole = (a: number, b: number) => {
    const row0 = Math.round(b / rowStep);
    for (let row = row0 - 2; row <= row0 + 2; row++) {
      const shift = row % 2 ? spacing / 2 : 0, col0 = Math.round((a - shift) / spacing);
      for (let col = col0 - 1; col <= col0 + 1; col++) {
        const hole = holes.get(`${row}:${col}`);
        if (hole && Math.abs(a - hole.a) + Math.abs(b - hole.b) < hole.r + 0.009) return true;
      }
    }
    return openings.some(o => Math.abs(a - o.a) < o.halfA + 0.01 && Math.abs(b - o.b) < o.halfB + 0.01);
  };
  const steiner: Vector2[][] = [];
  const step = 0.03;
  for (let row = 0, b = step * 0.5; b < total - 0.006; row++, b += step * 0.866) {
    const half = domainInfo(b).half;
    for (let a = (row % 2 ? step / 2 : 0) - Math.ceil(half / step) * step; a < half; a += step) {
      if (Math.abs(a) > half - 0.009 || nearHole(a, b)) continue;
      // Deterministic jitter avoids long collinear/co-circular runs.
      const hash = Math.sin(a * 127.1 + b * 311.7) * 43758.5453;
      const jitter = (hash - Math.floor(hash) - 0.5) * 0.006;
      steiner.push([new Vector2(a + jitter, b - jitter * 0.7)]);
    }
  }
  return { outline, loops, steiner, hexCount, centers2d };
}

/** Thin dark seam ribbons following 2D paths on the shell. */
function mouseSeams(paths: [number, number][][], halfWidth: number) {
  const positions: number[] = [], indices: number[] = [];
  const p = new Vector3();
  for (const path of paths) {
    const base = positions.length / 3;
    path.forEach(([a, b], i) => {
      const [pa, pb] = path[Math.max(0, i - 1)], [na, nb] = path[Math.min(path.length - 1, i + 1)];
      const len = Math.hypot(na - pa, nb - pb) || 1, perpA = -(nb - pb) / len, perpB = (na - pa) / len;
      for (const s of [-1, 1]) {
        mouseDomainPoint(a + perpA * halfWidth * s, b + perpB * halfWidth * s, p);
        const out = p.clone().sub(new Vector3(0, MOUSE.base, p.z)).normalize();
        p.addScaledVector(out, 0.0016);
        positions.push(p.x, p.y, p.z);
      }
      if (i < path.length - 1) { const k = base + i * 2; indices.push(k, k + 1, k + 3, k, k + 3, k + 2); }
    });
  }
  return orientAway(indexed(positions, indices), m => new Vector3(0, MOUSE.base, m.z));
}

/** Base perimeter path (left side front->rear, around the back, right side rear->front) with outward 2D normals. */
function stripPath() {
  const arcs = mouseArcs();
  const start = rowAtT(MOUSE_STRIP.start).j;
  const points: Vector2[] = [];
  for (let j = start; j < ARC_ROWS; j++) points.push(new Vector2(-mouseHalfWidth(arcs.t[j]), arcs.t[j] * MOUSE_HALF_LENGTH));
  for (let j = ARC_ROWS - 2; j >= start; j--) points.push(new Vector2(mouseHalfWidth(arcs.t[j]), arcs.t[j] * MOUSE_HALF_LENGTH));
  const path = points.filter((q, i) => i === 0 || q.distanceTo(points[i - 1]) > 0.004);
  const normals = path.map((q, i) => {
    const prev = path[Math.max(0, i - 1)], next = path[Math.min(path.length - 1, i + 1)];
    const n = new Vector2(next.y - prev.y, -(next.x - prev.x)).normalize();
    return n.dot(new Vector2(q.x, q.y - 0.1)) < 0 ? n.negate() : n;
  });
  return { path, normals };
}

export function buildMouse() {
  const arcs = mouseArcs();
  const last = ARC_ROWS - 1;
  const { outline, loops, steiner, hexCount, centers2d } = mouseShellDomain();
  const wheelT = MOUSE_WHEEL.z / MOUSE_HALF_LENGTH;
  const holeLoops = [...loops, ...steiner];
  const points2d = [...outline, ...holeLoops.flat()];
  const fixed = new Set<number>();
  const fix = (start: number, count: number) => {
    for (let k = 0; k < count; k++) { const a = start + k, b = start + ((k + 1) % count); fixed.add(a < b ? a * 1048576 + b : b * 1048576 + a); }
  };
  fix(0, outline.length);
  for (let i = 0, start = outline.length; i < loops.length; start += loops[i].length, i++) fix(start, loops[i].length);
  const faces = delaunayFlip(points2d, ShapeUtils.triangulateShape(outline, holeLoops), fixed);
  const positions: number[] = [];
  const p = new Vector3();
  for (const q of points2d) { mouseDomainPoint(q.x, q.y, p); positions.push(p.x, p.y, p.z); }
  const shell = indexed(positions, faces.flat());
  orientAway(shell, m => new Vector3(0, MOUSE.base, m.z));
  addZoneAttribute(shell);

  // Real openings get inward walls so the shell reads as ~1 mm thick plastic.
  const normals = shell.getAttribute('normal');
  const wallPositions: number[] = [];
  const P = new Vector3(), Q = new Vector3(), N = new Vector3(), M = new Vector3(), C = new Vector3();
  let offset = outline.length;
  for (const loop of loops) {
    C.set(0, 0, 0);
    for (let k = 0; k < loop.length; k++) C.add(P.fromArray(positions, (offset + k) * 3));
    C.divideScalar(loop.length);
    const quads: number[][] = [];
    for (let k = 0; k < loop.length; k++) {
      const i0 = offset + k, i1 = offset + ((k + 1) % loop.length);
      const a0 = new Vector3().fromArray(positions, i0 * 3), a1 = new Vector3().fromArray(positions, i1 * 3);
      const b0 = a0.clone().addScaledVector(N.fromBufferAttribute(normals, i0), -MOUSE.shellThickness);
      const b1 = a1.clone().addScaledVector(N.fromBufferAttribute(normals, i1), -MOUSE.shellThickness);
      quads.push([a0, a1, b1, b0].flatMap(v => [v.x, v.y, v.z]));
    }
    const [a0, a1, , b0] = [0, 1, 2, 3].map(i => new Vector3().fromArray(quads[0], i * 3));
    M.subVectors(a1, a0).cross(Q.subVectors(b0, a0));
    const inward = M.dot(Q.subVectors(C, a0)) >= 0;
    for (const quad of quads) {
      const v = [0, 1, 2, 3].map(i => quad.slice(i * 3, i * 3 + 3));
      const order = inward ? [0, 1, 2, 0, 2, 3] : [0, 2, 1, 0, 3, 2];
      for (const i of order) wallPositions.push(...v[i]);
    }
    offset += loop.length;
  }
  const walls = new BufferGeometry();
  walls.setAttribute('position', new Float32BufferAttribute(wallPositions, 3));
  walls.computeVertexNormals();
  addZoneAttribute(walls);

  const holeCenters = centers2d.map(c => mouseDomainPoint(c.x, c.y));

  // Seams: single split line in front of the wheel, both sides of the wheel/DPI channel, and the V-shaped
  // transverse seam where the main buttons end.
  const [wheelOpening] = mouseOpenings();
  const bTip = mouseArcAtT(-0.996), bWheel = wheelOpening.b - wheelOpening.halfB, bSeam = mouseArcAtT(MOUSE_SEAM_T);
  const line = (a0: number, b0: number, a1: number, b1: number, steps = 32): [number, number][] =>
    Array.from({ length: steps + 1 }, (_, i) => [lerp(a0, a1, i / steps), lerp(b0, b1, i / steps)]);
  const seamHalf = domainInfo(bSeam).half * 0.8;
  const seam: [number, number][] = Array.from({ length: 49 }, (_, i) => {
    const a = lerp(-seamHalf, seamHalf, i / 48);
    return [a, bSeam + 0.024 * (1 - Math.abs(a) / seamHalf)];
  });
  const grooves = mouseSeams([line(0, bTip, 0, bWheel - 0.004, 24), line(-0.092, bWheel, -0.092, bSeam + 0.024), line(0.092, bWheel, 0.092, bSeam + 0.024), seam], 0.0032);

  // Glowing interior seen through the perforations (zone weights drive the three RGB areas).
  const innerRings: Vector3[][] = [];
  for (let j = 0; j <= 60; j++) {
    const t = lerp(-0.97, 0.97, j / 60), ring: Vector3[] = [];
    for (let i = 0; i <= 32; i++) {
      mousePoint(lerp(-Math.PI / 2 + 0.02, Math.PI / 2 - 0.02, i / 32), t, p);
      ring.push(new Vector3(p.x * 0.93, MOUSE.base * 0.9 + (p.y - MOUSE.base) * 0.84, p.z * 0.955));
    }
    innerRings.push(ring);
  }
  const inner = orientAway(loft(innerRings, { closed: false }), m => new Vector3(0, MOUSE.base, m.z));
  addZoneAttribute(inner);

  // Solid base plate below the shell edge with a small undercut chamfer.
  const plan: Vector2[] = [];
  const inset = (t: number) => Math.max(0, mouseHalfWidth(t) - 0.003);
  for (let j = 0; j <= last; j++) plan.push(new Vector2(inset(arcs.t[j]), arcs.t[j] * MOUSE_HALF_LENGTH));
  for (let j = last - 1; j >= 1; j--) plan.push(new Vector2(-inset(arcs.t[j]), arcs.t[j] * MOUSE_HALF_LENGTH));
  const planRing = (y: number, scale: number) => plan.map(q => new Vector3(q.x * scale, y, q.y * (scale + (1 - scale) * 0.6)));
  const baseWall = loft([planRing(0, 0.965), planRing(0.012, 1), planRing(MOUSE.base + 0.004, 1)]);
  orientAway(baseWall, m => new Vector3(0, m.y, 0));
  const baseTop = new ShapeGeometry(new Shape(plan.map(q => new Vector2(q.x, -q.y))));
  baseTop.rotateX(-Math.PI / 2);
  baseTop.translate(0, MOUSE.base + 0.002, 0);
  const base = mergeAll([baseWall, baseTop]);

  // Light strip around the base perimeter and its soft spill on the desk.
  const { path, normals: outward } = stripPath();
  const stripPositions: number[] = [], stripIndices: number[] = [], ringPositions: number[] = [], ringIndices: number[] = [], ringGlow: number[] = [];
  const ringRows: [number, number][] = [[0.004, 1], [0.045, 0.42], [0.1, 0.12], [0.18, 0]];
  path.forEach((q, i) => {
    const n = outward[i];
    for (const y of [MOUSE_STRIP.bottom, MOUSE_STRIP.top]) stripPositions.push(q.x + n.x * 0.0025, y, q.y + n.y * 0.0025);
    for (const [d, glow] of ringRows) { ringPositions.push(q.x + n.x * d, 0.0025, q.y + n.y * d); ringGlow.push(glow); }
    if (i < path.length - 1) {
      const k = i * 2;
      stripIndices.push(k, k + 1, k + 3, k, k + 3, k + 2);
      const rows = ringRows.length;
      for (let r = 0; r < rows - 1; r++) { const a = i * rows + r; ringIndices.push(a, a + 1, a + rows + 1, a, a + rows + 1, a + rows); }
    }
  });
  const strip = orientAway(indexed(stripPositions, stripIndices), m => new Vector3(0, m.y, 0.1));
  addZoneAttribute(strip, 'flat');
  const deskGlow = indexed(ringPositions, ringIndices);
  deskGlow.setAttribute('glow', new Float32BufferAttribute(ringGlow, 1));
  addZoneAttribute(deskGlow, 'flat');

  // Rubber scroll wheel (axis along X) with chevron tread, centred in the channel opening.
  const R = MOUSE_WHEEL.radius, w = MOUSE_WHEEL.width / 2;
  const wheel = new LatheGeometry([
    new Vector2(0.03, -w), new Vector2(R - 0.012, -w), new Vector2(R - 0.003, -w + 0.005), new Vector2(R, -w + 0.011),
    new Vector2(R, -w * 0.33), new Vector2(R, 0), new Vector2(R, w * 0.33),
    new Vector2(R, w - 0.011), new Vector2(R - 0.003, w - 0.005), new Vector2(R - 0.012, w), new Vector2(0.03, w),
  ], 120);
  const wheelPosition = wheel.getAttribute('position');
  for (let i = 0; i < wheelPosition.count; i++) {
    const x = wheelPosition.getX(i), z = wheelPosition.getZ(i), y = wheelPosition.getY(i), r = Math.hypot(x, z);
    if (r < R - 0.0005) continue;
    const tread = clamp(Math.cos(Math.atan2(z, x) * 24 + Math.abs(y) * 70) * 2.5, -1, 1) * 0.5 + 0.5;
    const scale = (R - 0.0045 + 0.0045 * tread) / r;
    wheelPosition.setXYZ(i, x * scale, y, z * scale);
  }
  wheel.rotateZ(Math.PI / 2);
  wheel.computeVertexNormals();
  const wheelCenter: [number, number, number] = [0, mouseHeight(wheelT) + MOUSE_WHEEL.protrude - R, MOUSE_WHEEL.z];

  // DPI button behind the wheel and two side buttons on the left flank.
  const dpi = mouseSurfaceFrame(0, MOUSE_DPI.z / MOUSE_HALF_LENGTH);
  const dpiButton = placeOnFrame(roundedBox(MOUSE_DPI.width, 0.03, MOUSE_DPI.length, 0.013), dpi.point, dpi.normal, dpi.tangent, -0.008);
  const sideButtons = [-0.38, -0.06].map(t => {
    const frame = mouseSurfaceFrame(-0.64, t);
    return placeOnFrame(roundedBox(0.046, 0.028, 0.17, 0.013), frame.point, frame.normal, frame.tangent, -0.005);
  });
  let portZ = -MOUSE_HALF_LENGTH;
  for (let i = 0; i < 60; i++) { const t = -1 + i * 0.0005; if (mouseHeight(t) > 0.11) { portZ = t * MOUSE_HALF_LENGTH; break; } }
  const port = roundedBox(0.085, 0.03, 0.03, 0.01);
  port.translate(0, 0.085, portZ + 0.012);

  return {
    shell, walls, grooves, inner, base, wheel, wheelCenter, strip, deskGlow,
    buttons: mergeAll([dpiButton, ...sideButtons]), port,
    holeCount: hexCount, holeCenters,
  };
}

// ---------------------------------------------------------------------------------------------
// Logitech G733 (black), published envelope 194 x 190 x 83 mm; shapes follow the reference photos.
// Local frame: ear axis X (+X = wearer's left cup with the microphone), front toward +Z, cup bottom y = 0.
// The band/strap plane is z = 0; the earcups hang slightly forward of it, as in the side photo.
// ---------------------------------------------------------------------------------------------

export const HEADSET = {
  height: 1.94, width: 1.90, depth: 0.83,
  cupHalfHeight: 0.47, cupHalfDepth: 0.395, cupCenterY: 0.47, cupCenterZ: 0.12, cupExponent: 3.4,
  cushionInner: 0.40, cushionThickness: 0.22, shellThickness: 0.245,
  armX: 0.888, armBottom: 0.34, armWidth: 0.2,
  bandHalfSpan: 0.925, bandJoinY: 1.08, bandThickness: 0.045, bandWidth: 0.26,
  strapHalfSpan: 0.82, strapEndY: 1.22, strapTop: 1.72, strapThickness: 0.024, strapWidth: 0.40,
} as const;
/** Lowest point of the suspension strap underside: where the headset rests on a stand. */
export const HEADSET_STRAP_UNDERSIDE = HEADSET.strapTop - HEADSET.strapThickness / 2;
const CUP_SEGMENTS = 80;
const CUP_SHELL_X = HEADSET.cushionInner + HEADSET.cushionThickness;
/** Angular shell profile: [x offset, contour scale, z shift] from the cushion side to the outer face. */
const CUP_PROFILE: [number, number, number][] = [
  [0, 0.955, 0], [0.012, 0.985, 0], [0.07, 1, 0], [0.135, 0.985, -0.008], [0.18, 0.93, -0.028],
  [0.212, 0.845, -0.05], [0.232, 0.76, -0.062], [0.2405, 0.62, -0.066], [0.244, 0.34, -0.066],
];
const CUP_FACE_SHIFT = -0.066;

function cupContour(phi: number, scale = 1) {
  const { cupHalfHeight: hh, cupHalfDepth: hd, cupExponent: n } = HEADSET;
  const y = hh * signedPow(Math.sin(phi), 2 / n);
  const z = hd * signedPow(Math.cos(phi), 2 / n) * (1 - 0.1 * (y / hh));
  return new Vector2(y * scale, z * scale);
}
function cupPoint(side: number, x: number, phi: number, scale: number, dz = 0) {
  const c = cupContour(phi, scale);
  return new Vector3(side * x, HEADSET.cupCenterY + c.x, HEADSET.cupCenterZ + c.y + dz);
}
function cupNormal(phi: number) {
  const a = cupContour(phi - 1e-3), b = cupContour(phi + 1e-3), c = cupContour(phi);
  const n = new Vector2(b.y - a.y, -(b.x - a.x)).normalize();
  if (n.dot(c) < 0) n.negate();
  return new Vector3(0, n.x, n.y);
}

/** Glossy angular cup shell; creased normals keep the facets crisp. */
function earcupShell(side: number) {
  const rings = CUP_PROFILE.map(([dx, s, dz]) => Array.from({ length: CUP_SEGMENTS }, (_, i) => cupPoint(side, CUP_SHELL_X + dx, (i / CUP_SEGMENTS) * TAU, s, dz)));
  const geometry = orientOutward(loft(rings, {
    start: new Vector3(side * CUP_SHELL_X, HEADSET.cupCenterY, HEADSET.cupCenterZ),
    end: new Vector3(side * (CUP_SHELL_X + HEADSET.shellThickness), HEADSET.cupCenterY, HEADSET.cupCenterZ + CUP_FACE_SHIFT),
  }));
  const creased = toCreasedNormals(geometry, 0.5);
  geometry.dispose();
  return creased;
}

function cushion(side: number) {
  const xc = HEADSET.cushionInner + HEADSET.cushionThickness / 2, ax = HEADSET.cushionThickness / 2, steps = 28;
  const rings = Array.from({ length: CUP_SEGMENTS }, (_, i) => {
    const phi = (i / CUP_SEGMENTS) * TAU;
    return Array.from({ length: steps }, (_, k) => {
      const psi = (k / steps) * TAU;
      return cupPoint(side, xc + ax * signedPow(Math.sin(psi), 0.62), phi, 0.775 + 0.19 * signedPow(Math.cos(psi), 0.62));
    });
  });
  return orientOutward(loft(rings, { wrap: true, uvScale: [3, 16] }));
}

/** Front-edge light strip: path on the shell chamfer (phi from bottom to top) and its cross-section half sizes. */
const STRIP = { x: CUP_SHELL_X + 0.158, from: -0.85, to: 0.85, halfNormal: 0.0065, halfAxis: 0.02, scale: 0.958, shift: -0.018 } as const;
/** Strip centreline (phi = 0 is the zone boundary between the top and bottom halves). */
function stripPoint(side: number, phi: number) {
  return cupPoint(side, STRIP.x, phi, STRIP.scale, STRIP.shift).addScaledVector(cupNormal(phi), 0.004);
}
/** Top-zone weight from the height above the cup centre: 4 mm soft transition at the middle of the strip. */
const stripTopWeight = (y: number) => smoothstep((y - HEADSET.cupCenterY) / 0.04 + 0.5);

/**
 * Front-edge light strip on the shell chamfer. The G733 has exactly two lighting zones (top and bottom halves,
 * physically verified, the same colour on both cups): `zone` = (top, bottom, 0) weights per vertex.
 */
function lightStrip(side: number) {
  const steps = 40;
  const profile = roundedRect(STRIP.halfNormal, STRIP.halfAxis, 0.005, 2);
  const axis = new Vector3(side, 0, 0);
  const rings: Vector3[][] = [], weights: number[][] = [];
  for (let i = 0; i <= steps; i++) {
    // Denser rings around phi = 0, where the strip height changes fastest, keep the zone edge crisp.
    const u = 2 * i / steps - 1, phi = STRIP.to * Math.sign(u) * Math.abs(u) ** 1.6, n = cupNormal(phi), p = stripPoint(side, phi);
    rings.push(profile.map(q => p.clone().addScaledVector(n, q.x).addScaledVector(axis, q.y)));
    const top = stripTopWeight(p.y);
    weights.push([top, 1 - top, 0]);
  }
  const ends = [rings[0], rings[rings.length - 1]].map(r => r.reduce((acc, v) => acc.add(v), new Vector3()).divideScalar(r.length));
  const geometry = orientOutward(loft(rings, { start: ends[0], end: ends[1] }));
  const count = geometry.getAttribute('position').count, perRing = profile.length;
  const zone = new Float32Array(count * 3);
  for (let v = 0; v < count; v++) {
    const ring = v < rings.length * perRing ? Math.floor(v / perRing) : v === rings.length * perRing ? 0 : rings.length - 1;
    zone.set(weights[ring], v * 3);
  }
  geometry.setAttribute('zone', new BufferAttribute(zone, 3));
  const center = cupPoint(side, STRIP.x, 0, 1, STRIP.shift).addScaledVector(cupNormal(0), 0.035);
  return { geometry, glow: [center.x, center.y, center.z] as [number, number, number] };
}

function archPath(halfSpan: number, joinY: number, topY: number, exponent: number, count: number, extend: number) {
  const points: Vector3[] = [];
  if (extend > 0) points.push(new Vector3(halfSpan, joinY - extend, 0));
  for (let i = 0; i <= count; i++) {
    const a = Math.PI * i / count;
    points.push(new Vector3(halfSpan * signedPow(Math.cos(a), 2 / exponent), joinY + (topY - joinY) * Math.abs(Math.sin(a)) ** (2 / exponent), 0));
  }
  if (extend > 0) points.push(new Vector3(-halfSpan, joinY - extend, 0));
  return points;
}

function micBoom() {
  const port = cupPoint(1, CUP_SHELL_X + 0.09, -0.8, 1.0);
  const curve = new CatmullRomCurve3([
    port.clone(), port.clone().add(new Vector3(-0.01, -0.03, 0.15)), port.clone().add(new Vector3(-0.07, -0.06, 0.34)),
    port.clone().add(new Vector3(-0.19, -0.06, 0.5)), port.clone().add(new Vector3(-0.33, -0.03, 0.58)),
  ], false, 'centripetal');
  const tube = new TubeGeometry(curve, 56, 0.023, 10, false);
  const up = new Vector3(0, 1, 0);
  const endTangent = curve.getTangent(1), startTangent = curve.getTangent(0);
  const capsule = new CapsuleGeometry(0.04, 0.1, 6, 16);
  capsule.applyQuaternion(new Quaternion().setFromUnitVectors(up, endTangent));
  moveTo(capsule, curve.getPoint(1).addScaledVector(endTangent, 0.035));
  const collar = new CylinderGeometry(0.046, 0.052, 0.1, 24);
  collar.applyQuaternion(new Quaternion().setFromUnitVectors(up, startTangent));
  moveTo(collar, port.clone().addScaledVector(startTangent, 0.01));
  return mergeAll([tube, capsule, collar]);
}

/** Two buttons on the rear edge of the left (+X, microphone) cup, upper (g1) and lower (g2), above the volume roller. */
export const HEADSET_BUTTONS = [{ id: 'button:g1', phi: Math.PI - 0.58 }, { id: 'button:g2', phi: Math.PI - 0.32 }] as const;
const HEADSET_BUTTON_SIZE = [0.05, 0.075, 0.04] as const;
const headsetButtonCenter = (phi: number) => cupPoint(1, CUP_SHELL_X + 0.1, phi, 1).add(new Vector3(0, 0, 0.013));

function headsetControls() {
  const roller = new CylinderGeometry(0.05, 0.05, 0.03, 28);
  roller.rotateZ(Math.PI / 2);
  moveTo(roller, cupPoint(1, CUP_SHELL_X + 0.1, Math.PI + 0.14, 1).add(new Vector3(0, 0, 0.032)));
  const [w, h, d] = HEADSET_BUTTON_SIZE;
  const buttons = HEADSET_BUTTONS.map(({ phi }) => moveTo(roundedBox(w, h, d, 0.014), headsetButtonCenter(phi)));
  return [roller, ...buttons];
}

export function buildHeadset() {
  const { armX, armBottom, armWidth, bandHalfSpan, bandJoinY, height, bandThickness, bandWidth, strapHalfSpan, strapEndY, strapTop, strapThickness, strapWidth, cupCenterY } = HEADSET;
  // One continuous frame: flat arms down the outer side of each cup, flaring into the wide arch.
  const arm = (side: number) => [0, 0.2, 0.4, 0.6, 0.8, 1].map(f => new Vector3(side * lerp(armX, bandHalfSpan - 0.004, smoothstep(f)), lerp(armBottom, bandJoinY - 0.035, f), 0));
  const bandPath = [...arm(1), ...archPath(bandHalfSpan, bandJoinY, height - bandThickness / 2, 2.4, 72, 0), ...arm(-1).reverse()];
  const frame = sweepPlanar(bandPath, k => {
    const lift = smoothstep((bandPath[k].y - bandJoinY) / (height - bandJoinY));
    return roundedRect(bandThickness / 2, lerp(armWidth, bandWidth, lift) / 2, 0.018, 3);
  });
  const strapPath = archPath(strapHalfSpan, strapEndY, strapTop, 2.0, 72, 0.03);
  const strap = sweepPlanar(strapPath, () => roundedRect(strapThickness / 2, strapWidth / 2, 0.011, 2), [3, 9]);
  const clips = [-1, 1].map(side => moveTo(roundedBox(0.075, 0.085, strapWidth + 0.012, 0.016), new Vector3(side * (strapHalfSpan + 0.04), strapEndY + 0.02, 0)));
  // Light grey adjustment tabs on the strap, near each end (as in the photos).
  const archCenter = new Vector3(0, strapEndY, 0);
  const tabs = [10, strapPath.length - 11].map(k => {
    const point = strapPath[k], outward = point.clone().sub(archCenter).setZ(0).normalize();
    return placeOnFrame(roundedBox(0.09, 0.012, 0.07, 0.005), point.clone().setZ(0.09), outward, new Vector3(0, 0, 1), strapThickness / 2 + 0.004);
  });
  // Neutral glossy plate where the product mark sits on the outer face (no logo).
  const panels = [-1, 1].map(side => {
    const x = CUP_SHELL_X + HEADSET.shellThickness - 0.001, cy = cupCenterY - 0.08, cz = HEADSET.cupCenterZ + CUP_FACE_SHIFT + 0.07;
    const points = Array.from({ length: CUP_SEGMENTS }, (_, i) => { const c = cupContour((i / CUP_SEGMENTS) * TAU, 0.34); return new Vector3(side * x, cy + c.x, cz + c.y); });
    return disc(points, new Vector3(side * x, cy, cz), new Vector3(side, 0, 0));
  });
  const cloth = [-1, 1].map(side => {
    const x = HEADSET.cushionInner + 0.1;
    const points = Array.from({ length: CUP_SEGMENTS }, (_, i) => cupPoint(side, x, (i / CUP_SEGMENTS) * TAU, 0.6));
    return disc(points, new Vector3(side * x, cupCenterY, HEADSET.cupCenterZ), new Vector3(-side, 0, 0));
  });
  const strips = [-1, 1].map(lightStrip);
  return {
    plastic: mergeAll([earcupShell(-1), earcupShell(1), frame, ...clips, ...headsetControls()]),
    panels: mergeAll(panels),
    cushions: mergeAll([cushion(-1), cushion(1)]),
    cloth: mergeAll(cloth),
    strap,
    tabs: mergeAll(tabs),
    mic: micBoom(),
    strips: strips.map(s => s.geometry),
    stripGlow: strips.map(s => s.glow),
  };
}

// ---------------------------------------------------------------------------------------------
// Minimalist headset stand: weighted base, slim upright, curved padded cradle.
// ---------------------------------------------------------------------------------------------

export const STAND = {
  baseHalfX: 0.58, baseHalfZ: 0.45, baseHeight: 0.10, baseEdge: 0.03,
  uprightWidth: 0.16, uprightDepth: 0.07,
  cradleTop: 2.15, cradleRadius: 0.62, cradleSpan: 0.6, cradleWidth: 0.40, cradleThickness: 0.06,
  padThickness: 0.014, padWidth: 0.33,
} as const;
/** Height of the headset origin (cup bottoms) when it rests on the cradle. */
export const HEADSET_ON_STAND = STAND.cradleTop - HEADSET_STRAP_UNDERSIDE;

export function buildStand() {
  const { baseHalfX, baseHalfZ, baseHeight, baseEdge: e, uprightWidth, uprightDepth, cradleTop, cradleRadius, cradleSpan, cradleWidth, cradleThickness, padThickness, padWidth } = STAND;
  const base = new ExtrudeGeometry(new Shape(superellipse(baseHalfX - e, baseHalfZ - e, 3.2, 120)), { depth: baseHeight - 2 * e, bevelEnabled: true, bevelThickness: e, bevelSize: e, bevelSegments: 4, curveSegments: 4 });
  base.rotateX(-Math.PI / 2);
  base.translate(0, e, 0);
  const cradleBottom = cradleTop - padThickness - cradleThickness;
  const upright = roundedBox(uprightWidth, cradleBottom - baseHeight + 0.04, uprightDepth, 0.026, 4);
  upright.translate(0, (cradleBottom + baseHeight) / 2, 0);
  const centerline = cradleTop - padThickness - cradleThickness / 2;
  const arc = (radius: number, span: number) => Array.from({ length: 41 }, (_, i) => {
    const a = Math.PI / 2 + span - (2 * span * i) / 40;
    return new Vector3(radius * Math.cos(a), centerline - cradleRadius + radius * Math.sin(a), 0);
  });
  const cradle = sweepPlanar(arc(cradleRadius, cradleSpan), () => roundedRect(cradleThickness / 2, cradleWidth / 2, 0.022, 3));
  const pad = sweepPlanar(arc(cradleRadius + cradleThickness / 2 + padThickness / 2, cradleSpan - 0.05), () => roundedRect(padThickness / 2, padWidth / 2, 0.006, 2));
  return { metal: mergeAll([smoothExtrude(base), upright, cradle]), pad };
}

/** Large rounded desk slab; the top surface is y = 0. */
export function deskGeometry(width: number, depth: number) {
  const geometry = roundedBox(width, 0.4, depth, 0.06, 4);
  geometry.translate(0, -0.2, 0);
  return geometry;
}

// ---------------------------------------------------------------------------------------------
// Selectable parts. Everything is expressed in each device's placement-local frame (keyboard tilt and the
// headset's height on the stand applied) and derived from the same constants that build the meshes.
// ---------------------------------------------------------------------------------------------

type Triple = [number, number, number];
export interface PartBox { min: Triple; max: Triple }
export interface PartAnchor { position: Triple; normal: Triple }
export interface PartRegion {
  id: string;
  /** Highlight volumes; a hit on a `pick` surface inside a box grown by `pickPad` selects the part. */
  boxes: PartBox[];
  pickPad: Triple;
  /** Mesh surface tags (mesh userData.surface) that can select the part, and those that glow when highlighted. */
  pick: readonly string[];
  glow: readonly string[];
  /** Marker anchors just off the surface with outward normals (one per cup for the mirrored headset zones). */
  anchors: PartAnchor[];
  /** Volume framed by the part close-up: the part plus a little context. */
  view: PartBox;
}

const triple = (v: Vector3): Triple => [v.x, v.y, v.z];
function boxOf(points: Vector3[]): PartBox {
  const min = new Vector3(Infinity, Infinity, Infinity), max = new Vector3(-Infinity, -Infinity, -Infinity);
  for (const p of points) { min.min(p); max.max(p); }
  return { min: triple(min), max: triple(max) };
}
function cornersOf(min: Triple, max: Triple, matrix?: Matrix4) {
  const points: Vector3[] = [];
  for (const x of [min[0], max[0]]) for (const y of [min[1], max[1]]) for (const z of [min[2], max[2]]) {
    const p = new Vector3(x, y, z);
    points.push(matrix ? p.applyMatrix4(matrix) : p);
  }
  return points;
}

/** F-row keys with their own settings (F4..F12 and the volume-up/ins key left of del). */
export const KEYBOARD_PART_KEYS: Record<string, string> = {
  'key:F4': 'F4', 'key:F5': 'F5', 'key:F6': 'F6', 'key:F7': 'F7', 'key:F8': 'F8', 'key:F9': 'F9',
  'key:F10': 'F10', 'key:F11': 'F11', 'key:F12': 'F12', 'key:INS': 'ins',
};

function keyboardPartRegions(): PartRegion[] {
  const frame = keyboardFrame(new Matrix4());
  const up = new Vector3(0, 1, 0).transformDirection(frame);
  const row = keyboardKeys().filter(key => key.row === 0);
  const h = KEYBOARD.capHeight, pad = KEYBOARD.gap / 2;
  return Object.entries(KEYBOARD_PART_KEYS).map(([id, sub]) => {
    const key = row.find(entry => entry.sub === sub);
    if (!key) throw new Error(`Missing F-row key ${sub}`);
    const w = key.width / 2, d = key.depth / 2;
    return {
      id,
      boxes: [boxOf(cornersOf([key.x - w, 0, key.z - d], [key.x + w, h, key.z + d], frame))],
      pickPad: [pad, 0.01, pad], pick: ['keys', 'frame'], glow: ['keys'],
      // Near the rear edge of the cap so the marker does not hide the F-row icon.
      anchors: [{ position: triple(new Vector3(key.x, h + 0.006, key.z - d + 0.024).applyMatrix4(frame)), normal: triple(up) }],
      // About three keys either side and the neighbouring rows, centred on the key.
      view: boxOf(cornersOf([key.x - 0.6, 0, key.z - d - 0.1], [key.x + 0.6, 0.04, key.z + d + 0.1], frame)),
    };
  });
}

function mousePartRegions(): PartRegion[] {
  const dpi = mouseSurfaceFrame(0, MOUSE_DPI.z / MOUSE_HALF_LENGTH);
  const button = cornersOf([-MOUSE_DPI.width / 2, -0.015, -MOUSE_DPI.length / 2], [MOUSE_DPI.width / 2, 0.015, MOUSE_DPI.length / 2],
    surfaceMatrix(dpi.point, dpi.normal, dpi.tangent, -0.008));
  const regions: PartRegion[] = [{
    id: 'dpi', boxes: [boxOf(button)], pickPad: [0.012, 0.01, 0.012], pick: ['buttons', 'shell', 'walls'], glow: ['buttons'],
    anchors: [{ position: triple(dpi.point.clone().addScaledVector(dpi.normal, 0.02)), normal: triple(dpi.normal) }],
    view: { min: [-0.2, 0.12, -0.44], max: [0.2, MOUSE.height + 0.01, 0.02] },
  }];
  const ends = [-MOUSE_HALF_LENGTH - 0.01, MOUSE_ZONE_BOUNDS[0], MOUSE_ZONE_BOUNDS[1], MOUSE_HALF_LENGTH + 0.01];
  const half = MOUSE.width / 2 + 0.01;
  MOUSE_ZONE_IDS.forEach((zone, i) => {
    const marker = mouseSurfaceFrame(-0.3, MOUSE_ZONE_CENTERS[i] / MOUSE_HALF_LENGTH);
    regions.push({
      id: `zone:${zone}`, boxes: [{ min: [-half, 0, ends[i]], max: [half, MOUSE.height + 0.01, ends[i + 1]] }], pickPad: [0, 0, 0],
      pick: ['shell', 'walls', 'inner', 'strip'], glow: ['shell', 'walls', 'inner', 'strip', 'base'],
      anchors: [{ position: triple(marker.point.clone().addScaledVector(marker.normal, 0.014)), normal: triple(marker.normal) }],
      view: { min: [-half, 0, Math.max(ends[i] - 0.05, ends[0])], max: [half, MOUSE.height + 0.01, Math.min(ends[i + 1] + 0.05, ends[3])] },
    });
  });
  return regions;
}

function headsetPartRegions(): PartRegion[] {
  const lift = new Vector3(0, HEADSET_ON_STAND, 0), axis = new Vector3();
  const stripHalf = (side: number, from: number, to: number) => {
    const points: Vector3[] = [];
    axis.set(side, 0, 0);
    for (let i = 0; i <= 24; i++) {
      const phi = lerp(from, to, i / 24), n = cupNormal(phi), p = stripPoint(side, phi).add(lift);
      for (const a of [-1, 1]) for (const b of [-1, 1]) points.push(p.clone().addScaledVector(n, a * STRIP.halfNormal).addScaledVector(axis, b * STRIP.halfAxis));
    }
    return boxOf(points);
  };
  const { cupCenterY, cupHalfHeight, cupCenterZ } = HEADSET;
  const front = cupCenterZ + HEADSET.cupHalfDepth + 0.04;
  // The boxes reach 2 mm past the strip sides so the shell right next to the lit strip glows too (a halo that
  // stays visible over any zone colour); they are not grown vertically, which keeps the top/bottom seam exact.
  const halo = ({ min, max }: PartBox): PartBox => ({ min: [min[0] - 0.02, min[1], min[2] - 0.02], max: [max[0] + 0.02, max[1], max[2] + 0.02] });
  const zone = (id: string, from: number, to: number, anchorPhi: number, view: PartBox): PartRegion => ({
    id, boxes: [-1, 1].map(side => halo(stripHalf(side, from, to))), pickPad: [0.015, 0.004, 0.015], pick: ['strip', 'plastic'], glow: ['strip', 'plastic'],
    anchors: [-1, 1].map(side => {
      const n = cupNormal(anchorPhi);
      return { position: triple(stripPoint(side, anchorPhi).add(lift).addScaledVector(n, 0.03)), normal: triple(n) };
    }),
    view,
  });
  const width = CUP_SHELL_X + HEADSET.shellThickness + 0.02;
  const [bw, bh, bd] = HEADSET_BUTTON_SIZE;
  return [
    zone('zone:top', 0, STRIP.to, 0.45, { min: [-width, lift.y + cupCenterY - 0.06, cupCenterZ - 0.1], max: [width, lift.y + cupCenterY + cupHalfHeight + 0.03, front] }),
    zone('zone:bottom', STRIP.from, 0, -0.45, { min: [-width, lift.y, cupCenterZ - 0.1], max: [width, lift.y + cupCenterY + 0.06, front] }),
    ...HEADSET_BUTTONS.map(({ id, phi }) => {
      const c = headsetButtonCenter(phi).add(lift), n = cupNormal(phi);
      return {
        id, boxes: [boxOf(cornersOf([c.x - bw / 2, c.y - bh / 2, c.z - bd / 2], [c.x + bw / 2, c.y + bh / 2, c.z + bd / 2]))],
        pickPad: [0.02, 0.012, 0.02] as Triple, pick: ['plastic'], glow: ['plastic'],
        anchors: [{ position: triple(c.clone().addScaledVector(n, 0.045)), normal: triple(n) }],
        // The button centred with the neighbouring button, the roller and the cup rim around it.
        view: { min: [c.x - 0.26, c.y - 0.26, c.z - 0.2], max: [c.x + 0.26, c.y + 0.26, c.z + 0.2] } as PartBox,
      };
    }),
  ];
}

let partCache: Record<string, PartRegion[]> | null = null;
/** Selectable parts of a device except 'body' (everything else), in placement-local coordinates. */
export function devicePartRegions(id: 'mx-keys-mini' | 'g733' | 'aerox-3'): readonly PartRegion[] {
  partCache ??= { 'mx-keys-mini': keyboardPartRegions(), 'g733': headsetPartRegions(), 'aerox-3': mousePartRegions() };
  return partCache[id];
}

/**
 * Part under a pointer hit: the first region whose pick surfaces include the hit mesh's surface tag and whose
 * padded box contains the placement-local point; otherwise 'body'. Allocation-free (runs on pointer moves).
 */
export function pickPart(regions: readonly PartRegion[], surface: string | undefined, p: { x: number; y: number; z: number }) {
  if (surface) {
    for (const region of regions) {
      if (!region.pick.includes(surface)) continue;
      const [px, py, pz] = region.pickPad;
      for (const { min, max } of region.boxes) {
        if (p.x >= min[0] - px && p.x <= max[0] + px && p.y >= min[1] - py && p.y <= max[1] + py && p.z >= min[2] - pz && p.z <= max[2] + pz) return region.id;
      }
    }
  }
  return 'body';
}
