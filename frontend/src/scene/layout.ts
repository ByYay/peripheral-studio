import { Vector3 } from 'three';

// Desk arrangement and camera framing. Pure math (no relative imports) so `node --test` can load it.
// Bounds are local to each placement and are verified against the generated geometry in scene.test.mjs.

export type DeviceId = 'mx-keys-mini' | 'g733' | 'aerox-3';
type Triple = [number, number, number];

export interface Placement {
  position: Triple;
  yaw: number;
  /** Local AABB of everything rendered for the device (the G733 includes its stand). */
  bounds: { min: Triple; max: Triple };
  /** Local AABB used for the close-up framing. */
  focusBounds: { min: Triple; max: Triple };
  /** Close-up camera direction relative to the device yaw, and fill ratio of the free area. */
  focus: { yaw: number; pitch: number; margin: number };
  /** Hover lift in model units (100 mm). */
  lift: number;
}

/** Height of the headset origin (cup bottoms) on the stand cradle; equals STAND.cradleTop - HEADSET_STRAP_UNDERSIDE. */
export const HEADSET_ON_STAND = 0.442;

export const PLACEMENTS: Record<DeviceId, Placement> = {
  'mx-keys-mini': {
    position: [0.05, 0, 0.55], yaw: 0,
    bounds: { min: [-1.48, 0, -0.665], max: [1.48, 0.21, 0.665] },
    focusBounds: { min: [-1.48, 0, -0.665], max: [1.48, 0.21, 0.665] },
    focus: { yaw: 0, pitch: 0.82, margin: 0.84 }, lift: 0.025,
  },
  'g733': {
    position: [-2.62, 0, -0.42], yaw: 0.42,
    bounds: { min: [-0.95, 0, -0.455], max: [0.95, 2.385, 1.13] },
    focusBounds: { min: [-0.95, 0.42, -0.31], max: [0.95, 2.385, 1.13] },
    focus: { yaw: 0.48, pitch: 0.28, margin: 0.8 }, lift: 0.05,
  },
  'aerox-3': {
    position: [2.36, 0, 0.6], yaw: 0.1,
    bounds: { min: [-0.336, 0, -0.604], max: [0.336, 0.381, 0.604] },
    focusBounds: { min: [-0.336, 0, -0.604], max: [0.336, 0.381, 0.604] },
    focus: { yaw: -0.5, pitch: 0.62, margin: 0.74 }, lift: 0.03,
  },
};

export const DEVICE_ORDER: DeviceId[] = ['g733', 'mx-keys-mini', 'aerox-3'];
export const isDeviceId = (id: string | null | undefined): id is DeviceId => !!id && id in PLACEMENTS;

/**
 * Clickable parts per device (UI contract). 'body' is everything else on the device (general settings).
 * aerox-3: DPI button behind the wheel and the three lighting thirds; mx-keys-mini: F4..F12 and INS (volume up,
 * left of del); g733: the two lighting halves (same colour on both cups) and the two left-cup rear-edge buttons.
 */
export const DEVICE_PARTS = {
  'aerox-3': ['dpi', 'zone:front', 'zone:middle', 'zone:rear', 'body'],
  'mx-keys-mini': ['key:F4', 'key:F5', 'key:F6', 'key:F7', 'key:F8', 'key:F9', 'key:F10', 'key:F11', 'key:F12', 'key:INS', 'body'],
  'g733': ['zone:top', 'zone:bottom', 'button:g1', 'button:g2', 'body'],
} as const satisfies Record<DeviceId, readonly string[]>;
export type DevicePart<T extends DeviceId = DeviceId> = (typeof DEVICE_PARTS)[T][number];
export const isDevicePart = (id: DeviceId, part: string | null | undefined): part is string =>
  !!part && (DEVICE_PARTS[id] as readonly string[]).includes(part);

export interface CameraPose { target: Vector3; yaw: number; pitch: number; distance: number }

/** World-space corners of a placement-local AABB (8 points appended to `out`). */
export function localCorners(id: DeviceId, box: { min: Triple; max: Triple }, out: Vector3[] = []) {
  const placement = PLACEMENTS[id];
  const { min, max } = box;
  const c = Math.cos(placement.yaw), s = Math.sin(placement.yaw);
  for (const x of [min[0], max[0]]) for (const y of [min[1], max[1]]) for (const z of [min[2], max[2]]) {
    out.push(new Vector3(placement.position[0] + x * c + z * s, placement.position[1] + y, placement.position[2] - x * s + z * c));
  }
  return out;
}

/** World-space corners of a device AABB (8 points appended to `out`). */
export function deviceCorners(id: DeviceId, which: 'bounds' | 'focusBounds', out: Vector3[] = []) {
  return localCorners(id, PLACEMENTS[id][which], out);
}

/** Camera offset direction (from target to camera) for yaw/pitch. */
export function orbitDirection(yaw: number, pitch: number, out = new Vector3()) {
  return out.set(Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), Math.cos(yaw) * Math.cos(pitch));
}

/**
 * Frames `points` for a perspective camera looking along (yaw, pitch). `fovY` is the vertical field of view of
 * the free area and `aspect` its width/height. `margin` is the largest normalized extent (1 = edge to edge).
 * The target is re-centred so the projected bounds are centred in the free area.
 */
