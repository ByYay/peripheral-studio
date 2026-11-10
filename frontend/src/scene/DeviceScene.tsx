import { Component, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import type { ThreeEvent } from '@react-three/fiber';
import { BufferGeometry, Float32BufferAttribute, Fog, MeshBasicMaterial, PMREMGenerator, PerspectiveCamera, SphereGeometry, Vector3 } from 'three';
import type { Group, Object3D, Points, SpotLight } from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import type { Device } from '../types';
import { deskGeometry, devicePartRegions, HEADSET_ON_STAND, pickPart } from './geometry';
import type { PartRegion } from './geometry';
import { DEVICE_ORDER, PLACEMENTS, SCENE_FOV, deskPose, focusPose, isDeviceId, isDevicePart, orbitDirection, partPose } from './layout';
import type { CameraPose, DeviceId } from './layout';
import { contactMaterial, createHighlight, deskMaterial, HIGHLIGHT_SLOTS, markerMaterial } from './materials';
import type { FloatUniform, Highlight } from './materials';
import { Aerox3, G733, HeadsetStand, MXKeysMini, noRaycast } from './models';
import { createContactTexture, createDeskTexture } from './textures';
import { resolveEffect } from './lighting';
import type { PreviewAnimation } from './lighting';

export interface SceneLabels {
  canvas: string;
  unavailable: string;
  webgl: string;
}

/** CSS pixels of the canvas covered by UI (right panel / bottom sheet). */
export interface SceneInset { right: number; bottom: number }

export interface DeviceSceneProps {
  /** Settings already resolved for display by the UI. */
  devices: Device[];
  /** null = desk overview; device id = focus view. */
  selectedId: string | null;
  /** Pointer click on a device (drags are ignored). In focus view with onSelectPart, only other devices call it. */
  onSelect: (id: string) => void;
  /** Hovered device id, null when none. */
  onHover?: (id: string | null) => void;
  /**
   * Selected part of the focused device (one of DEVICE_PARTS[selectedId]): close-up camera and a persistent
   * highlight. null, 'body' or an unknown id = whole-device framing ('body' gets a soft outline glow).
   */
  selectedPart?: string | null;
  /** Click on a part of the focused device (drags ignored). Enables part hover feedback and discovery markers. */
  onSelectPart?: (part: string) => void;
  /** Part of the focused device under the pointer, null when none. */
  onHoverPart?: (part: string | null) => void;
  /** Visualization only; does not change or imply a hardware lighting mode. */
  animation?: PreviewAnimation;
  /** 0..100 white MX Keys backlight level, visual only; default 0 (off). */
  keyboardBacklight?: number;
  inset?: SceneInset;
  reducedMotion?: boolean;
  /** Localized accessible and fallback texts supplied by the UI. */
  labels?: SceneLabels;
  /** The GPU dropped the WebGL context (e.g. after sleep or a driver reset); remount the scene. */
  onContextLost?: () => void;
}

const defaultLabels: SceneLabels = {
  canvas: 'Interactive 3D desk preview. Select a device to focus it; in focus view drag to orbit and scroll to zoom.',
  unavailable: '3D preview unavailable. Device controls remain available.',
  webgl: 'WebGL is unavailable. Device controls remain available.',
};

const BACKGROUND = '#050506';

function useReducedMotion(override: boolean | undefined) {
  const [preferred, setPreferred] = useState(() => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const changed = () => setPreferred(query.matches);
    query.addEventListener('change', changed);
    return () => query.removeEventListener('change', changed);
  }, []);
  return override ?? preferred;
}

/** `pointer`: something clickable is under the cursor (a device, or a part of the focused device). */
interface Interaction { hovered: DeviceId | null; pointer: boolean; dragging: boolean; focus: boolean }

function applyCursor(element: HTMLElement, interaction: Interaction) {
  element.style.cursor = interaction.dragging ? 'grabbing' : interaction.pointer ? 'pointer' : interaction.focus ? 'grab' : '';
}

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

// ------------------------------------------------------------------------------------------------
// Camera rig: eased, interruptible transitions between desk and focus framings, with the free-area
// centre (canvas minus UI inset) as the optical axis via setViewOffset.
// ------------------------------------------------------------------------------------------------

/** Channels: target xyz, yaw, pitch, log(distance), inset right, inset bottom. */
const CHANNELS = 8;
/** Third-order critically damped follow: S-shaped ease, ~1 s to settle, velocity-continuous retargeting. */
const RIG_RATE = 9;

