import { AdditiveBlending, Color, DoubleSide, Matrix4, MeshBasicMaterial, MeshPhysicalMaterial, MeshStandardMaterial, ShaderMaterial, SpriteMaterial, Vector3, Vector4 } from 'three';
import type { Texture } from 'three';

export interface FloatUniform { value: number }
export interface ColorUniform { value: Color }
interface Disposable { dispose(): void }

const RIM_COLOR = new Color('#a9bed9');
/** Part selection/hover glow (close to the UI accent). */
export const HIGHLIGHT_COLOR = '#9cf0d6';
export const HIGHLIGHT_SLOTS = 4;

/**
 * Per-device part highlight: up to four placement-local boxes (selected part in slots 0-1, hovered part in
 * slots 2-3). Each surface tag owns a vec4 of slot weights so a part only glows on its own meshes.
 */
export interface Highlight {
  /** World -> placement-local transform of the device. */
  frame: { value: Matrix4 };
  min: { value: Vector3[] };
  max: { value: Vector3[] };
  color: ColorUniform;
  /** One entry per surface tag, registered when the device materials are created. */
  surfaces: { surface: string; level: { value: Vector4 } }[];
}

export function createHighlight(): Highlight {
  const slots = () => Array.from({ length: HIGHLIGHT_SLOTS }, () => new Vector3());
  return { frame: { value: new Matrix4() }, min: { value: slots() }, max: { value: slots() }, color: { value: new Color(HIGHLIGHT_COLOR) }, surfaces: [] };
}

function highlightLevel(highlight: Highlight, surface: string) {
  let entry = highlight.surfaces.find(item => item.surface === surface);
  if (!entry) { entry = { surface, level: { value: new Vector4() } }; highlight.surfaces.push(entry); }
  return entry.level;
}

const HIGHLIGHT_HEAD = `uniform vec3 uHlMin[${HIGHLIGHT_SLOTS}];
uniform vec3 uHlMax[${HIGHLIGHT_SLOTS}];
uniform vec4 uHlLevel;
uniform vec3 uHlColor;
varying vec3 vHlPos;
float upmHlBox(vec3 lo, vec3 hi) {
  vec3 d = max(lo - vHlPos, vHlPos - hi);
  return 1.0 - smoothstep(0.0, 0.016, max(max(d.x, d.y), d.z));
}
float upmHlEdge(vec3 lo, vec3 hi) {
  vec2 d = min(vHlPos.xz - lo.xz, hi.xz - vHlPos.xz);
  return 1.0 - smoothstep(0.0, 0.012, min(d.x, d.y));
}
`;
// Soft tint with a fresnel lift plus a brighter band along the box sides (reads as an outline on caps/buttons
// and as boundary lines on the mouse thirds). Skipped entirely while no slot is active.
const HIGHLIGHT_BODY = `if (uHlLevel.x + uHlLevel.y + uHlLevel.z + uHlLevel.w > 0.0) {
  float hlMask = 0.0, hlRing = 0.0;
  for (int i = 0; i < ${HIGHLIGHT_SLOTS}; i++) {
    float hlInside = upmHlBox(uHlMin[i], uHlMax[i]) * uHlLevel[i];
    hlMask = max(hlMask, hlInside);
    hlRing = max(hlRing, hlInside * upmHlEdge(uHlMin[i], uHlMax[i]));
  }
  float hlFresnel = 1.0 - clamp(dot(normal, normalize(vViewPosition)), 0.0, 1.0);
  totalEmissiveRadiance += uHlColor * (hlMask * (0.07 + 0.5 * hlFresnel * hlFresnel) + hlRing * 0.55);
}
`;

/**
 * Adds optional emissive terms to a standard/physical material without changing its lighting model:
 * - `rim`: view-dependent fresnel highlight driven by a shared per-device uniform (hover feedback).
 * - `zones`: three RGB light zones weighted by the geometry's `zone` attribute (light diffusers, Aerox 3 glow).
 * - `highlight`: part selection/hover glow inside the device's highlight boxes, for one surface tag.
 * Programs are shared through customProgramCacheKey; uniforms stay per material.
 */
