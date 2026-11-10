import type { Device, Language, Preferences, Profile, ProfileState, Settings, Trigger } from './types';

/** Messages stay English; the UI translates them by code (see i18n.ts). */
export class ApiRequestError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

// A desktop session receives an unpredictable token in the URL fragment, which
// never reaches HTTP logs. Session storage survives reload only within this
// window/origin; the desktop uses a private WebView and a fresh port/token.
const fragment = new URLSearchParams(window.location.hash.slice(1));
let sessionToken = fragment.get('token');
try {
  if (sessionToken) window.sessionStorage.setItem('upm-session-token', sessionToken);
  else sessionToken = window.sessionStorage.getItem('upm-session-token');
} catch {
  // Restricted storage still permits the original bootstrapped session.
}
const token = sessionToken ?? (import.meta.env.DEV ? 'upm-development-only' : '');
if (fragment.has('token')) {
  window.history.replaceState(null, '', window.location.pathname + window.location.search);
}

async function request<T>(path: string, method = 'GET', body?: unknown, timeoutMs = 8000): Promise<T> {
  if (!token) throw new ApiRequestError('session_unavailable', 'Session unavailable. Close and reopen the desktop application.');
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`/api/v1${path}`, {
      method,
      headers: {
        'X-UPM-Token': token,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: 'no-store',
      credentials: 'omit',
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new ApiRequestError(data.error?.code ?? 'request_failed', data.error?.message ?? `Request failed (${response.status}).`);
    }
    return data as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiRequestError('timeout', 'The device service timed out. Please retry.');
    }
    if (error instanceof TypeError) {
      throw new ApiRequestError('network', 'The local service is unreachable.');
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function getDevices(): Promise<Device[]> {
  return (await request<{ devices: Device[] }>('/devices')).devices;
}
export interface ProfileList { profiles: Profile[]; profileState: ProfileState }
export function getProfiles(): Promise<ProfileList> {
  return request('/profiles');
}
export async function updateSettings(id: string, patch: Settings): Promise<Device> {
  return (await request<{ device: Device }>(`/devices/${encodeURIComponent(id)}/settings`, 'PATCH', patch)).device;
}
/** Without a trigger a main profile (it becomes the chosen one); with one an automatic profile. */
export function createProfile(name: string, trigger?: Trigger): Promise<{ profile: Profile; profileState: ProfileState }> {
  return request('/profiles', 'POST', trigger ? { name, kind: 'automatic', trigger } : { name });
}
export function updateProfile(id: string, patch: { name?: string; trigger?: Trigger }): Promise<{ profile: Profile; profileState: ProfileState }> {
  return request(`/profiles/${encodeURIComponent(id)}`, 'PATCH', patch);
}
export function deleteProfile(id: string): Promise<ProfileList> {
  return request(`/profiles/${encodeURIComponent(id)}`, 'DELETE');
}
/** Deletes profiles, saved device settings and key assignments; the language choice stays. */
export function clearData(): Promise<ProfileList & { assignments: AssignmentState }> {
  return request('/data', 'DELETE');
}
export function activateProfile(id: string): Promise<{ devices: Device[]; profile: Profile; profileState: ProfileState }> {
  return request(`/profiles/${encodeURIComponent(id)}/activate`, 'POST', {});
}

/** devicesRevision changes when devices come, go or get their saved settings back. */
export function getHealth(): Promise<{ mode: 'simulation' | 'hardware'; devicesRevision?: number }> { return request('/health'); }
export function getPreferences(): Promise<Preferences> { return request('/preferences'); }
export function savePreferences(language: Language): Promise<Preferences> {
  return request('/preferences', 'PUT', { language });
}
export function saveStartWithWindows(enabled: boolean): Promise<Preferences> {
  return request('/preferences', 'PUT', { startWithWindows: enabled });
}
export type ActionType = 'default' | 'disabled' | 'open' | 'url' | 'media' | 'shortcut';
export interface Assignment { type: ActionType; path?: string; url?: string; key?: string; preset?: string }
export interface AssignmentState {
  available: boolean;
  error: string | null;
  filePicker: boolean;
  media: string[];
  shortcuts: string[];
  devices: Record<string, { controls: string[]; assignments: Record<string, Assignment> }>;
}
export function getAssignments(): Promise<AssignmentState> { return request('/assignments'); }
export function saveAssignment(deviceId: string, control: string, action: Assignment): Promise<AssignmentState> {
  return request(`/assignments/${encodeURIComponent(deviceId)}/${encodeURIComponent(control)}`, 'PUT', action, 20000);
}
/** Opens the native Windows file dialog; waits while the user chooses. */
export async function pickFile(): Promise<string | null> {
  return (await request<{ path: string | null }>('/dialogs/open-file', 'POST', {}, 10 * 60 * 1000)).path;
}