interface RigState {
  goal: Float64Array; s1: Float64Array; s2: Float64Array; value: Float64Array;
  orbit: { yaw: number; pitch: number; zoom: number; goalYaw: number; goalPitch: number; goalZoom: number };
  parallax: { x: number; y: number; goalX: number; goalY: number };
  /** focusKey = device + framed part; user orbit/zoom offsets reset whenever it changes. */
  ready: boolean; focusId: DeviceId | null; focusKey: string;
}

function regionById(regions: readonly PartRegion[], id: string | null) {
  if (id) for (let i = 0; i < regions.length; i++) if (regions[i].id === id) return regions[i];
  return undefined;
}

function writeGoal(rig: RigState, pose: CameraPose, right: number, bottom: number) {
  const g = rig.goal;
  g[0] = pose.target.x; g[1] = pose.target.y; g[2] = pose.target.z;
  g[3] = pose.yaw; g[4] = pose.pitch; g[5] = Math.log(pose.distance); g[6] = right; g[7] = bottom;
}

function snapRig(rig: RigState) {
  rig.s1.set(rig.goal); rig.s2.set(rig.goal); rig.value.set(rig.goal);
  const o = rig.orbit; o.yaw = o.goalYaw; o.pitch = o.goalPitch; o.zoom = o.goalZoom;
  const p = rig.parallax; p.x = p.goalX; p.y = p.goalY;
}

function stepRig(rig: RigState, dt: number, reduced: boolean) {
  const a = reduced ? 1 : 1 - Math.exp(-RIG_RATE * dt);
  let moving = false;
  for (let i = 0; i < CHANNELS; i++) {
    rig.s1[i] += (rig.goal[i] - rig.s1[i]) * a;
    rig.s2[i] += (rig.s1[i] - rig.s2[i]) * a;
    rig.value[i] += (rig.s2[i] - rig.value[i]) * a;
    const eps = i >= 6 ? 0.05 : 2e-5;
    if (Math.abs(rig.goal[i] - rig.value[i]) > eps || Math.abs(rig.goal[i] - rig.s1[i]) > eps || Math.abs(rig.goal[i] - rig.s2[i]) > eps) moving = true;
  }
  if (!moving) { rig.s1.set(rig.goal); rig.s2.set(rig.goal); rig.value.set(rig.goal); }
  const o = rig.orbit, ob = reduced ? 1 : 1 - Math.exp(-16 * dt);
  o.yaw += (o.goalYaw - o.yaw) * ob; o.pitch += (o.goalPitch - o.pitch) * ob; o.zoom += (o.goalZoom - o.zoom) * ob;
  if (Math.abs(o.goalYaw - o.yaw) + Math.abs(o.goalPitch - o.pitch) + Math.abs(o.goalZoom - o.zoom) > 2e-5) moving = true;
  else { o.yaw = o.goalYaw; o.pitch = o.goalPitch; o.zoom = o.goalZoom; }
  const p = rig.parallax, pb = reduced ? 1 : 1 - Math.exp(-3.5 * dt);
  p.x += (p.goalX - p.x) * pb; p.y += (p.goalY - p.y) * pb;
  if (Math.abs(p.goalX - p.x) + Math.abs(p.goalY - p.y) > 1e-5) moving = true;
  else { p.x = p.goalX; p.y = p.goalY; }
  return moving;
}

const ZOOM_MIN = Math.log(0.62), ZOOM_MAX = Math.log(1.45);