export function withEffects<T extends MeshStandardMaterial>(material: T, { rim, zones, zoneGain = 1, zoneBackOnly = false, highlight }: {
  rim?: FloatUniform; zones?: ColorUniform[]; zoneGain?: number; zoneBackOnly?: boolean; highlight?: { rig: Highlight; surface: string };
}): T {
  const level = highlight ? highlightLevel(highlight.rig, highlight.surface) : null;
  material.onBeforeCompile = shader => {
    let head = '', body = '', vertexHead = '', vertexBody = '';
    if (rim) {
      shader.uniforms.uRim = rim;
      shader.uniforms.uRimColor = { value: RIM_COLOR };
      head += 'uniform float uRim;\nuniform vec3 uRimColor;\n';
      body += 'float rimTerm = 1.0 - clamp(dot(normal, normalize(vViewPosition)), 0.0, 1.0);\n'
        + 'totalEmissiveRadiance += uRimColor * (uRim * rimTerm * rimTerm * rimTerm);\n';
    }
    if (zones) {
      zones.forEach((zone, i) => { shader.uniforms[`uZone${i}`] = zone; });
      shader.uniforms.uZoneGain = { value: zoneGain };
      vertexHead += 'attribute vec3 zone;\nvarying vec3 vZone;\n';
      vertexBody += 'vZone = zone;\n';
      head += 'uniform vec3 uZone0;\nuniform vec3 uZone1;\nuniform vec3 uZone2;\nuniform float uZoneGain;\nvarying vec3 vZone;\n';
      body += 'vec3 zoneLight = (vZone.x * uZone0 + vZone.y * uZone1 + vZone.z * uZone2) * uZoneGain;\n'
        + `${zoneBackOnly ? 'if (!gl_FrontFacing) ' : ''}totalEmissiveRadiance += zoneLight;\n`;
    }
    if (highlight && level) {
      const { rig } = highlight;
      Object.assign(shader.uniforms, { uHlFrame: rig.frame, uHlMin: rig.min, uHlMax: rig.max, uHlColor: rig.color, uHlLevel: level });
      vertexHead += 'uniform mat4 uHlFrame;\nvarying vec3 vHlPos;\n';
      vertexBody += 'vHlPos = (uHlFrame * modelMatrix * vec4(transformed, 1.0)).xyz;\n';
      head += HIGHLIGHT_HEAD;
      body += HIGHLIGHT_BODY;
    }
    if (vertexHead) {
      shader.vertexShader = shader.vertexShader
        .replace('#include <common>', `#include <common>\n${vertexHead}`)
        .replace('#include <begin_vertex>', `#include <begin_vertex>\n${vertexBody}`);
    }
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>\n${head}`)
      .replace('#include <emissivemap_fragment>', `#include <emissivemap_fragment>\n${body}`);
  };
  material.customProgramCacheKey = () => `upm-${rim ? 'rim' : ''}-${zones ? (zoneBackOnly ? 'zone-back' : 'zone') : ''}-${highlight ? 'hl' : ''}`;
  return material;
}

function bundle<T extends object>(value: T, resources: Disposable[]): T & Disposable {
  return Object.assign(value, { dispose: () => resources.forEach(resource => resource.dispose()) });
}

/** Highlight option for one surface tag (undefined when the device has no highlight rig). */
const hl = (rig: Highlight | undefined, surface: string) => (rig ? { rig, surface } : undefined);

export function keyboardMaterials(legends: { color: Texture; glow: Texture }, rim: FloatUniform, highlight?: Highlight) {
  const frame = withEffects(new MeshStandardMaterial({ color: '#2b2e33', roughness: 0.5, metalness: 0.28 }), { rim, highlight: hl(highlight, 'frame') });
  const keys = withEffects(new MeshStandardMaterial({
    color: '#ffffff', map: legends.color, emissive: '#ffffff', emissiveMap: legends.glow, emissiveIntensity: 0, roughness: 0.5, metalness: 0.04,
  }), { rim, highlight: hl(highlight, 'keys') });
  const glow = new MeshBasicMaterial({ color: '#000000', vertexColors: true, transparent: true, blending: AdditiveBlending, depthWrite: false, toneMapped: false, side: DoubleSide });
  const gap = new MeshBasicMaterial({ color: '#000000', transparent: true, opacity: 0.8, depthWrite: false, side: DoubleSide });
  const detail = new MeshStandardMaterial({ color: '#070708', roughness: 0.8 });
  /** White backlight: legends glow and light leaks between keys; 0 is fully off. */
  const setBacklight = (level: number) => {
    keys.emissiveIntensity = level * 1.15;
    glow.color.setScalar(level * 0.085);
  };
  return bundle({ frame, keys, glow, gap, detail, setBacklight }, [frame, keys, glow, gap, detail]);
}

