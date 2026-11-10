import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Box3, Color, DoubleSide, Matrix4, Mesh, MeshBasicMaterial, Raycaster, Vector3 } from 'three';
import {
  buildHeadset, buildKeyboard, buildMouse, buildStand, devicePartRegions, HEADSET, HEADSET_BUTTONS, HEADSET_ON_STAND, HEADSET_STRAP_UNDERSIDE, KEYBOARD,
  KEYBOARD_PART_KEYS, keyboardFrame, keyboardKeys, meshVolume, MOUSE, MOUSE_HOLES, MOUSE_ZONE_BOUNDS, MOUSE_ZONE_CENTERS, MOUSE_ZONE_IDS, mouseShellDomain,
  mouseZoneWeights, pickPart, STAND,
} from './geometry.ts';
import {
  DEVICE_ORDER, DEVICE_PARTS, HEADSET_ON_STAND as LAYOUT_HEADSET_ON_STAND, PLACEMENTS, SCENE_FOV, deskPose, deviceCorners, focusPose, isDevicePart,
  localCorners, orbitDirection, partPose, partView, projectExtents,
} from './layout.ts';
import { backlightLevel, brightness, effectRate, LIGHT_ZONES, lightingAmount, resolveEffect, safeColor, sampleLighting, zoneColor } from './lighting.ts';