function CameraRig({ focusId, part, ids, insetRight, insetBottom, reducedMotion, interaction }: {
  /** `part`: a selectable part (not 'body') of the focused device to frame, or null for the whole device. */
  focusId: DeviceId | null; part: string | null; ids: DeviceId[]; insetRight: number; insetBottom: number; reducedMotion: boolean; interaction: Interaction;
}) {
  const camera = useThree(state => state.camera);
  const gl = useThree(state => state.gl);
  const size = useThree(state => state.size);
  const invalidate = useThree(state => state.invalidate);
  const rig = useMemo<RigState>(() => ({
    goal: new Float64Array(CHANNELS), s1: new Float64Array(CHANNELS), s2: new Float64Array(CHANNELS), value: new Float64Array(CHANNELS),
    orbit: { yaw: 0, pitch: 0, zoom: 0, goalYaw: 0, goalPitch: 0, goalZoom: 0 },
    parallax: { x: 0, y: 0, goalX: 0, goalY: 0 },
    ready: false, focusId: null, focusKey: '',
  }), []);
  const scratch = useMemo(() => ({ target: new Vector3(), direction: new Vector3() }), []);
  const reduced = useRef(reducedMotion);
  const idsKey = ids.join('|');

  useEffect(() => {
    reduced.current = reducedMotion;
    if (reducedMotion) { rig.parallax.goalX = 0; rig.parallax.goalY = 0; }
  }, [reducedMotion, rig]);

  useEffect(() => {
    const freeWidth = Math.max(64, size.width - insetRight), freeHeight = Math.max(64, size.height - insetBottom);
    const aspect = freeWidth / freeHeight;
    const region = focusId ? regionById(devicePartRegions(focusId), part) : undefined;
    const pose = !focusId ? deskPose(aspect, idsKey ? (idsKey.split('|') as DeviceId[]) : DEVICE_ORDER)
      : region ? partPose(focusId, region.id, region.view, aspect) : focusPose(focusId, aspect);
    writeGoal(rig, pose, size.width - freeWidth, size.height - freeHeight);
    const focusKey = focusId ? `${focusId}/${region?.id ?? ''}` : '';
    if (rig.focusKey !== focusKey || rig.focusId !== focusId) {
      rig.focusId = focusId; rig.focusKey = focusKey;
      rig.orbit.goalYaw = 0; rig.orbit.goalPitch = 0; rig.orbit.goalZoom = 0;
      if (focusId) { rig.parallax.goalX = 0; rig.parallax.goalY = 0; }
    }
    if (!rig.ready || reducedMotion) { snapRig(rig); rig.ready = true; }
    invalidate();
  }, [focusId, part, idsKey, insetRight, insetBottom, size.width, size.height, reducedMotion, rig, invalidate]);

  useEffect(() => {
    const element = gl.domElement;
    let drag: { id: number; x: number; y: number; moved: number } | null = null;
    const down = (event: PointerEvent) => {
      if (!rig.focusId || event.button !== 0) return;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: 0 };
      try { element.setPointerCapture(event.pointerId); } catch { /* capture is optional */ }
    };
    const move = (event: PointerEvent) => {
      if (drag && event.pointerId === drag.id) {
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
        drag.x = event.clientX; drag.y = event.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
        if (drag.moved > 4 && !interaction.dragging) { interaction.dragging = true; applyCursor(element, interaction); }
        rig.orbit.goalYaw = clamp(rig.orbit.goalYaw - dx * 0.0065, -0.8, 0.8);
        rig.orbit.goalPitch = clamp(rig.orbit.goalPitch + dy * 0.005, -0.5, 0.6);
        invalidate();
        return;
      }
      if (!rig.focusId && !reduced.current && event.pointerType === 'mouse') {
        const rect = element.getBoundingClientRect();
        rig.parallax.goalX = -0.022 * clamp(((event.clientX - rect.left) / rect.width) * 2 - 1, -1, 1);
        rig.parallax.goalY = 0.012 * clamp(((event.clientY - rect.top) / rect.height) * 2 - 1, -1, 1);
        invalidate();
      }
    };
    const up = (event: PointerEvent) => {
      if (!drag || event.pointerId !== drag.id) return;
      drag = null;
      try { element.releasePointerCapture(event.pointerId); } catch { /* already released */ }
      if (interaction.dragging) { interaction.dragging = false; applyCursor(element, interaction); }
    };
    const leave = () => { rig.parallax.goalX = 0; rig.parallax.goalY = 0; invalidate(); };
    const wheel = (event: WheelEvent) => {
      if (!rig.focusId) return;
      event.preventDefault();
      rig.orbit.goalZoom = clamp(rig.orbit.goalZoom + event.deltaY * 0.0011, ZOOM_MIN, ZOOM_MAX);
      invalidate();
    };
    element.addEventListener('pointerdown', down);
    element.addEventListener('pointermove', move);
    element.addEventListener('pointerup', up);
    element.addEventListener('pointercancel', up);
    element.addEventListener('pointerleave', leave);
    element.addEventListener('wheel', wheel, { passive: false });
    return () => {
      element.removeEventListener('pointerdown', down);
      element.removeEventListener('pointermove', move);
      element.removeEventListener('pointerup', up);
      element.removeEventListener('pointercancel', up);
      element.removeEventListener('pointerleave', leave);
      element.removeEventListener('wheel', wheel);
    };
  }, [gl, rig, interaction, invalidate]);

  useFrame((state, delta) => {
    if (!(camera instanceof PerspectiveCamera) || !rig.ready) return;
    const moving = stepRig(rig, Math.min(delta, 0.05), reduced.current);
    const v = rig.value, width = state.size.width, height = state.size.height;
    const freeWidth = Math.max(64, width - clamp(v[6], 0, width)), freeHeight = Math.max(64, height - clamp(v[7], 0, height));
    camera.aspect = freeWidth / freeHeight;
    camera.setViewOffset(freeWidth, freeHeight, 0, 0, width, height);
    camera.updateProjectionMatrix();
    const yaw = v[3] + rig.orbit.yaw + rig.parallax.x;
    const pitch = clamp(v[4] + rig.orbit.pitch + rig.parallax.y, 0.06, 1.35);
    // Pull back while the target travels (filter lag is zero at both ends, so this never jumps).
    const lag = Math.hypot(rig.s1[0] - v[0], rig.s1[1] - v[1], rig.s1[2] - v[2]);
    const distance = Math.exp(v[5] + rig.orbit.zoom + Math.min(0.35, lag * 0.12));
    scratch.target.set(v[0], v[1], v[2]);
    camera.position.copy(scratch.target).addScaledVector(orbitDirection(yaw, pitch, scratch.direction), distance);
    camera.lookAt(scratch.target);
    const fog = state.scene.fog;
    if (fog instanceof Fog) { fog.near = distance + 1.6; fog.far = distance + 10; }
    if (moving) invalidate();
  });
  return null;
}

