import { memo, useCallback, useEffect, useMemo, useRef } from 'react';
import { useFrame, useThree } from '@react-three/fiber';
import { BufferGeometry, Color } from 'three';
import type { Device } from '../types';
import { buildHeadset, buildKeyboard, buildMouse, buildStand, KEYBOARD_LIFT, KEYBOARD_TILT } from './geometry';
import { headsetMaterials, keyboardMaterials, mouseMaterials, standMaterials } from './materials';
import type { FloatUniform, Highlight } from './materials';
import { createFabricTexture, createGlowTexture, createLegendTextures, createStrapTexture } from './textures';
import { backlightLevel, effectRate, LIGHT_ZONES, lightingAmount, sampleLighting, zoneColor } from './lighting';
import type { PreviewAnimation } from './lighting';

/** Lighting effects are slow fades: 30 frames/s looks the same as the display rate at half the GPU work. */
const EFFECT_FRAME_MS = 1000 / 30;

/** Decorative meshes (glows, decals) must not capture pointer hits. */
export const noRaycast = () => undefined;

/** Surface tags read by part picking (mesh userData.surface); see devicePartRegions in geometry.ts. */
const surface = (name: string) => ({ surface: name });
const S = {
  frame: surface('frame'), keys: surface('keys'),
  plastic: surface('plastic'), cushion: surface('cushion'), strap: surface('strap'), mic: surface('mic'), strip: surface('strip'), stand: surface('stand'),
  shell: surface('shell'), walls: surface('walls'), inner: surface('inner'), base: surface('base'), wheel: surface('wheel'), buttons: surface('buttons'),
} as const;

function disposeGeometries(value: object) {
  for (const item of Object.values(value)) {
    if (item instanceof BufferGeometry) item.dispose();
    else if (Array.isArray(item)) item.forEach(entry => { if (entry instanceof BufferGeometry) entry.dispose(); });
  }
}

/** Creates an asset once per mount and disposes it on unmount. */
function useAsset<T>(create: () => T, dispose: (value: T) => void, deps: unknown[]) {
  const value = useMemo(create, deps);
  useEffect(() => () => dispose(value), [value]);
  return value;
}

/**
 * Drives the device's lighting zones: settings.lightingZones overrides settings.lightingColor per zone,
 * lightingBrightness scales every zone (0 = dark). `effect` is already resolved per device; breathing and
 * spectrum use per-zone phase offsets (front -> rear, top -> bottom). Requests frames only while animating,
 * at most EFFECT_FRAME_MS apart.
 */
function useLightZones(device: Device, effect: PreviewAnimation, zones: readonly string[], phases: readonly number[], apply: (index: number, color: Color, level: number) => void) {
  const invalidate = useThree(state => state.invalidate);
  const key = zones.map(zone => zoneColor(device.settings, zone)).join('|');
  const bases = useMemo(() => key.split('|').map(hex => new Color(hex)), [key]);
  const amount = lightingAmount(device);
  const rate = effectRate(effect, device.settings.lightingSpeed);
  const scratch = useMemo(() => new Color(), []);
  const pending = useRef<number | null>(null);
  useEffect(() => { invalidate(); }, [bases, amount, effect, apply, invalidate]);
  useEffect(() => () => { if (pending.current !== null) window.clearTimeout(pending.current); }, []);
  useFrame(({ clock }) => {
    for (let i = 0; i < bases.length; i++) {
      const level = sampleLighting(scratch, bases[i], amount, effect, clock.elapsedTime * rate, phases[i]);
      apply(i, scratch, level);
    }
    if (effect !== 'static' && amount > 0 && pending.current === null) {
      pending.current = window.setTimeout(() => { pending.current = null; invalidate(); }, EFFECT_FRAME_MS);
    }
  });
}

export const MXKeysMini = memo(function MXKeysMini({ backlight, rim, highlight }: { backlight: number; rim: FloatUniform; highlight?: Highlight }) {
  const invalidate = useThree(state => state.invalidate);
  const parts = useAsset(buildKeyboard, disposeGeometries, []);
  const legends = useAsset(() => createLegendTextures(parts.keys), value => { value.color.dispose(); value.glow.dispose(); }, [parts]);
  const materials = useAsset(() => keyboardMaterials(legends, rim, highlight), value => value.dispose(), [legends, rim, highlight]);
  const level = backlightLevel(backlight);
  useEffect(() => { materials.setBacklight(level); invalidate(); }, [materials, level, invalidate]);
  return <group position-y={KEYBOARD_LIFT} rotation-x={KEYBOARD_TILT}>
    <mesh geometry={parts.body} material={materials.frame} castShadow receiveShadow userData={S.frame} />
    <mesh geometry={parts.caps} material={materials.keys} castShadow receiveShadow userData={S.keys} />
    <mesh geometry={parts.gap} material={materials.gap} raycast={noRaycast} renderOrder={1} />
    <mesh geometry={parts.glow} material={materials.glow} raycast={noRaycast} renderOrder={2} visible={level > 0} />
    <mesh geometry={parts.details} material={materials.detail} userData={S.frame} />
  </group>;
});

