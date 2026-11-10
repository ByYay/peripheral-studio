export interface Settings {
  lightingColor?: string;
  lightingBrightness?: number;
  dpi?: number;
  pollingRate?: number;
  backlightMode?: 'off' | 'auto' | 'manual';
  backlightLevel?: number;
  /** Per-zone colors (aerox: front/middle/rear, g733: top/bottom); override lightingColor. */
  lightingZones?: Record<string, string>;
  lightingEffect?: LightingEffect;
  lightingSpeed?: LightingSpeed;
}

export type LightingEffect = 'static' | 'breathing' | 'rainbow';
export type LightingSpeed = 'slow' | 'normal' | 'fast';

export interface Range {
  min: number;
  max: number;
  step: number;
}

export interface Device {
  id: 'mx-keys-mini' | 'g733' | 'aerox-3';
  name: string;
  kind: 'keyboard' | 'headset' | 'mouse';
  connection: 'simulated' | 'connected' | 'disconnected' | 'unsupported';
  batteryPercent: number | null;
  capabilities: { lighting: boolean; dpi: boolean; pollingRate: boolean; backlight?: boolean };
  settings: Settings;
  desiredSettings?: Settings;
  status?: { code: string; message: string };
  telemetry?: Record<string, unknown> & { batteryMillivolts?: number; writeVerification?: 'readback' | 'acknowledged'; fnKeys?: 'special' | 'standard'; sharedKeys?: string[] };
  limits: { dpi?: Range; pollingRates?: number[]; backlightLevel?: Range; lightingZones?: string[]; lightingEffects?: LightingEffect[]; lightingSpeedEffects?: LightingEffect[] };
}

export type Language = 'tr' | 'en';

export interface Preferences {
  language: Language;
  /** Present only when Windows can start the app (the installed EXE). */
  startWithWindows?: boolean;
}

/** A time range (local, may pass midnight) or programs (.exe file names) that must be running. */
export type Trigger = { type: 'time'; start: string; end: string } | { type: 'app'; apps: string[] };

export interface Profile {
  id: string;
  name: string;
  /** One main profile is chosen; an automatic one applies while its trigger holds. */
  kind: 'main' | 'automatic';
  settings: Record<string, Settings>;
  trigger?: Trigger;
}

export interface ProfileState {
  mainProfileId: string | null;
  /** Changes made while it is active are saved into it. */
  activeProfileId: string | null;
  activeBy: 'main' | 'rule' | 'manual' | null;
}