/**
 * Additive light spill driven by the same three zone uniforms (`zone` weights x `glow` falloff per vertex).
 * Not tone mapped so any user colour keeps its hue; zero brightness adds nothing.
 */
export function zoneGlowMaterial(zones: ColorUniform[], gain: number) {
  return new ShaderMaterial({
    uniforms: { uZone0: zones[0], uZone1: zones[1], uZone2: zones[2], uGain: { value: gain } },
    vertexShader: `attribute vec3 zone;
attribute float glow;
varying vec3 vZone;
varying float vGlow;
void main() {
  vZone = zone;
  vGlow = glow;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`,
    fragmentShader: `uniform vec3 uZone0;
uniform vec3 uZone1;
uniform vec3 uZone2;
uniform float uGain;
varying vec3 vZone;
varying float vGlow;
void main() {
  gl_FragColor = vec4((vZone.x * uZone0 + vZone.y * uZone1 + vZone.z * uZone2) * uGain * vGlow * vGlow, 1.0);
  #include <colorspace_fragment>
}`,
    transparent: true, blending: AdditiveBlending, depthWrite: false, toneMapped: false,
  });
}

/**
 * Part discovery markers (Points): soft dot + thin ring at a constant pixel size, drawn on top of the device.
 * Markers on surfaces facing away from the camera are dimmed rather than hidden; `uHover` (part index) enlarges one.
 */
export function markerMaterial(pixelSize: number) {
  return new ShaderMaterial({
    uniforms: { uSize: { value: pixelSize }, uFade: { value: 0 }, uHover: { value: -1 }, uColor: { value: new Color('#dcfff3') } },
    vertexShader: `attribute float partIndex;
uniform float uSize;
uniform float uFade;
uniform float uHover;
varying float vAlpha;
varying float vHover;
void main() {
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  float facing = dot(normalize(normalMatrix * normal), normalize(-mv.xyz));
  vHover = 1.0 - step(0.5, abs(partIndex - uHover));
  vAlpha = uFade * mix(0.32, 1.0, smoothstep(-0.2, 0.25, facing));
  gl_PointSize = uSize * (1.0 + 0.3 * vHover);
  gl_Position = projectionMatrix * mv;
}`,
    fragmentShader: `uniform vec3 uColor;
varying float vAlpha;
varying float vHover;
void main() {
  float r = length(gl_PointCoord * 2.0 - 1.0);
  float ring = 1.0 - smoothstep(0.05, 0.14, abs(r - 0.68));
  float core = 1.0 - smoothstep(0.15, 0.29, r);
  float halo = (1.0 - smoothstep(0.2, 1.0, r)) * (0.18 + 0.22 * vHover);
  float a = max(max(ring * 0.85, core), halo) * vAlpha;
  if (a < 0.004) discard;
  gl_FragColor = vec4(uColor, a);
  #include <colorspace_fragment>
}`,
    transparent: true, depthTest: false, depthWrite: false, toneMapped: false,
  });
}

/** Light diffuser: dark glossy plastic when off, emits the zone colours (untone-mapped) when lit. */
const diffuser = (zones: ColorUniform[], gain: number, highlight?: Highlight) =>
  withEffects(new MeshStandardMaterial({ color: '#141416', roughness: 0.3, toneMapped: false }), { zones, zoneGain: gain, highlight: hl(highlight, 'strip') });