export function fitPose(points: Vector3[], yaw: number, pitch: number, fovY: number, aspect: number, margin: number): CameraPose {
  const forward = orbitDirection(yaw, pitch).negate();
  const right = new Vector3(Math.cos(yaw), 0, -Math.sin(yaw));
  const up = new Vector3().crossVectors(right, forward).normalize();
  const tanY = Math.tan(fovY / 2) * margin, tanX = tanY * aspect;
  const min = new Vector3(Infinity, Infinity, Infinity), max = new Vector3(-Infinity, -Infinity, -Infinity);
  for (const p of points) { min.min(p); max.max(p); }
  const target = min.clone().add(max).multiplyScalar(0.5);
  const q = new Vector3();
  let distance = 1;
  for (let iteration = 0; iteration < 6; iteration++) {
    distance = 0.1;
    for (const p of points) {
      q.subVectors(p, target);
      const x = q.dot(right), y = q.dot(up), z = q.dot(forward);
      distance = Math.max(distance, Math.abs(x) / tanX - z, Math.abs(y) / tanY - z);
    }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of points) {
      q.subVectors(p, target);
      const depth = distance + q.dot(forward);
      const sx = q.dot(right) / depth, sy = q.dot(up) / depth;
      minX = Math.min(minX, sx); maxX = Math.max(maxX, sx); minY = Math.min(minY, sy); maxY = Math.max(maxY, sy);
    }
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    if (Math.abs(cx) < 1e-5 && Math.abs(cy) < 1e-5) break;
    target.addScaledVector(right, cx * distance).addScaledVector(up, cy * distance);
  }
  return { target, yaw, pitch, distance };
}

/** Normalized screen extents (-1..1 = free-area edges) of `points` seen from `pose`; used by tests. */
export function projectExtents(points: Vector3[], pose: CameraPose, fovY: number, aspect: number) {
  const forward = orbitDirection(pose.yaw, pose.pitch).negate();
  const right = new Vector3(Math.cos(pose.yaw), 0, -Math.sin(pose.yaw));
  const up = new Vector3().crossVectors(right, forward).normalize();
  const tanY = Math.tan(fovY / 2), tanX = tanY * aspect;
  let x = 0, y = 0;
  const q = new Vector3();
  for (const p of points) {
    q.subVectors(p, pose.target);
    const depth = pose.distance + q.dot(forward);
    x = Math.max(x, Math.abs(q.dot(right) / depth / tanX));
    y = Math.max(y, Math.abs(q.dot(up) / depth / tanY));
  }
  return { x, y };
}

export const SCENE_FOV = (30 * Math.PI) / 180;

/** Desk overview: fixed cinematic angle, a little higher on narrow/portrait free areas. */
export function deskPose(aspect: number, ids: DeviceId[] = DEVICE_ORDER, fovY = SCENE_FOV) {
  const narrow = Math.min(1, Math.max(0, (1.7 - aspect) / 1.15));
  // Portrait: swing to the right so the left-to-right row recedes in depth and fills the taller frame.
  const portrait = Math.min(1, Math.max(0, (1.1 - aspect) / 0.6));
  const points = (ids.length ? ids : DEVICE_ORDER).flatMap(id => deviceCorners(id, 'bounds'));
  return fitPose(points, -0.05 + 0.8 * portrait, 0.33 + 0.2 * narrow, fovY, aspect, 0.9);
}

/** Device close-up. */
export function focusPose(id: DeviceId, aspect: number, fovY = SCENE_FOV) {
  const placement = PLACEMENTS[id];
  const portrait = Math.min(1, Math.max(0, (1.2 - aspect) / 0.7));
  return fitPose(deviceCorners(id, 'focusBounds'), placement.yaw + placement.focus.yaw, placement.focus.pitch + 0.1 * portrait, fovY, aspect, placement.focus.margin);
}

/** Part close-up direction relative to the device yaw, and fill ratio of the free area. */
export interface PartView { yaw: number; pitch: number; margin: number }

/** Keyed by part id, or by its prefix ('key', 'button') when all parts of that kind share a view. */
const PART_VIEWS: Record<DeviceId, Record<string, PartView>> = {
  // From above and slightly in front of the F-row, the selected key centred with its neighbours as context.
  'mx-keys-mini': { key: { yaw: 0, pitch: 0.98, margin: 0.9 } },
  'aerox-3': {
    // Almost straight down onto the DPI button with the wheel and the button seams around it.
    dpi: { yaw: -0.3, pitch: 1.15, margin: 0.78 },
    'zone:front': { yaw: -0.85, pitch: 0.78, margin: 0.64 },
    'zone:middle': { yaw: -0.6, pitch: 0.8, margin: 0.64 },
    'zone:rear': { yaw: -0.35, pitch: 0.62, margin: 0.66 },
  },
  'g733': {
    // Front view of both cups (the two zones are mirrored on the cups); view from behind the left cup for its buttons.
    'zone:top': { yaw: 0.3, pitch: 0.3, margin: 0.9 },
    'zone:bottom': { yaw: 0.3, pitch: 0.14, margin: 0.9 },
    button: { yaw: 2.6, pitch: 0.3, margin: 0.7 },
  },
};

export function partView(id: DeviceId, part: string): PartView {
  const views = PART_VIEWS[id];
  const { yaw, pitch, margin } = PLACEMENTS[id].focus;
  return views[part] ?? views[part.split(':')[0]] ?? { yaw, pitch, margin };
}

/** Part close-up framing the placement-local `view` box of the part (see devicePartRegions in geometry.ts). */
export function partPose(id: DeviceId, part: string, view: { min: Triple; max: Triple }, aspect: number, fovY = SCENE_FOV) {
  const placement = PLACEMENTS[id], v = partView(id, part);
  const portrait = Math.min(1, Math.max(0, (1.2 - aspect) / 0.7));
  return fitPose(localCorners(id, view), placement.yaw + v.yaw, v.pitch + 0.08 * portrait, fovY, aspect, v.margin);
}