// ------------------------------------------------------------------------------------------------
// Studio: matte black desk, soft key light with PCF soft shadows, cool rim lights, low PMREM reflections.
// ------------------------------------------------------------------------------------------------

function Environment() {
  const gl = useThree(state => state.gl);
  const scene = useThree(state => state.scene);
  const invalidate = useThree(state => state.invalidate);
  useEffect(() => {
    const generator = new PMREMGenerator(gl);
    const room = new RoomEnvironment();
    const target = generator.fromScene(room, 0.04);
    room.dispose();
    generator.dispose();
    scene.environment = target.texture;
    scene.environmentIntensity = 0.28;
    invalidate();
    return () => { scene.environment = null; target.dispose(); };
  }, [gl, scene, invalidate]);
  return null;
}

function Lights() {
  const scene = useThree(state => state.scene);
  const key = useRef<SpotLight>(null), rimRight = useRef<SpotLight>(null), rimLeft = useRef<SpotLight>(null);
  useEffect(() => {
    const aims: [SpotLight | null, [number, number, number]][] = [
      [key.current, [-0.6, 0.2, 0.1]], [rimRight.current, [0.2, 0.5, 0.3]], [rimLeft.current, [-2.6, 1.3, -0.4]],
    ];
    const targets: Object3D[] = [];
    for (const [light, aim] of aims) {
      if (!light) continue;
      light.target.position.set(...aim);
      scene.add(light.target);
      targets.push(light.target);
    }
    return () => targets.forEach(target => scene.remove(target));
  }, [scene]);
  return <>
    <hemisphereLight args={['#c9d3e0', '#060607', 0.32]} />
    <spotLight ref={key} position={[-3.6, 8.4, 6.4]} angle={0.5} penumbra={1} decay={2} intensity={300} color="#fff5ea" castShadow
      shadow-mapSize={[2048, 2048]} shadow-bias={-0.00012} shadow-normalBias={0.012} shadow-camera-near={5} shadow-camera-far={20} />
    <spotLight ref={rimRight} position={[4.8, 4.4, -6.6]} angle={0.55} penumbra={1} decay={2} intensity={420} color="#c9dcff" />
    <spotLight ref={rimLeft} position={[-7.2, 4.9, -4.6]} angle={0.42} penumbra={1} decay={2} intensity={330} color="#e8efff" />
    <directionalLight position={[2.5, 2.2, 8]} intensity={0.22} color="#dfe8f5" />
  </>;
}

function Desk() {
  const geometry = useMemo(() => deskGeometry(90, 60), []);
  const grain = useMemo(createDeskTexture, []);
  const material = useMemo(() => deskMaterial(grain), [grain]);
  useEffect(() => () => { geometry.dispose(); grain.dispose(); material.dispose(); }, [geometry, grain, material]);
  return <mesh geometry={geometry} material={material} position={[0, 0, -6]} receiveShadow raycast={noRaycast} />;
}

// ------------------------------------------------------------------------------------------------
// Devices
// ------------------------------------------------------------------------------------------------

const CONTACT: Record<DeviceId, { width: number; depth: number; blur: number; radius: number; opacity: number }> = {
  'mx-keys-mini': { width: 2.92, depth: 1.28, blur: 0.1, radius: 0.08, opacity: 0.7 },
  'g733': { width: 1.12, depth: 0.86, blur: 0.09, radius: 0.32, opacity: 0.65 },
  'aerox-3': { width: 0.6, depth: 1.14, blur: 0.08, radius: 0.28, opacity: 0.75 },
};