/** zones[0..2] = front, middle, rear. */
export function mouseMaterials(rim: FloatUniform, highlight?: Highlight) {
  const zones: ColorUniform[] = [0, 1, 2].map(() => ({ value: new Color(0, 0, 0) }));
  const shell = withEffects(new MeshStandardMaterial({ color: '#141518', roughness: 0.5, metalness: 0, side: DoubleSide }),
    { rim, zones, zoneGain: 0.3, zoneBackOnly: true, highlight: hl(highlight, 'shell') });
  const walls = withEffects(new MeshStandardMaterial({ color: '#141518', roughness: 0.6, side: DoubleSide }), { zones, zoneGain: 0.45, highlight: hl(highlight, 'walls') });
  const inner = withEffects(new MeshStandardMaterial({ color: '#060607', roughness: 0.9 }), { zones, zoneGain: 0.72, highlight: hl(highlight, 'inner') });
  const solid = withEffects(new MeshStandardMaterial({ color: '#141518', roughness: 0.46 }), { rim, highlight: hl(highlight, 'buttons') });
  const base = withEffects(new MeshStandardMaterial({ color: '#0e0f11', roughness: 0.62 }), { rim, highlight: hl(highlight, 'base') });
  const wheel = withEffects(new MeshStandardMaterial({ color: '#1d1e21', roughness: 0.84 }), { rim });
  const groove = new MeshBasicMaterial({ color: '#030303', polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
  const port = new MeshStandardMaterial({ color: '#050506', roughness: 0.8 });
  const strip = diffuser(zones, 1.25, highlight);
  const deskGlow = zoneGlowMaterial(zones, 0.55);
  return bundle({ zones, shell, walls, inner, solid, base, wheel, groove, port, strip, deskGlow }, [shell, walls, inner, solid, base, wheel, groove, port, strip, deskGlow]);
}

export function headsetMaterials(fabric: Texture, dots: Texture, glowTexture: Texture, rim: FloatUniform, highlight?: Highlight) {
  const plastic = withEffects(new MeshPhysicalMaterial({ color: '#141518', roughness: 0.44, metalness: 0, clearcoat: 0.3, clearcoatRoughness: 0.45 }),
    { rim, highlight: hl(highlight, 'plastic') });
  const panel = withEffects(new MeshStandardMaterial({ color: '#0c0d0f', roughness: 0.2, metalness: 0.1 }), { rim });
  const cushion = withEffects(new MeshPhysicalMaterial({
    color: '#0c0d0f', roughness: 0.94, sheen: 0.6, sheenColor: '#2c3037', sheenRoughness: 0.6, bumpMap: fabric, bumpScale: 0.6,
  }), { rim });
  const cloth = new MeshStandardMaterial({ color: '#0a0a0b', roughness: 1, bumpMap: fabric, bumpScale: 0.5 });
  const strap = withEffects(new MeshPhysicalMaterial({
    color: '#ffffff', map: dots, roughness: 0.8, sheen: 0.7, sheenColor: '#4b5058', sheenRoughness: 0.55, bumpMap: fabric, bumpScale: 0.5,
  }), { rim });
  const tab = new MeshStandardMaterial({ color: '#a7aab0', roughness: 0.45 });
  const mic = withEffects(new MeshStandardMaterial({ color: '#131417', roughness: 0.6 }), { rim });
  // Two hardware zones (top, bottom) shared by both earcup strips; the third channel stays unused (black).
  const zones: ColorUniform[] = [0, 1, 2].map(() => ({ value: new Color(0, 0, 0) }));
  const strips = [0, 1].map(() => diffuser(zones, 1, highlight));
  const sprites = [0, 1].map(() => new SpriteMaterial({ map: glowTexture, color: '#000000', transparent: true, blending: AdditiveBlending, depthWrite: false, toneMapped: false }));
  return bundle({ plastic, panel, cushion, cloth, strap, tab, mic, zones, strips, sprites }, [plastic, panel, cushion, cloth, strap, tab, mic, ...strips, ...sprites]);
}

export function standMaterials() {
  const metal = new MeshStandardMaterial({ color: '#0f1012', metalness: 0.2, roughness: 0.62 });
  const pad = new MeshStandardMaterial({ color: '#0b0b0c', roughness: 0.95 });
  return bundle({ metal, pad }, [metal, pad]);
}

export function deskMaterial(grain: Texture) {
  grain.repeat.set(64, 42);
  return new MeshPhysicalMaterial({ color: '#09090a', roughness: 0.98, metalness: 0, roughnessMap: grain, bumpMap: grain, bumpScale: 0.04, specularIntensity: 0.55 });
}

export function contactMaterial(alpha: Texture, opacity: number) {
  return new MeshBasicMaterial({ color: '#000000', alphaMap: alpha, transparent: true, opacity, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 });
}