/** Top and bottom zones; the phase step makes breathing/spectrum travel top -> bottom. */
const HEADSET_PHASES = [0, 0.12] as const;

export const G733 = memo(function G733({ device, effect, rim, highlight }: { device: Device; effect: PreviewAnimation; rim: FloatUniform; highlight?: Highlight }) {
  const parts = useAsset(buildHeadset, disposeGeometries, []);
  const textures = useAsset(() => ({ fabric: createFabricTexture(), dots: createStrapTexture(), glow: createGlowTexture() }),
    value => { value.fabric.dispose(); value.dots.dispose(); value.glow.dispose(); }, []);
  const materials = useAsset(() => headsetMaterials(textures.fabric, textures.dots, textures.glow, rim, highlight), value => value.dispose(), [textures, rim, highlight]);
  const apply = useCallback((index: number, color: Color, level: number) => {
    const zones = materials.zones;
    zones[index].value.copy(color).multiplyScalar(level);
    // Both cups show the same two zones; the soft spill sprites take the mix of top and bottom.
    if (index === 1) for (const sprite of materials.sprites) sprite.color.copy(zones[0].value).add(zones[1].value).multiplyScalar(0.25);
  }, [materials]);
  useLightZones(device, effect, LIGHT_ZONES.g733, HEADSET_PHASES, apply);
  return <group>
    <mesh geometry={parts.plastic} material={materials.plastic} castShadow receiveShadow userData={S.plastic} />
    <mesh geometry={parts.panels} material={materials.panel} userData={S.plastic} />
    <mesh geometry={parts.cushions} material={materials.cushion} castShadow receiveShadow userData={S.cushion} />
    <mesh geometry={parts.cloth} material={materials.cloth} userData={S.cushion} />
    <mesh geometry={parts.strap} material={materials.strap} castShadow receiveShadow userData={S.strap} />
    <mesh geometry={parts.tabs} material={materials.tab} userData={S.strap} />
    <mesh geometry={parts.mic} material={materials.mic} castShadow receiveShadow userData={S.mic} />
    {parts.strips.map((geometry, i) => <mesh key={i} geometry={geometry} material={materials.strips[i]} userData={S.strip} />)}
    {parts.stripGlow.map((position, i) => <sprite key={i} material={materials.sprites[i]} position={position} scale={[0.34, 0.7, 1]} raycast={noRaycast} />)}
  </group>;
});

export const HeadsetStand = memo(function HeadsetStand() {
  const parts = useAsset(buildStand, disposeGeometries, []);
  const materials = useAsset(standMaterials, value => value.dispose(), []);
  return <group>
    <mesh geometry={parts.metal} material={materials.metal} castShadow receiveShadow userData={S.stand} />
    <mesh geometry={parts.pad} material={materials.pad} castShadow receiveShadow userData={S.stand} />
  </group>;
});

const MOUSE_PHASES = [0, 0.2, 0.4] as const;

export const Aerox3 = memo(function Aerox3({ device, effect, rim, highlight }: { device: Device; effect: PreviewAnimation; rim: FloatUniform; highlight?: Highlight }) {
  const parts = useAsset(buildMouse, disposeGeometries, []);
  const materials = useAsset(() => mouseMaterials(rim, highlight), value => value.dispose(), [rim, highlight]);
  const apply = useCallback((index: number, color: Color, level: number) => {
    materials.zones[index].value.copy(color).multiplyScalar(level);
  }, [materials]);
  useLightZones(device, effect, LIGHT_ZONES['aerox-3'], MOUSE_PHASES, apply);
  return <group>
    <mesh geometry={parts.shell} material={materials.shell} castShadow receiveShadow userData={S.shell} />
    <mesh geometry={parts.walls} material={materials.walls} userData={S.walls} />
    <mesh geometry={parts.inner} material={materials.inner} receiveShadow userData={S.inner} />
    <mesh geometry={parts.base} material={materials.base} castShadow receiveShadow userData={S.base} />
    <mesh geometry={parts.grooves} material={materials.groove} raycast={noRaycast} />
    <mesh geometry={parts.wheel} material={materials.wheel} position={parts.wheelCenter} castShadow userData={S.wheel} />
    <mesh geometry={parts.buttons} material={materials.solid} castShadow userData={S.buttons} />
    <mesh geometry={parts.port} material={materials.port} userData={S.base} />
    {/* Base perimeter light strip and its soft spill on the desk. */}
    <mesh geometry={parts.strip} material={materials.strip} userData={S.strip} />
    <mesh geometry={parts.deskGlow} material={materials.deskGlow} raycast={noRaycast} renderOrder={1} />
  </group>;
});