/** Marker size in CSS pixels; pick proxies make the dot itself clickable even where it overlaps other surfaces. */
const MARKER_PX = 18;
const MARKER_PROXY_RADIUS = 0.034;
/** Selected part: ramps in, pulses a few times with decaying amplitude, then holds a steady glow (no endless redraw). */
const PULSE_SECONDS = 4.2, PULSE_PERIOD = 1.4, SELECTED_LEVEL = 0.75, HOVER_LEVEL = 0.5;

function approach(value: number, goal: number, rate: number, dt: number, reduced: boolean) {
  if (reduced || value === goal) return goal;
  const next = value + (goal - value) * (1 - Math.exp(-rate * dt));
  return Math.abs(goal - next) < 0.002 ? goal : next;
}

/** Writes the selected (slots 0-1) and hovered (slots 2-3) part boxes and per-surface weights. Allocation-free. */
function writeHighlight(highlight: Highlight, selected: PartRegion | undefined, selectedLevel: number, hovered: PartRegion | undefined, hoverLevel: number) {
  const half = HIGHLIGHT_SLOTS / 2;
  for (let slot = 0; slot < HIGHLIGHT_SLOTS; slot++) {
    const region = slot < half ? selected : hovered, box = region?.boxes[slot % half];
    if (box) { highlight.min.value[slot].fromArray(box.min); highlight.max.value[slot].fromArray(box.max); }
  }
  for (let i = 0; i < highlight.surfaces.length; i++) {
    const { surface, level } = highlight.surfaces[i];
    const s = selected && selected.glow.includes(surface) ? selectedLevel : 0, h = hovered && hovered.glow.includes(surface) ? hoverLevel : 0;
    level.value.set(selected?.boxes[0] ? s : 0, selected?.boxes[1] ? s : 0, hovered?.boxes[0] ? h : 0, hovered?.boxes[1] ? h : 0);
  }
}

interface ActorProps {
  device: Device; hovered: boolean; animation: PreviewAnimation; backlight: number; reducedMotion: boolean;
  onHoverChange: (id: DeviceId, over: boolean) => void; onSelect: (id: string) => void;
  /** This device is in focus view; with partsEnabled its parts are clickable and marked. */
  focused: boolean; partsEnabled: boolean; selectedPart: string | null; hoverPart: string | null;
  onPartHover: (part: string | null) => void; onSelectPart: (part: string) => void;
}

