import { Color } from 'three';
import type { LightingEffect, LightingSpeed, Settings } from '../types';

export type PreviewAnimation = 'static' | 'breathing' | 'spectrum';

/** Lighting zones rendered per device, in the order of the geometry `zone` attribute channels. */
export const LIGHT_ZONES = { 'aerox-3': ['front', 'middle', 'rear'], 'g733': ['top', 'bottom'] } as const;

export function brightness(value: number | undefined) {
  return Number.isFinite(value) ? Math.min(100, Math.max(0, value!)) / 100 : 0.65;
}

const HEX = /^#[0-9a-f]{6}$/i;

export function safeColor(value: string | undefined, fallback = '#72e8cc') {
  return value && HEX.test(value) ? value : fallback;
}

/** Zone colour: a valid settings.lightingZones entry overrides settings.lightingColor. */
export function zoneColor(settings: Pick<Settings, 'lightingColor' | 'lightingZones'>, zone: string) {
  const zones = settings.lightingZones;
  const value = zones && Object.prototype.hasOwnProperty.call(zones, zone) ? zones[zone] : undefined;
  return typeof value === 'string' && HEX.test(value) ? value : safeColor(settings.lightingColor);
}

/** Per-device effect (settings.lightingEffect) wins over the global preview animation; reduced motion is static. */
export function resolveEffect(effect: LightingEffect | undefined, fallback: PreviewAnimation, reducedMotion = false): PreviewAnimation {
  if (reducedMotion) return 'static';
  if (effect === 'rainbow') return 'spectrum';
  if (effect === 'breathing' || effect === 'static') return effect;
  return fallback;
}

/**
 * Zone intensity 0..1 for a device. Safety rule: a non-simulated device without an acknowledged
 * lightingBrightness stays dark (never infer real lighting state).
 */
export function lightingAmount(device: { connection: string; settings: { lightingBrightness?: number } }) {
  return device.connection !== 'simulated' && device.settings.lightingBrightness === undefined
    ? 0 : brightness(device.settings.lightingBrightness);
}

/** MX Keys Mini white backlight 0..100 -> 0..1; missing/invalid means off. */
export function backlightLevel(value: number | undefined) {
  return Number.isFinite(value) ? Math.min(100, Math.max(0, value!)) / 100 : 0;
}

/** Animation rate for settings.lightingSpeed, matching the devices (breathing 6/4/2 s, rainbow 12/6/3 s). */
export function effectRate(animation: PreviewAnimation, speed: LightingSpeed | undefined) {
  if (speed === 'fast') return 2;
  if (speed === 'slow') return animation === 'spectrum' ? 0.5 : 2 / 3;
  return 1;
}

// Mutates a reusable Color: no per-frame allocations or React state updates.
// At rate 1 breathing takes 4 s and the spectrum 6 s, like the devices' normal speed.
export function sampleLighting(target: Color, base: string | Color, amount: number, animation: PreviewAnimation, seconds: number, phase = 0) {
  target.set(base);
  if (animation === 'spectrum') target.setHSL((seconds / 6 + phase) % 1, 0.85, 0.60);
  const pulse = animation === 'breathing' ? 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(seconds * Math.PI / 2 + phase * Math.PI * 2)) : 1;
  return amount * pulse;
}