const finite = geometry => ['position', 'normal'].every(name => !geometry.getAttribute(name) || [...geometry.getAttribute(name).array].every(Number.isFinite));
const box = (...geometries) => {
  const result = new Box3();
  for (const geometry of geometries) { geometry.computeBoundingBox(); result.union(geometry.boundingBox); }
  return result;
};
const near = (actual, expected, tolerance, label) => assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: ${actual.toFixed(4)} vs ${expected} ±${tolerance}`);
const disposeAll = parts => Object.values(parts).flat().forEach(value => value?.isBufferGeometry && value.dispose());
const keyboardWorld = parts => { const frame = keyboardFrame(new Matrix4()); return [parts.body.clone().applyMatrix4(frame), parts.caps.clone().applyMatrix4(frame)]; };

/** Axis-aligned rectangles covering a key outline (the ISO enter is two). */
const keyRects = key => {
  const xs = [...new Set(key.outline.map(([x]) => x))].sort((a, b) => a - b), zs = [...new Set(key.outline.map(([, z]) => z))].sort((a, b) => a - b);
  if (key.outline.length === 4) return [{ x0: key.x + xs[0], x1: key.x + xs[1], z0: key.z + zs[0], z1: key.z + zs[1] }];
  // L shape: [xTop, xBottom, xRight] x [zTop, zNotch, zBottom]
  return [{ x0: key.x + xs[0], x1: key.x + xs[2], z0: key.z + zs[0], z1: key.z + zs[1] }, { x0: key.x + xs[1], x1: key.x + xs[2], z0: key.z + zs[1], z1: key.z + zs[2] }];
};

test('MX Keys Mini TR-Q: six rows, ISO enter, Turkish letters and the photo legends', () => {
  const keys = keyboardKeys();
  assert.equal(keys.length, 80, 'key count of the TR-Q MX Keys Mini');
  const perRow = [0, 1, 2, 3, 4, 5].map(row => keys.filter(key => key.row === row).length);
  assert.deepEqual(perRow, [15, 14, 14, 13, 13, 11], 'keys per row (F-row, numbers, Q + enter, A, Z, bottom)');
  assert.equal(keys.filter(key => key.style === 'space').length, 1, 'one space bar');
  assert.ok(!keys.some(key => /^(home|end|pgup|pgdn|num)/i.test(key.label)), 'no navigation cluster or numpad');
  const labels = new Set(keys.map(key => key.label));
  for (const letter of ['Ğ', 'Ü', 'Ş', 'İ', 'Ö', 'Ç', 'I', 'Q', 'W']) assert.ok(labels.has(letter), `has ${letter}`);
  assert.deepEqual(keys.filter(key => key.row === 2 && key.style === 'letter').map(key => key.label), ['Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P', 'Ğ', 'Ü']);
  assert.deepEqual(keys.filter(key => key.row === 3 && key.style === 'letter').map(key => key.label), ['A', 'S', 'D', 'F', 'G', 'H', 'J', 'K', 'L', 'Ş', 'İ']);
  assert.deepEqual(keys.filter(key => key.row === 4 && key.style === 'letter').map(key => key.label), ['Z', 'X', 'C', 'V', 'B', 'N', 'M', 'Ö', 'Ç']);
  const find = label => keys.find(key => key.label === label && key.row > 0) ?? keys.find(key => key.label === label);
  assert.equal(find('Q').alt, '@'); assert.equal(find('E').alt, '€'); assert.equal(find('3').shift, '^'); assert.equal(find('3').alt, '#');
  assert.equal(find('"').shift, 'é'); assert.equal(find('*').shift, '?'); assert.equal(find('<').shift, '>');
  assert.deepEqual(keys.filter(key => key.row === 0).map(key => key.icon), ['lock', 'switch', 'switch', 'switch', 'backlight-down', 'backlight-up',
    'dictation', 'emoji', 'screenshot', 'mic-mute', 'play-pause', 'mute', 'volume-down', 'volume-up', 'lock']);
  // ISO enter: one L-shaped key spanning the Q and A rows, wider on top.
  const enter = find('enter');
  assert.equal(enter.outline.length, 6);
  const [top, bottom] = keyRects(enter);
  assert.ok(top.x1 - top.x0 > bottom.x1 - bottom.x0 && enter.depth > (KEYBOARD.pitch - KEYBOARD.gap) * 1.9, 'tall ISO enter, wider top');
  assert.equal(find('shift').width < 0.25, true, 'short ISO left shift');
  // Inverted T: all four arrows are half height; up and down share a column.
  const arrows = keys.filter(key => key.style === 'arrow');
  assert.equal(arrows.length, 4);
  assert.ok(arrows.every(key => key.depth < 0.08) && find('↑').x === find('↓').x && find('↑').z < find('↓').z);
  // Inside the deck, no overlaps (ISO enter tested as its two rectangles).
  const deckRear = KEYBOARD.depth / 2 - KEYBOARD.deckDepth;
  const rects = keys.flatMap(key => keyRects(key).map(rect => ({ ...rect, label: key.label })));
  for (const r of rects) {
    assert.ok(r.x0 > -KEYBOARD.width / 2 + 0.04 && r.x1 < KEYBOARD.width / 2 - 0.04, `${r.label} inside x`);
    assert.ok(r.z0 > deckRear + 0.05 && r.z1 < KEYBOARD.depth / 2 - 0.05, `${r.label} inside z`);
  }
  for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) {
    const a = rects[i], b = rects[j];
    if (a.label === 'enter' && b.label === 'enter') continue;
    const gapX = Math.max(a.x0 - b.x1, b.x0 - a.x1), gapZ = Math.max(a.z0 - b.z1, b.z0 - a.z1);
    assert.ok(gapX > 0.005 || gapZ > 0.005, `${a.label} overlaps ${b.label}`);
  }
});

test('MX Keys Mini: real 296 x 132 x 21 mm envelope with a flat bottom and finite, outward geometry', () => {
  const parts = buildKeyboard();
  const [body, caps] = keyboardWorld(parts);
  const b = box(body, caps);
  near(b.max.x - b.min.x, 2.96, 0.01, 'width');
  near(b.max.z - b.min.z, 1.32, 0.02, 'depth');
  near(b.max.y, 0.21, 0.012, 'height');
  assert.ok(b.min.y > -0.002 && b.min.y < 0.002, `bottom on desk ${b.min.y}`);
  for (const geometry of [parts.body, parts.caps, parts.glow, parts.gap, parts.details]) assert.ok(finite(geometry));
  assert.ok(meshVolume(parts.body) > 0, 'body winds outward');
  // Key tops face up (dished but upward).
  const position = parts.caps.getAttribute('position'), normal = parts.caps.getAttribute('normal');
  let up = 0, count = 0;
  for (let i = 0; i < position.count; i++) if (position.getY(i) > KEYBOARD.capHeight - 0.006) { up += normal.getY(i); count++; }
  assert.ok(up / count > 0.9, `cap tops face up (${up / count})`);
  // Cap walls face outward (away from the key they belong to).
  const keys = keyboardKeys();
  let outward = 0, walls = 0;
  for (let i = 0; i < position.count; i++) {
    if (Math.abs(position.getY(i)) > 0.002) continue;
    const x = position.getX(i), z = position.getZ(i);
    const key = keys.find(k => keyRects(k).some(r => x > r.x0 - 0.002 && x < r.x1 + 0.002 && z > r.z0 - 0.002 && z < r.z1 + 0.002));
    if (!key || key.outline.length !== 4) continue;
    walls++;
    if ((x - key.x) * normal.getX(i) + (z - key.z) * normal.getZ(i) > 0) outward++;
  }
  assert.ok(walls > 500 && outward / walls > 0.97, `cap walls outward ${outward}/${walls}`);
  // Raised rear bar: the tallest part sits behind the function row.
  const [bodyWorld] = keyboardWorld(parts);
  const p = bodyWorld.getAttribute('position');
  let tallestZ = 0, tallest = -1;
  for (let i = 0; i < p.count; i++) if (p.getY(i) > tallest) { tallest = p.getY(i); tallestZ = p.getZ(i); }
  assert.ok(tallestZ < -KEYBOARD.depth / 2 + KEYBOARD.barDepth, 'rear bar is the highest point');
  bodyWorld.dispose();
  // Backlight halo carries a falloff in vertex colors.
  const colors = [...parts.glow.getAttribute('color').array];
  assert.ok(colors.includes(1) && colors.includes(0));
  [body, caps].forEach(g => g.dispose());
  disposeAll(parts);
});

test('Aerox 3 Wireless: real 120.6 x 67.0 x 38.0 mm envelope', () => {
  const parts = buildMouse();
  const b = box(parts.shell, parts.base);
  near(b.max.z - b.min.z, MOUSE.length, 0.006, 'length');
  near(b.max.x - b.min.x, MOUSE.width, 0.006, 'width');
  near(b.max.y - b.min.y, MOUSE.height, 0.004, 'height');
  assert.ok(Math.abs(b.min.y) < 0.002);
  const wheel = box(parts.wheel);
  assert.ok(parts.wheelCenter[1] + wheel.max.y <= MOUSE.height, 'wheel stays below the hump');
  for (const geometry of [parts.shell, parts.walls, parts.inner, parts.base, parts.wheel, parts.buttons, parts.grooves, parts.strip, parts.deskGlow]) assert.ok(finite(geometry));
  disposeAll(parts);
});

test('Aerox 3 Wireless: large rounded-diamond perforations are real openings in a sound, smooth shell', () => {
  const parts = buildMouse();
  assert.ok(parts.holeCount > 100 && parts.holeCount < 320, `hole count ${parts.holeCount}`);
  const { loops, centers2d } = mouseShellDomain();
  for (let i = 0; i < parts.holeCount; i += 17) {
    const c = centers2d[i], l1 = loops[i].map(p => Math.abs(p.x - c.x) + Math.abs(p.y - c.y));
    assert.ok(Math.max(...l1) <= MOUSE_HOLES.radius + 1e-6 && Math.min(...l1) > 0.6 * Math.max(...l1), 'rounded diamond');
  }
  const front = centers2d.reduce((best, p) => (p.y < best.y ? p : best)), rear = centers2d.reduce((best, p) => (p.y > best.y ? p : best));
  const size = c => { const i = centers2d.indexOf(c); return Math.max(...loops[i].map(p => p.x)) - c.x; };
  assert.ok(size(front) < size(rear), 'holes shrink towards the front buttons');
  const material = new MeshBasicMaterial({ side: DoubleSide });
  const mesh = new Mesh(parts.shell, material);
  mesh.updateMatrixWorld();
  const down = new Vector3(0, -1, 0);
  const top = parts.holeCenters.reduce((best, p) => (p.y > best.y ? p : best));
  assert.equal(new Raycaster(new Vector3(top.x, 1, top.z), down).intersectObject(mesh).length, 0, 'ray passes through a perforation');
  assert.ok(new Raycaster(new Vector3(0.2, 1, -0.56), down).intersectObject(mesh).length > 0, 'solid front button tip');
  assert.ok(new Raycaster(new Vector3(0.07, 1, -0.2), down).intersectObject(mesh).length > 0, 'solid wheel channel beside the DPI button');
  // Triangulation: area matches the 2D layout (no slivers cutting across the dome) and every edge is manifold.
  const position = parts.shell.getAttribute('position'), index = parts.shell.getIndex();
  const a = new Vector3(), b = new Vector3(), c = new Vector3();
  const edges = new Map();
  let area = 0;
  for (let i = 0; i < index.count; i += 3) {
    const ids = [index.getX(i), index.getX(i + 1), index.getX(i + 2)];
    a.fromBufferAttribute(position, ids[0]); b.fromBufferAttribute(position, ids[1]); c.fromBufferAttribute(position, ids[2]);
    area += b.clone().sub(a).cross(c.clone().sub(a)).length() / 2;
    for (const [p, q] of [[ids[0], ids[1]], [ids[1], ids[2]], [ids[2], ids[0]]]) { const k = p < q ? `${p}_${q}` : `${q}_${p}`; edges.set(k, (edges.get(k) ?? 0) + 1); }
  }
  const domain = mouseShellDomain();
  const polygonArea = points => Math.abs(points.reduce((sum, p, i) => { const q = points[(i + 1) % points.length]; return sum + p.x * q.y - q.x * p.y; }, 0) / 2);
  const flatArea = polygonArea(domain.outline) - domain.loops.reduce((sum, loop) => sum + polygonArea(loop), 0);
  near(area / flatArea, 1, 0.08, 'shell area / layout area');
  const counts = [...edges.values()];
  assert.ok(counts.every(count => count <= 2), 'manifold');
  assert.equal(counts.filter(count => count === 1).length, domain.outline.length + domain.loops.reduce((sum, loop) => sum + loop.length, 0), 'only outline and hole edges are open');
  // Three RGB zones ordered front -> rear, weights carried by the geometry.
  assert.deepEqual([...MOUSE_ZONE_CENTERS].sort((x, y) => x - y), [...MOUSE_ZONE_CENTERS]);
  for (const geometry of [parts.shell, parts.walls, parts.inner, parts.strip, parts.deskGlow]) assert.equal(geometry.getAttribute('zone').itemSize, 3);
  // Base perimeter light strip: from below the side buttons, along both sides and around the rear.
  const strip = box(parts.strip);
  assert.ok(strip.min.x < -0.3 && strip.max.x > 0.3 && strip.max.z > 0.59 && strip.min.z > -0.35, 'strip wraps sides and rear only');
  assert.ok(strip.max.y < MOUSE.base && strip.min.y > 0.005, 'strip sits on the base, below the shell');
  assert.equal(parts.deskGlow.getAttribute('glow').itemSize, 1);
  material.dispose();
  disposeAll(parts);
});

test('G733: published 194 x 190 x 83 mm envelope, front-edge gradient strips and the boom mic on the left cup', () => {
  const parts = buildHeadset();
  const b = box(parts.plastic, parts.cushions);
  near(b.max.x - b.min.x, 1.90, 0.02, 'width');
  near(b.max.y - b.min.y, 1.94, 0.01, 'height');
  near(b.max.z - b.min.z, 0.83, 0.03, 'depth');
  assert.ok(meshVolume(parts.cushions) > 0 && meshVolume(parts.strap) > 0, 'closed parts wind outward');
  assert.equal(parts.strips.length, 2);
  const strips = parts.strips.map(geometry => box(geometry));
  assert.ok(strips.every(s => s.min.z > 0.3 && s.max.y - s.min.y > 0.6), 'tall strips along the front edges');
  assert.ok(strips.some(s => s.max.x < 0) && strips.some(s => s.min.x > 0), 'one strip per earcup');
  for (const geometry of parts.strips) {
    const zone = geometry.getAttribute('zone'), position = geometry.getAttribute('position');
    let topIndex = 0, bottomIndex = 0;
    for (let i = 0; i < position.count; i++) { if (position.getY(i) > position.getY(topIndex)) topIndex = i; if (position.getY(i) < position.getY(bottomIndex)) bottomIndex = i; }
    assert.ok(zone.getX(topIndex) > 0.99 && zone.getY(bottomIndex) > 0.99, 'two hardware zones: top half, bottom half');
  }
  assert.ok(box(parts.strap).max.z - box(parts.strap).min.z > 0.38, 'wide elastic strap');
  assert.ok(box(parts.tabs).max.y > HEADSET.strapEndY, 'adjustment tabs on the strap');
  const mic = box(parts.mic);
  assert.ok(mic.min.x > 0.2 && mic.max.z > 0.9, 'mic leaves the left (+X) cup towards the mouth');
  for (const geometry of [parts.plastic, parts.cushions, parts.strap, parts.mic, ...parts.strips]) assert.ok(finite(geometry));
  disposeAll(parts);
});

test('Stand supports the headset: strap rests on the cradle, cups hang clear of the upright and base', () => {
  const headset = buildHeadset(), stand = buildStand();
  near(HEADSET_ON_STAND, LAYOUT_HEADSET_ON_STAND, 1e-6, 'layout uses the geometric rest height');
  near(box(stand.pad).max.y, STAND.cradleTop, 0.002, 'pad top is the cradle top');
  const strap = headset.strap.getAttribute('position');
  let lowest = Infinity;
  for (let i = 0; i < strap.count; i++) if (Math.abs(strap.getX(i)) < 0.03) lowest = Math.min(lowest, strap.getY(i));
  near(lowest + HEADSET_ON_STAND, STAND.cradleTop, 0.003, 'strap underside touches the pad');
  near(lowest, HEADSET_STRAP_UNDERSIDE, 0.003, 'strap underside constant');
  // Cradle never pokes through the strap: compare per x-bin.
  const strapMin = new Map();
  for (let i = 0; i < strap.count; i++) { const k = Math.round(strap.getX(i) / 0.02); strapMin.set(k, Math.min(strapMin.get(k) ?? Infinity, strap.getY(i) + HEADSET_ON_STAND)); }
  for (const geometry of [stand.pad, stand.metal]) {
    const p = geometry.getAttribute('position');
    for (let i = 0; i < p.count; i++) {
      const k = Math.round(p.getX(i) / 0.02);
      if (strapMin.has(k) && Math.abs(p.getZ(i)) < HEADSET.strapWidth / 2) assert.ok(p.getY(i) <= strapMin.get(k) + 0.003, `cradle below strap at x=${p.getX(i).toFixed(3)}`);
    }
  }
  assert.ok(STAND.padWidth <= HEADSET.strapWidth, 'pad narrower than the strap');
  assert.ok(HEADSET.cushionInner > STAND.uprightWidth / 2 + 0.15, 'cushions clear the upright');
  assert.ok(HEADSET_ON_STAND > STAND.baseHeight + 0.3, 'cups hang above the base');
  disposeAll(headset); disposeAll(stand);
});

test('Placements: declared bounds wrap the generated geometry tightly and footprints do not collide', () => {
  const keyboard = buildKeyboard(), mouse = buildMouse(), headset = buildHeadset(), stand = buildStand();
  const [body, caps] = keyboardWorld(keyboard);
  const lifted = [headset.plastic, headset.cushions, headset.strap, headset.mic, headset.tabs].map(g => g.clone().translate(0, HEADSET_ON_STAND, 0));
  const actual = {
    'mx-keys-mini': box(body, caps),
    'aerox-3': box(mouse.shell, mouse.base),
    'g733': box(stand.metal, stand.pad, ...lifted),
  };
  for (const id of DEVICE_ORDER) {
    const { min, max } = PLACEMENTS[id].bounds, real = actual[id];
    for (let axis = 0; axis < 3; axis++) {
      const key = ['x', 'y', 'z'][axis];
      assert.ok(min[axis] <= real.min[key] + 0.003 && max[axis] >= real.max[key] - 0.003, `${id} bounds contain geometry on ${key}`);
      assert.ok(real.min[key] - min[axis] < 0.04 && max[axis] - real.max[key] < 0.04, `${id} bounds tight on ${key}`);
    }
  }
  const footprint = (id, which = 'bounds') => { const b = new Box3().setFromPoints(deviceCorners(id, which)); b.min.y = 0; b.max.y = 0.01; return b; };
  assert.ok(!footprint('mx-keys-mini').intersectsBox(footprint('aerox-3')), 'mouse beside the keyboard');
  const standBase = new Box3(new Vector3(-STAND.baseHalfX, 0, -STAND.baseHalfZ), new Vector3(STAND.baseHalfX, 0.01, STAND.baseHalfZ));
  const { position, yaw } = PLACEMENTS.g733;
  const corners = [];
  for (const x of [-STAND.baseHalfX, STAND.baseHalfX]) for (const z of [-STAND.baseHalfZ, STAND.baseHalfZ]) corners.push(new Vector3(position[0] + x * Math.cos(yaw) + z * Math.sin(yaw), 0.005, position[2] - x * Math.sin(yaw) + z * Math.cos(yaw)));
  assert.ok(!new Box3().setFromPoints(corners).intersectsBox(footprint('mx-keys-mini')), 'stand base clear of the keyboard');
  assert.ok(standBase.isEmpty() === false);
  [body, caps, ...lifted].forEach(g => g.dispose());
  [keyboard, mouse, headset, stand].forEach(disposeAll);
});

test('Camera framing fits the desk and every close-up inside the free area at any aspect', () => {
  for (const aspect of [0.42, 0.62, 1, 1.4, 1.9, 2.6]) {
    const desk = deskPose(aspect);
    const points = DEVICE_ORDER.flatMap(id => deviceCorners(id, 'bounds'));
    const fit = projectExtents(points, desk, SCENE_FOV, aspect);
    assert.ok(fit.x <= 0.901 && fit.y <= 0.901, `desk inside at ${aspect}: ${JSON.stringify(fit)}`);
    assert.ok(Math.max(fit.x, fit.y) > 0.88, `desk uses the frame at ${aspect}`);
    assert.ok([desk.target.x, desk.target.y, desk.target.z, desk.distance].every(Number.isFinite));
    for (const id of DEVICE_ORDER) {
      const pose = focusPose(id, aspect);
      const extents = projectExtents(deviceCorners(id, 'focusBounds'), pose, SCENE_FOV, aspect);
      const margin = PLACEMENTS[id].focus.margin;
      assert.ok(extents.x <= margin + 0.001 && extents.y <= margin + 0.001, `${id} inside at ${aspect}`);
      assert.ok(Math.max(extents.x, extents.y) > margin - 0.02, `${id} fills the free area at ${aspect}`);
      assert.ok(pose.distance > 0.5 && pose.pitch > 0.05 && pose.pitch < 1.3);
    }
  }
});

test('lighting validates color, clamps brightness and makes zero completely dark', () => {
  assert.equal(brightness(0), 0);
  assert.equal(brightness(200), 1);
  assert.equal(brightness(-2), 0);
  assert.equal(brightness(Number.NaN), 0.65);
  assert.equal(safeColor('#AAbb22'), '#AAbb22');
  assert.equal(safeColor('url(https://invalid)'), '#72e8cc');
  const color = new Color();
  for (const mode of ['static', 'breathing', 'spectrum']) {
    assert.equal(sampleLighting(color, '#ff0000', 0, mode, 2), 0);
    assert.ok(sampleLighting(color, '#ff0000', 1, mode, 2) <= 1);
  }
  sampleLighting(color, '#ff0000', 1, 'static', 100);
  assert.equal(color.getHexString(), 'ff0000');
  sampleLighting(color, '#ff0000', 1, 'spectrum', 3);
  assert.notEqual(color.getHexString(), 'ff0000');
});

test('lighting safety: unacknowledged real devices stay dark; keyboard backlight is clamped and off by default', () => {
  assert.equal(lightingAmount({ connection: 'connected', settings: {} }), 0);
  assert.equal(lightingAmount({ connection: 'disconnected', settings: {} }), 0);
  assert.equal(lightingAmount({ connection: 'connected', settings: { lightingBrightness: 40 } }), 0.4);
  assert.equal(lightingAmount({ connection: 'simulated', settings: {} }), 0.65);
  assert.equal(lightingAmount({ connection: 'simulated', settings: { lightingBrightness: 0 } }), 0);
  assert.equal(backlightLevel(undefined), 0);
  assert.equal(backlightLevel(0), 0);
  assert.equal(backlightLevel(60), 0.6);
  assert.equal(backlightLevel(250), 1);
  assert.equal(backlightLevel(Number.NaN), 0);
});

// ------------------------------------------------------------------------------------------------
// Clickable parts
// ------------------------------------------------------------------------------------------------

/** Placement-local meshes tagged like models.tsx (surface tags drive part picking). */
function deviceMeshes(id) {
  const material = new MeshBasicMaterial({ side: DoubleSide });
  const mesh = (geometry, surface, matrix) => { const m = new Mesh(matrix ? geometry.clone().applyMatrix4(matrix) : geometry, material); m.userData.surface = surface; m.updateMatrixWorld(); return m; };
  if (id === 'mx-keys-mini') {
    const parts = buildKeyboard(), frame = keyboardFrame(new Matrix4());
    return { parts, material, meshes: [mesh(parts.body, 'frame', frame), mesh(parts.caps, 'keys', frame), mesh(parts.details, 'frame', frame)] };
  }
  if (id === 'aerox-3') {
    const parts = buildMouse(), wheel = new Matrix4().makeTranslation(...parts.wheelCenter);
    return { parts, material, meshes: [mesh(parts.shell, 'shell'), mesh(parts.walls, 'walls'), mesh(parts.inner, 'inner'), mesh(parts.base, 'base'),
      mesh(parts.wheel, 'wheel', wheel), mesh(parts.buttons, 'buttons'), mesh(parts.port, 'base'), mesh(parts.strip, 'strip')] };
  }
  const parts = buildHeadset(), stand = buildStand(), up = new Matrix4().makeTranslation(0, HEADSET_ON_STAND, 0);
  return { parts: { ...parts, ...stand }, material, meshes: [
    mesh(parts.plastic, 'plastic', up), mesh(parts.panels, 'plastic', up), mesh(parts.cushions, 'cushion', up), mesh(parts.cloth, 'cushion', up),
    mesh(parts.strap, 'strap', up), mesh(parts.tabs, 'strap', up), mesh(parts.mic, 'mic', up), ...parts.strips.map(s => mesh(s, 'strip', up)),
    mesh(stand.metal, 'stand'), mesh(stand.pad, 'stand'),
  ] };
}
const firstHit = (meshes, origin, direction) => new Raycaster(origin, direction.clone().normalize(), 0, 10).intersectObjects(meshes, false)[0];
const pickAlong = (id, meshes, origin, direction) => {
  const hit = firstHit(meshes, origin, direction);
  return hit ? pickPart(devicePartRegions(id), hit.object.userData.surface, hit.point) : null;
};

test('DEVICE_PARTS: exact part ids per device, each non-body part has a geometric region', () => {
  assert.deepEqual(DEVICE_PARTS, {
    'aerox-3': ['dpi', 'zone:front', 'zone:middle', 'zone:rear', 'body'],
    'mx-keys-mini': ['key:F4', 'key:F5', 'key:F6', 'key:F7', 'key:F8', 'key:F9', 'key:F10', 'key:F11', 'key:F12', 'key:INS', 'body'],
    'g733': ['zone:top', 'zone:bottom', 'button:g1', 'button:g2', 'body'],
  });
  for (const id of DEVICE_ORDER) assert.deepEqual(devicePartRegions(id).map(region => region.id), DEVICE_PARTS[id].filter(part => part !== 'body'));
  assert.ok(isDevicePart('g733', 'zone:top') && isDevicePart('aerox-3', 'body') && !isDevicePart('g733', 'dpi') && !isDevicePart('mx-keys-mini', null));
  // INS is the 15th F-row key's neighbour: the volume-up key directly left of del.
  const row = keyboardKeys().filter(key => key.row === 0);
  const ins = row.findIndex(key => key.sub === KEYBOARD_PART_KEYS['key:INS']);
  assert.equal(row[ins].icon, 'volume-up'); assert.equal(row[ins + 1].label, 'del'); assert.equal(ins, 13);
  // G733 buttons: rear edge (-z) of the left (+X, microphone) cup, g1 above g2.
  const [g1, g2] = HEADSET_BUTTONS.map(button => devicePartRegions('g733').find(region => region.id === button.id).boxes[0]);
  assert.ok(g1.min[0] > 0.5 && g2.min[0] > 0.5 && g1.max[2] < 0 && g2.max[2] < 0, 'left cup rear edge');
  assert.ok(g1.min[1] > g2.max[1] - 0.02 && (g1.min[1] + g1.max[1]) / 2 > (g2.min[1] + g2.max[1]) / 2, 'g1 upper, g2 lower');
});

test('Parts: every part has boxes/anchors inside the device bounds and its real mesh picks it', () => {
  const origin = new Vector3(), direction = new Vector3();
  for (const id of DEVICE_ORDER) {
    const { min, max } = PLACEMENTS[id].bounds;
    const inside = (p, tolerance) => p.every((v, axis) => v >= min[axis] - tolerance && v <= max[axis] + tolerance);
    const { parts, meshes, material } = deviceMeshes(id);
    for (const region of devicePartRegions(id)) {
      assert.ok(region.boxes.length >= 1 && region.boxes.length <= 2 && region.anchors.length >= 1, `${region.id} has boxes and anchors`);
      for (const box of region.boxes) assert.ok(inside(box.min, 0.01) && inside(box.max, 0.01) && box.min.every((v, k) => v < box.max[k]), `${id} ${region.id} box inside bounds`);
      for (const anchor of region.anchors) {
        assert.ok(inside(anchor.position, 0.06), `${id} ${region.id} anchor inside bounds`);
        near(Math.hypot(...anchor.normal), 1, 1e-6, `${region.id} anchor normal`);
        // Ray from outside along the anchor normal: the first mesh hit must classify as this part.
        origin.fromArray(anchor.position).addScaledVector(direction.fromArray(anchor.normal), 0.5);
        assert.equal(pickAlong(id, meshes, origin, direction.negate()), region.id, `${id}: ray onto ${region.id} anchor`);
      }
    }
    // Everything else is 'body'.
    const down = new Vector3(0, -1, 0);
    if (id === 'mx-keys-mini') {
      const frame = keyboardFrame(new Matrix4());
      for (const label of ['', 'Q', 'esc', 'del', 'enter']) {
        const key = keyboardKeys().find(k => k.label === label && (label !== '' || k.style === 'space'));
        assert.equal(pickAlong(id, meshes, new Vector3(key.x, 1, key.z).applyMatrix4(frame), down), 'body', `${label || 'space'} is body`);
      }
      const f1 = keyboardKeys().find(k => k.sub === 'F1');
      assert.equal(pickAlong(id, meshes, new Vector3(f1.x, 1, f1.z).applyMatrix4(frame), down), 'body', 'Easy-Switch F1 is body');
    } else if (id === 'aerox-3') {
      assert.equal(pickAlong(id, meshes, new Vector3(0, 1, parts.wheelCenter[2]), down), 'body', 'wheel is body');
      assert.equal(pickAlong(id, meshes, new Vector3(-1, 0.008, 0), new Vector3(1, 0, 0)), 'body', 'base side below the strip is body');
      assert.equal(pickAlong(id, meshes, new Vector3(-1, 0.025, 0.4), new Vector3(1, 0, 0)), 'zone:rear', 'strip in the rear third');
      assert.equal(pickAlong(id, meshes, new Vector3(-1, 0.025, -0.23), new Vector3(1, 0, 0)), 'zone:front', 'strip in the front third');
    } else {
      assert.equal(pickAlong(id, meshes, new Vector3(-2, HEADSET_ON_STAND + HEADSET.cupCenterY, HEADSET.cupCenterZ), new Vector3(1, 0, 0)), 'body', 'right cup outer face is body');
      assert.equal(pickAlong(id, meshes, new Vector3(0, 3, 0), down), 'body', 'strap/band is body');
    }
    material.dispose();
    disposeAll(parts);
  }
});

test('Zone overrides map to the right meshes: Aerox thirds (shell, strip, desk glow) and G733 top/bottom on both cups', () => {
  assert.deepEqual([...MOUSE_ZONE_IDS], [...LIGHT_ZONES['aerox-3']]);
  assert.deepEqual(devicePartRegions('aerox-3').filter(r => r.id.startsWith('zone:')).map(r => r.id), LIGHT_ZONES['aerox-3'].map(zone => `zone:${zone}`));
  near(MOUSE_ZONE_BOUNDS[1] - MOUSE_ZONE_BOUNDS[0], MOUSE.length / 3, 1e-9, 'middle third');
  const mouse = buildMouse();
  const regions = devicePartRegions('aerox-3');
  for (const [name, geometry] of [['shell', mouse.shell], ['strip', mouse.strip], ['deskGlow', mouse.deskGlow], ['inner', mouse.inner]]) {
    const zone = geometry.getAttribute('zone'), position = geometry.getAttribute('position');
    const seen = new Set();
    for (let i = 0; i < position.count; i++) {
      const z = position.getZ(i);
      if (MOUSE_ZONE_BOUNDS.some(b => Math.abs(z - b) < 0.035)) continue; // soft transition
      const w = [zone.getX(i), zone.getY(i), zone.getZ(i)], strongest = w.indexOf(Math.max(...w));
      const third = z < MOUSE_ZONE_BOUNDS[0] ? 0 : z < MOUSE_ZONE_BOUNDS[1] ? 1 : 2;
      assert.equal(strongest, third, `${name} vertex z=${z.toFixed(3)} lit by its own third`);
      const region = regions.find(r => r.id === `zone:${MOUSE_ZONE_IDS[third]}`);
      if (Math.abs(z) < MOUSE.length / 2) assert.ok(z >= region.boxes[0].min[2] && z <= region.boxes[0].max[2], `${name} z=${z.toFixed(3)} inside ${region.id}`);
      if (name === 'strip' || name === 'deskGlow') near(w[0] + w[1] + w[2], 1, 1e-5, `${name} full strength`);
      seen.add(strongest);
    }
    assert.equal(seen.size, 3, `${name} shows all three zones`);
  }
  assert.deepEqual(mouseZoneWeights(MOUSE_ZONE_CENTERS[0]), [1, 0, 0]);
  assert.deepEqual(mouseZoneWeights(MOUSE_ZONE_CENTERS[2]), [0, 0, 1]);
  disposeAll(mouse);

  assert.deepEqual([...LIGHT_ZONES.g733], ['top', 'bottom']);
  const headset = buildHeadset();
  for (const geometry of headset.strips) {
    const zone = geometry.getAttribute('zone'), position = geometry.getAttribute('position');
    for (let i = 0; i < position.count; i++) {
      const y = position.getY(i) - HEADSET.cupCenterY;
      assert.equal(zone.getZ(i), 0, 'third channel unused');
      near(zone.getX(i) + zone.getY(i), 1, 1e-6, 'top + bottom');
      if (y > 0.06) assert.ok(zone.getX(i) > 0.99, 'upper half = top zone');
      if (y < -0.06) assert.ok(zone.getY(i) > 0.99, 'lower half = bottom zone');
    }
  }
  // Both cups carry the same top/bottom split (one hardware colour per zone on both cups) and the picking boxes mirror it.
  const [top, bottom] = devicePartRegions('g733');
  for (const region of [top, bottom]) {
    assert.equal(region.boxes.length, 2, `${region.id}: one box per cup`);
    assert.ok(region.boxes[0].max[0] < 0 && region.boxes[1].min[0] > 0, 'right (-X) and left (+X) cup');
    near(region.boxes[0].min[1], region.boxes[1].min[1], 1e-6, 'mirrored');
  }
  assert.ok(top.boxes[0].min[1] >= bottom.boxes[0].max[1] - 0.01 && top.boxes[0].min[1] > bottom.boxes[0].min[1] + 0.3, 'top above bottom (sub-mm overlap at the seam)');
  disposeAll(headset);

  // Colours: zone overrides win, invalid entries fall back to lightingColor; effects resolve per device.
  const settings = { lightingColor: '#112233', lightingZones: { front: '#ff0000', rear: 'red', top: '#00FF00' } };
  assert.equal(zoneColor(settings, 'front'), '#ff0000');
  assert.equal(zoneColor(settings, 'middle'), '#112233');
  assert.equal(zoneColor(settings, 'rear'), '#112233');
  assert.equal(zoneColor(settings, 'top'), '#00FF00');
  assert.equal(zoneColor({}, 'top'), '#72e8cc');
  assert.equal(zoneColor({ lightingZones: { toString: '#ffffff' } }, 'toString'), '#ffffff');
  assert.equal(zoneColor({ lightingColor: '#123456', lightingZones: {} }, 'constructor'), '#123456', 'no prototype lookups');
  assert.equal(resolveEffect('rainbow', 'static'), 'spectrum');
  assert.equal(resolveEffect('breathing', 'spectrum'), 'breathing');
  assert.equal(resolveEffect('static', 'spectrum'), 'static');
  assert.equal(resolveEffect(undefined, 'breathing'), 'breathing');
  assert.equal(resolveEffect('rainbow', 'breathing', true), 'static', 'reduced motion');
  // Speeds follow the device periods: breathing 6/4/2 s, rainbow 12/6/3 s.
  assert.deepEqual(['slow', 'normal', 'fast', undefined].map(speed => effectRate('breathing', speed)), [2 / 3, 1, 2, 1]);
  assert.deepEqual(['slow', 'normal', 'fast'].map(speed => effectRate('spectrum', speed)), [0.5, 1, 2]);
  const color = new Color();
  for (const effect of ['static', 'breathing', 'spectrum']) assert.equal(sampleLighting(color, new Color(zoneColor(settings, 'front')), 0, effect, 3, 0.4), 0, 'brightness 0 is dark per zone');
  assert.equal(lightingAmount({ connection: 'connected', settings: { lightingZones: { front: '#ff0000' } } }), 0, 'zones do not light an unacknowledged real device');
});

test('Part close-ups: each part is framed closer than its device and fits the free area at any aspect', () => {
  const free = (points, pose, aspect) => projectExtents(points, pose, SCENE_FOV, aspect);
  for (const aspect of [0.42, 0.62, 1, 1.4, 1.9, 2.6]) {
    for (const id of DEVICE_ORDER) {
      const device = focusPose(id, aspect);
      const bounds = new Box3().setFromPoints(deviceCorners(id, 'bounds'));
      for (const region of devicePartRegions(id)) {
        const pose = partPose(id, region.id, region.view, aspect), { margin } = partView(id, region.id);
        const view = free(localCorners(id, region.view), pose, aspect);
        assert.ok(view.x <= margin + 0.001 && view.y <= margin + 0.001, `${id} ${region.id} view inside at ${aspect}`);
        assert.ok(Math.max(view.x, view.y) > margin - 0.02, `${id} ${region.id} view fills the free area at ${aspect}`);
        for (const box of region.boxes) {
          const part = free(localCorners(id, box), pose, aspect);
          assert.ok(part.x <= margin + 0.001 && part.y <= margin + 0.001, `${id} ${region.id} part inside at ${aspect}`);
        }
        assert.ok(pose.distance < device.distance * 0.92, `${id} ${region.id} closer than the device framing at ${aspect}`);
        assert.ok(pose.pitch > 0.06 && pose.pitch < 1.35 && [pose.target.x, pose.target.y, pose.target.z, pose.distance].every(Number.isFinite));
        const camera = pose.target.clone().addScaledVector(orbitDirection(pose.yaw, pose.pitch), pose.distance);
        assert.ok(!bounds.containsPoint(camera) && camera.y > 0.05, `${id} ${region.id} camera outside the device, above the desk`);
      }
    }
  }
  // Representative framings: DPI from above, F7 centred on its own key, G733 buttons from behind the left cup.
  const dpi = partPose('aerox-3', 'dpi', devicePartRegions('aerox-3')[0].view, 1.2);
  assert.ok(dpi.pitch > 1.0, 'DPI seen from above');
  const f7 = devicePartRegions('mx-keys-mini').find(region => region.id === 'key:F7');
  const f7Pose = partPose('mx-keys-mini', 'key:F7', f7.view, 1.2);
  const keyCenter = localCorners('mx-keys-mini', f7.boxes[0]).reduce((sum, p) => sum.add(p), new Vector3()).divideScalar(8);
  assert.ok(Math.hypot(keyCenter.x - f7Pose.target.x, keyCenter.z - f7Pose.target.z) < 0.15, 'F7 near the optical axis');
  const g1 = devicePartRegions('g733').find(region => region.id === 'button:g1');
  const g1Pose = partPose('g733', 'button:g1', g1.view, 1.2);
  const local = orbitDirection(g1Pose.yaw - PLACEMENTS.g733.yaw, g1Pose.pitch);
  assert.ok(local.z < -0.5 && local.x > 0.3, 'G733 buttons viewed from behind the left cup');
});