const DeviceActor = memo(function DeviceActor({
  device, hovered, animation, backlight, reducedMotion, onHoverChange, onSelect, focused, partsEnabled, selectedPart, hoverPart, onPartHover, onSelectPart,
}: ActorProps) {
  const id = device.id as DeviceId;
  const placement = PLACEMENTS[id];
  const regions = devicePartRegions(id);
  const gl = useThree(state => state.gl);
  const dpr = useThree(state => state.viewport.dpr);
  const invalidate = useThree(state => state.invalidate);
  const actor = useRef<Group>(null), lift = useRef<Group>(null), points = useRef<Points>(null);
  const rim = useMemo<FloatUniform>(() => ({ value: 0 }), []);
  const highlight = useMemo(() => {
    const result = createHighlight();
    result.frame.value.makeRotationY(placement.yaw).setPosition(...placement.position).invert();
    return result;
  }, [placement]);
  const effect = resolveEffect(device.settings.lightingEffect, animation, reducedMotion);
  const contact = useMemo(() => {
    const spec = CONTACT[id];
    const result = createContactTexture(spec.width, spec.depth, spec.blur, spec.radius);
    return { ...result, spec, material: contactMaterial(result.texture, spec.opacity) };
  }, [id]);
  useEffect(() => () => { contact.texture.dispose(); contact.material.dispose(); }, [contact]);

  // Discovery markers: one point per anchor (both cups for the mirrored headset zones), plus invisible pick proxies.
  const markers = useMemo(() => {
    const anchors = regions.flatMap((region, index) => region.anchors.map(anchor => ({ ...anchor, part: region.id, index })));
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(anchors.flatMap(anchor => anchor.position), 3));
    geometry.setAttribute('normal', new Float32BufferAttribute(anchors.flatMap(anchor => anchor.normal), 3));
    geometry.setAttribute('partIndex', new Float32BufferAttribute(anchors.map(anchor => anchor.index), 1));
    return {
      geometry, material: markerMaterial(MARKER_PX), proxy: new SphereGeometry(MARKER_PROXY_RADIUS, 10, 8), proxyMaterial: new MeshBasicMaterial(),
      anchors: anchors.map(anchor => ({ position: anchor.position, userData: { markerPart: anchor.part } })),
    };
  }, [regions]);
  useEffect(() => () => { markers.geometry.dispose(); markers.material.dispose(); markers.proxy.dispose(); markers.proxyMaterial.dispose(); }, [markers]);
  useEffect(() => { markers.material.uniforms.uSize.value = MARKER_PX * dpr; invalidate(); }, [markers, dpr, invalidate]);

  const pickParts = focused && partsEnabled;
  const markersOn = pickParts && !selectedPart && regions.length > 0;
  const anim = useMemo(() => ({ lift: 0, hover: 0, hoverPart: null as string | null, sel: 0, selPart: null as string | null, selStart: 0, markers: 0, active: true }), []);
  useEffect(() => { anim.active = true; invalidate(); }, [hovered, pickParts, selectedPart, hoverPart, markersOn, reducedMotion, anim, invalidate]);
  useFrame((state, delta) => {
    if (!anim.active) return;
    const dt = Math.min(delta, 0.05), t = state.clock.elapsedTime, reduced = reducedMotion;
    // Device hover lift (desk view and other devices); a focused device with clickable parts stays put.
    const liftGoal = hovered && !pickParts ? 1 : 0;
    if (anim.lift !== liftGoal) {
      anim.lift = approach(anim.lift, liftGoal, 12, dt, reduced);
      if (lift.current) lift.current.position.y = anim.lift * placement.lift;
      if (id !== 'g733') contact.material.opacity = contact.spec.opacity * (1 - 0.3 * anim.lift);
      gl.shadowMap.needsUpdate = true;
    }
    // Hovered part switches at once and fades in; it fades out when the pointer leaves.
    if (hoverPart && hoverPart !== anim.hoverPart) { anim.hoverPart = hoverPart; anim.hover = reduced ? 1 : 0; }
    anim.hover = approach(anim.hover, hoverPart ? 1 : 0, 14, dt, reduced);
    if (!hoverPart && anim.hover === 0) anim.hoverPart = null;
    if (selectedPart && selectedPart !== anim.selPart) { anim.selPart = selectedPart; anim.sel = reduced ? 1 : 0; anim.selStart = t; }
    anim.sel = approach(anim.sel, selectedPart ? 1 : 0, 10, dt, reduced);
    if (!selectedPart && anim.sel === 0) anim.selPart = null;
    anim.markers = approach(anim.markers, markersOn ? 1 : 0, 6, dt, reduced);
    const age = t - anim.selStart;
    const pulsing = !reduced && anim.selPart !== null && age < PULSE_SECONDS;
    const selectedLevel = anim.sel * (SELECTED_LEVEL + (pulsing ? 0.25 * Math.cos((age * Math.PI * 2) / PULSE_PERIOD) * (1 - age / PULSE_SECONDS) : 0));
    const hoverLevel = anim.hover * HOVER_LEVEL;
    // 'body' has no box: it glows with the fresnel rim, like the device hover in desk view.
    rim.value = Math.max(anim.lift * 0.55, anim.hoverPart === 'body' ? anim.hover * 0.22 : 0, anim.selPart === 'body' ? selectedLevel * 0.5 : 0);
    const hoveredRegion = anim.hoverPart === anim.selPart ? undefined : regionById(regions, anim.hoverPart);
    writeHighlight(highlight, regionById(regions, anim.selPart), selectedLevel, hoveredRegion, hoverLevel);
    if (points.current) {
      const uniforms = markers.material.uniforms;
      points.current.visible = anim.markers > 0;
      uniforms.uFade.value = anim.markers;
      let hoverIndex = -1;
      for (let i = 0; i < regions.length; i++) if (regions[i].id === anim.hoverPart) hoverIndex = i;
      uniforms.uHover.value = hoverIndex;
    }
    const moving = pulsing || anim.lift !== liftGoal || anim.hover !== (hoverPart ? 1 : 0) || anim.sel !== (selectedPart ? 1 : 0) || anim.markers !== (markersOn ? 1 : 0);
    if (moving) invalidate(); else anim.active = false;
  });

  const scratch = useMemo(() => new Vector3(), []);
  /** Part under the pointer: a live marker wins, otherwise the nearest hit on this device's meshes. */
  const pick = useCallback((event: ThreeEvent<PointerEvent | MouseEvent>) => {
    const group = actor.current, frame = lift.current, hits = event.intersections;
    if (!group || !frame) return null;
    if (markersOn) {
      for (let i = 0; i < hits.length; i++) {
        const marker = hits[i].eventObject === group ? hits[i].object.userData.markerPart : undefined;
        if (typeof marker === 'string') return marker;
      }
    }
    for (let i = 0; i < hits.length; i++) {
      const hit = hits[i];
      if (hit.eventObject !== group || hit.object.userData.markerPart) continue;
      const surface = hit.object.userData.surface as string | undefined;
      if (!surface || surface === 'stand') return null;
      return pickPart(regions, surface, frame.worldToLocal(scratch.copy(hit.point)));
    }
    return null;
  }, [markersOn, regions, scratch]);

  const over = useCallback((event: ThreeEvent<PointerEvent>) => { event.stopPropagation(); onHoverChange(id, true); }, [id, onHoverChange]);
  const out = useCallback(() => { onHoverChange(id, false); if (pickParts) onPartHover(null); }, [id, pickParts, onHoverChange, onPartHover]);
  const move = useCallback((event: ThreeEvent<PointerEvent>) => {
    if (!pickParts) return;
    event.stopPropagation();
    onPartHover(pick(event));
  }, [pickParts, pick, onPartHover]);
  const click = useCallback((event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    if (event.delta > 4) return;
    if (!pickParts) { onSelect(id); return; }
    const part = pick(event);
    if (part) onSelectPart(part);
  }, [id, pickParts, pick, onSelect, onSelectPart]);
  return <group ref={actor} position={placement.position} rotation-y={placement.yaw} onPointerOver={over} onPointerOut={out} onPointerMove={move} onClick={click}>
    <mesh material={contact.material} rotation-x={-Math.PI / 2} position-y={0.0015} scale={[contact.width, contact.depth, 1]} raycast={noRaycast} renderOrder={1}>
      <planeGeometry args={[1, 1]} />
    </mesh>
    {id === 'g733' && <HeadsetStand />}
    <group ref={lift}>
      {id === 'mx-keys-mini' && <MXKeysMini backlight={backlight} rim={rim} highlight={highlight} />}
      {id === 'g733' && <group position-y={HEADSET_ON_STAND}><G733 device={device} effect={effect} rim={rim} highlight={highlight} /></group>}
      {id === 'aerox-3' && <Aerox3 device={device} effect={effect} rim={rim} highlight={highlight} />}
      {pickParts && <points ref={points} geometry={markers.geometry} material={markers.material} renderOrder={20} raycast={noRaycast} frustumCulled={false} />}
      {markersOn && markers.anchors.map((anchor, i) => <mesh key={i} geometry={markers.proxy} material={markers.proxyMaterial} position={anchor.position} userData={anchor.userData} visible={false} />)}
    </group>
  </group>;
});

function Showroom({ devices, selectedId, onSelect, onHover, selectedPart, onSelectPart, onHoverPart, animation, backlight, insetRight, insetBottom, reducedMotion }: {
  devices: Device[]; selectedId: string | null; onSelect: (id: string) => void; onHover?: (id: string | null) => void;
  selectedPart?: string | null; onSelectPart?: (part: string) => void; onHoverPart?: (part: string | null) => void;
  animation: PreviewAnimation; backlight: number; insetRight: number; insetBottom: number; reducedMotion: boolean;
}) {
  const gl = useThree(state => state.gl);
  const invalidate = useThree(state => state.invalidate);
  const visible = useMemo(() => devices.filter(device => isDeviceId(device.id)), [devices]);
  const ids = useMemo(() => DEVICE_ORDER.filter(id => visible.some(device => device.id === id)), [visible]);
  const focusId = isDeviceId(selectedId) && ids.includes(selectedId) ? selectedId : null;
  const partsEnabled = typeof onSelectPart === 'function';
  const activePart = focusId && isDevicePart(focusId, selectedPart) ? selectedPart : null;
  const [hovered, setHovered] = useState<DeviceId | null>(null);
  const [hoverPart, setHoverPart] = useState<string | null>(null);
  const liveHoverPart = focusId && partsEnabled ? hoverPart : null;
  const interaction = useMemo<Interaction>(() => ({ hovered: null, pointer: false, dragging: false, focus: false }), []);
  const onHoverRef = useRef(onHover), onHoverPartRef = useRef(onHoverPart), onSelectPartRef = useRef(onSelectPart);
  const reported = useRef<DeviceId | null>(null), reportedPart = useRef<string | null>(null);
  useEffect(() => { onHoverRef.current = onHover; onHoverPartRef.current = onHoverPart; onSelectPartRef.current = onSelectPart; }, [onHover, onHoverPart, onSelectPart]);
  useEffect(() => { setHoverPart(null); }, [focusId]);
  const onHoverChange = useCallback((id: DeviceId, over: boolean) => {
    setHovered(current => (over ? id : current === id ? null : current));
  }, []);
  const selectPart = useCallback((part: string) => onSelectPartRef.current?.(part), []);
  useEffect(() => {
    interaction.hovered = hovered;
    // Over the focused device only actual parts are clickable (the stand is not).
    interaction.pointer = hovered !== null && !(hovered === focusId && partsEnabled && liveHoverPart === null);
    interaction.focus = focusId !== null;
    applyCursor(gl.domElement, interaction);
    if (reported.current !== hovered) { reported.current = hovered; onHoverRef.current?.(hovered); }
    if (reportedPart.current !== liveHoverPart) { reportedPart.current = liveHoverPart; onHoverPartRef.current?.(liveHoverPart); }
  }, [hovered, focusId, partsEnabled, liveHoverPart, gl, interaction]);
  useEffect(() => () => { gl.domElement.style.cursor = ''; }, [gl]);
  const deviceKey = ids.join('|');
  useEffect(() => {
    // Shadows only change when a device mounts or lifts; camera moves reuse the shadow map.
    gl.shadowMap.autoUpdate = false;
    gl.shadowMap.needsUpdate = true;
    invalidate();
  }, [gl, deviceKey, invalidate]);
  return <>
    <color attach="background" args={[BACKGROUND]} />
    <fog attach="fog" args={[BACKGROUND, 12, 30]} />
    <Environment />
    <Lights />
    <Desk />
    {visible.map(device => {
      const focused = device.id === focusId;
      return <DeviceActor key={device.id} device={device} hovered={hovered === device.id} animation={animation} backlight={backlight}
        reducedMotion={reducedMotion} onHoverChange={onHoverChange} onSelect={onSelect} focused={focused} partsEnabled={partsEnabled}
        selectedPart={focused ? activePart : null} hoverPart={focused ? liveHoverPart : null} onPartHover={setHoverPart} onSelectPart={selectPart} />;
    })}
    <CameraRig focusId={focusId} part={activePart === 'body' ? null : activePart} ids={ids} insetRight={insetRight} insetBottom={insetBottom}
      reducedMotion={reducedMotion} interaction={interaction} />
  </>;
}

class SceneBoundary extends Component<{ children: ReactNode; message: string }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <div role="status" style={{ padding: 24, color: '#b9c5d3' }}>{this.props.message}</div> : this.props.children;
  }
}

export function DeviceScene({
  devices, selectedId, onSelect, onHover, selectedPart = null, onSelectPart, onHoverPart,
  animation = 'static', keyboardBacklight = 0, inset, reducedMotion: motionOverride, labels = defaultLabels, onContextLost,
}: DeviceSceneProps) {
  const contextLost = useRef(onContextLost);
  useEffect(() => { contextLost.current = onContextLost; }, [onContextLost]);
  const reducedMotion = useReducedMotion(motionOverride);
  const effect = reducedMotion ? 'static' : animation;
  const insetRight = Math.max(0, Number.isFinite(inset?.right) ? inset!.right : 0);
  const insetBottom = Math.max(0, Number.isFinite(inset?.bottom) ? inset!.bottom : 0);
  return <SceneBoundary message={labels.unavailable}>
    <Canvas frameloop="demand" dpr={[1, 1.75]} shadows="soft"
      camera={{ fov: (SCENE_FOV * 180) / Math.PI, near: 0.1, far: 120, position: [0, 3, 10], aspect: 1 }}
      gl={{ antialias: true, alpha: false, powerPreference: 'high-performance' }}
      style={{ width: '100%', height: '100%', touchAction: 'none', display: 'block' }}
      fallback={<div role="status">{labels.webgl}</div>}
      onCreated={({ gl }) => gl.domElement.addEventListener('webglcontextlost', () => contextLost.current?.())}
      aria-label={labels.canvas}>
      <Showroom devices={devices} selectedId={selectedId} onSelect={onSelect} onHover={onHover}
        selectedPart={selectedPart} onSelectPart={onSelectPart} onHoverPart={onHoverPart} animation={effect} backlight={keyboardBacklight}
        insetRight={insetRight} insetBottom={insetBottom} reducedMotion={reducedMotion} />
    </Canvas>
  </SceneBoundary>;
}
