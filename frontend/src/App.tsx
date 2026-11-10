import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { activateProfile, clearData, createProfile, deleteProfile, getAssignments, getDevices, getHealth, getPreferences, getProfiles, pickFile, saveAssignment, savePreferences, saveStartWithWindows, updateProfile, updateSettings, type Assignment, type AssignmentState, type ProfileList } from './api';
import type { Device, Language, Profile, ProfileState, Settings, Trigger } from './types';
import { DEVICE_PARTS, DeviceScene } from './scene';
import { Icon, type IconName } from './ui/Icon';
import { AssignmentPanel, Battery, GeneralPanel, LightingPanel, controlOf, partLabel } from './ui/PartPanels';
import { ProfilesMenu } from './ui/ProfilesMenu';
import { I18nContext, LocalizedError, createI18n, errorText, formatPercent, initialLanguage, languageNames, languages, useI18n, type MessageKey } from './i18n';
import './styles.css';

type Notice = { key: MessageKey; values?: Record<string, string | number> };
type Inset = { right: number; bottom: number };
type DeviceId = keyof typeof DEVICE_PARTS;
const deviceOrder: DeviceId[] = ['g733', 'mx-keys-mini', 'aerox-3'];
const deviceIcons: Record<Device['kind'], IconName> = { keyboard: 'keyboard', headset: 'headset', mouse: 'mouse' };
// Shown in 3D only when no lighting data is reported; labeled as simulated in the UI.
const simulatedLighting: Partial<Record<Device['id'], Settings>> = {
  g733: { lightingColor: '#8B5CF6', lightingBrightness: 70 },
  'aerox-3': { lightingColor: '#22D3EE', lightingBrightness: 80 },
};
const narrowQuery = '(max-width: 760px)';
const noInset: Inset = { right: 0, bottom: 0 };
const acknowledgedOnly = (item: Device) => item.telemetry?.writeVerification === 'acknowledged';
// Devices that cannot read settings back show the last values sent from this app.
const baseline = (item: Device): Settings => acknowledgedOnly(item) ? { ...item.desiredSettings } : item.settings;
const isCharging = (item: Device) => item.telemetry?.charging === true;
const partsOf = (id: string) => DEVICE_PARTS[id as DeviceId] ?? ['body'];
const panelKind = (part: string) => part.startsWith('zone:') ? 'lighting' : part === 'body' ? 'general' : 'assignment';

export function App() {
  const [mode, setMode] = useState<'simulation' | 'hardware'>('hardware');
  const [language, setLanguage] = useState<Language>(initialLanguage);
  const [devices, setDevices] = useState<Device[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [assignments, setAssignments] = useState<AssignmentState | null>(null);
  // null is the desk overview; a device focus shows its parts; a part opens its settings.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedPart, setSelectedPart] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [hoveredPart, setHoveredPart] = useState<string | null>(null);
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [startWithWindows, setStartWithWindows] = useState<boolean | undefined>(undefined);
  // The desktop host sends upm:pause before it suspends a minimized window: dropping the 3D scene
  // then frees its GPU memory; upm:resume rebuilds it.
  const [sceneActive, setSceneActive] = useState(true);
  // A lost WebGL context (GPU reset after sleep or a driver update) leaves a dark canvas: build a new one.
  const [sceneGeneration, setSceneGeneration] = useState(0);
  const rebuildScene = useCallback(() => { window.setTimeout(() => setSceneGeneration(value => value + 1), 1000); }, []);
  const [draft, setDraft] = useState<Settings>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  // Without an active profile the settings are the unsaved custom workspace.
  const [profileState, setProfileState] = useState<ProfileState | null>(null);
  const [inset, setInset] = useState<Inset>(noInset);
  const requestLock = useRef(false);
  const refreshing = useRef(false);
  const revision = useRef<number | undefined>(undefined);
  const generation = useRef(0);
  const activeId = useRef<string | null | undefined>(undefined);
  const lowBatteries = useRef('');
  const languageChosen = useRef(false);
  const panelRef = useRef<HTMLElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const i18n = useMemo(() => createI18n(language), [language]);
  const { t } = i18n;
  const sceneLabels = useMemo(() => ({ canvas: t('scene.canvas'), unavailable: t('scene.unavailable'), webgl: t('scene.webgl') }), [t]);
  const ordered = useMemo(() => [...devices].sort((a, b) => deviceOrder.indexOf(a.id) - deviceOrder.indexOf(b.id)), [devices]);
  const device = devices.find(item => item.id === selectedId);
  const hovered = devices.find(item => item.id === hoveredId);

  useEffect(() => { document.documentElement.lang = language; }, [language]);
  useEffect(() => {
    // The saved choice (or Windows language) wins unless the user already switched.
    getPreferences().then(preferences => {
      if (!languageChosen.current) setLanguage(preferences.language);
      setStartWithWindows(preferences.startWithWindows);
    }).catch(() => undefined);
  }, []);
  function changeStartWithWindows(enabled: boolean) {
    void perform(async () => {
      const preferences = await saveStartWithWindows(enabled);
      setStartWithWindows(preferences.startWithWindows);
      setNotice({ key: preferences.startWithWindows ? 'notice.startupOn' : 'notice.startupOff' });
    });
  }
  function changeLanguage(next: Language) {
    if (next === language) return;
    languageChosen.current = true;
    setLanguage(next);
    savePreferences(next).catch(() => setError(new LocalizedError('language.saveFailed')));
  }

  // Remembers the active profile: a later refresh tells when a rule switched it.
  const showProfiles = useCallback((list: ProfileList) => {
    setProfiles(list.profiles); setProfileState(list.profileState);
    activeId.current = list.profileState.activeProfileId;
  }, []);
  // Background (quiet) refreshes never block clicks; a change made meanwhile wins over their result.
  const refresh = useCallback(async (quiet = false) => {
    if (requestLock.current || (quiet && refreshing.current)) return;
    const started = generation.current;
    if (quiet) refreshing.current = true;
    else { requestLock.current = true; setBusy(true); setError(null); }
    try {
      const [nextDevices, profileList, health, nextAssignments] = await Promise.all([getDevices(), getProfiles(), getHealth(), getAssignments()]);
      if (quiet && (started !== generation.current || requestLock.current)) return;
      setError(null);
      setMode(health.mode);
      revision.current = health.devicesRevision;
      const previousActive = activeId.current;
      showProfiles(profileList);
      setDevices(nextDevices); setAssignments(nextAssignments);
      const low = nextDevices.filter(item => item.batteryPercent !== null && item.batteryPercent <= 20 && !isCharging(item)).map(item => item.name).join(', ');
      const switched = profileList.profileState.activeBy === 'rule' || profileList.profileState.activeBy === 'main'
        ? profileList.profiles.find(item => item.id === profileList.profileState.activeProfileId) : undefined;
      if (low && low !== lowBatteries.current) setNotice({ key: 'notice.lowBattery', values: { names: low } });
      else if (quiet && previousActive !== undefined && switched && switched.id !== previousActive) setNotice({ key: 'notice.profileSwitched', values: { name: switched.name } });
      else if (!quiet) setNotice({ key: `notice.connected.${health.mode}` });
      lowBatteries.current = low;
    } catch (failure) { setError(failure); }
    finally {
      if (quiet) refreshing.current = false;
      else { requestLock.current = false; setBusy(false); }
      setLoading(false);
    }
  }, [showProfiles]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { setDraft(device ? baseline(device) : {}); }, [device]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 7000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function perform(operation: () => Promise<void>) {
    if (requestLock.current) return;
    generation.current += 1;
    requestLock.current = true; setBusy(true); setError(null); setNotice(null);
    try { await operation(); }
    catch (failure) { setError(failure); }
    finally { requestLock.current = false; setBusy(false); }
  }
  const changes = useMemo(() => {
    if (!device) return {} as Settings;
    const base = baseline(device) as Record<string, unknown>;
    return Object.fromEntries(Object.entries(draft).filter(([key, value]) => JSON.stringify(base[key]) !== JSON.stringify(value))) as Settings;
  }, [device, draft]);
  const dirty = Object.keys(changes).length > 0;
  useEffect(() => {
    // Restored after a while: the device list may be stale.
    const pause = () => setSceneActive(false), resume = () => { setSceneActive(true); void refresh(true); };
    window.addEventListener('upm:pause', pause);
    window.addEventListener('upm:resume', resume);
    return () => { window.removeEventListener('upm:pause', pause); window.removeEventListener('upm:resume', resume); };
  }, [refresh]);
  useEffect(() => {
    if (dirty || busy) return;
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void refresh(true); }, 15000);
    // A device came, went or got its settings back: show it within seconds. Health is cheap; a
    // device scan is not, so the full refresh runs only when the revision changed.
    const watch = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      getHealth().then(health => {
        if (revision.current !== undefined && health.devicesRevision !== revision.current) void refresh(true);
      }).catch(() => undefined);
    }, 3000);
    return () => { window.clearInterval(timer); window.clearInterval(watch); };
  }, [dirty, busy, refresh]);

  const displayDevices = useMemo(() => devices.map(item => {
    const settings = item.id === device?.id ? draft : baseline(item);
    const fallback = simulatedLighting[item.id];
    return fallback && item.connection !== 'simulated' && settings.lightingBrightness === undefined
      ? { ...item, settings: { ...fallback, ...settings } } : { ...item, settings };
  }), [devices, device, draft]);
  // The 3D keyboard shows the reported backlight; without data, a labeled simulated level.
  const keyboard = displayDevices.find(item => item.id === 'mx-keys-mini');
  const keyboardBacklight = keyboard?.settings.backlightMode === 'off' ? 0 : keyboard?.settings.backlightLevel === undefined ? 60
    : Math.round(keyboard.settings.backlightLevel / Math.max(1, keyboard.limits.backlightLevel?.max ?? 7) * 100);

  const discard = useCallback(() => { if (dirty) setNotice({ key: 'notice.discarded' }); }, [dirty]);
  const selectDevice = useCallback((id: string) => {
    if (requestLock.current || id === selectedId) return;
    discard();
    setSelectedId(id); setSelectedPart(null); setHoveredPart(null);
  }, [selectedId, discard]);
  const selectPart = useCallback((part: string) => {
    if (requestLock.current || part === selectedPart) return;
    discard();
    if (device) setDraft(baseline(device));
    setSelectedPart(part);
  }, [selectedPart, discard, device]);
  const goBack = useCallback(() => {
    discard();
    if (selectedPart) { setSelectedPart(null); if (device) setDraft(baseline(device)); }
    else { setSelectedId(null); setHoveredPart(null); }
  }, [discard, selectedPart, device]);
  useEffect(() => {
    if (selectedId === null && !profilesOpen && !settingsOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      event.preventDefault();
      if (settingsOpen) setSettingsOpen(false); else if (profilesOpen) setProfilesOpen(false); else goBack();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedId, profilesOpen, settingsOpen, goBack]);

  // The scene frames its subject in the canvas area not covered by UI.
  const focused = !!device;
  const partOpen = focused && selectedPart !== null;
  useEffect(() => {
    const measure = () => {
      const panel = panelRef.current, overlay = overlayRef.current;
      let next = noInset;
      if (panel) next = window.matchMedia(narrowQuery).matches ? { right: 0, bottom: panel.offsetHeight + 8 } : { right: panel.offsetWidth + 16, bottom: 0 };
      else if (overlay) next = { right: 0, bottom: overlay.offsetHeight };
      setInset(previous => previous.right === next.right && previous.bottom === next.bottom ? previous : next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    for (const element of [panelRef.current, overlayRef.current]) if (element) observer.observe(element);
    window.addEventListener('resize', measure);
    return () => { observer.disconnect(); window.removeEventListener('resize', measure); };
  }, [focused, partOpen, devices.length]);

  async function applyChanges() {
    if (!device || !dirty) return;
    // Devices without readback receive the whole lighting state so it stays consistent.
    const patch = acknowledgedOnly(device) ? draft : changes;
    await perform(async () => {
      const updated = await updateSettings(device.id, patch);
      setDevices(previous => previous.map(item => item.id === updated.id ? updated : item));
      setNotice({ key: acknowledgedOnly(device) ? 'notice.applied.sent' : `notice.applied.${mode}` });
    });
  }
  async function storeAssignment(action: Assignment) {
    if (!device || !selectedPart) return;
    await perform(async () => {
      setAssignments(await saveAssignment(device.id, controlOf(selectedPart), action));
      setNotice({ key: 'assign.saved' });
    });
  }
  async function chooseFile() {
    try { return await pickFile(); }
    catch (failure) { setError(failure); return null; }
  }
  async function saveProfile(name: string, trigger?: Trigger) {
    await perform(async () => {
      const { profile } = await createProfile(name, trigger);
      showProfiles(await getProfiles());  // the first automatic profile may also create a main one
      setNotice({ key: trigger ? 'notice.automaticSaved' : 'notice.profileSaved', values: { name: profile.name } });
    });
  }
  function saveRule(profile: Profile, trigger: Trigger) {
    void perform(async () => {
      await updateProfile(profile.id, { trigger });
      showProfiles(await getProfiles());
      setNotice({ key: 'notice.ruleSaved', values: { name: profile.name } });
    });
  }
  function removeProfile(profile: Profile) {
    const wasActive = profile.id === profileState?.activeProfileId;
    void perform(async () => {
      showProfiles(await deleteProfile(profile.id));
      setNotice({ key: 'notice.profileDeleted', values: { name: profile.name } });
    }).then(() => { if (wasActive) void refresh(true); });  // the main profile applies again
  }
  async function eraseData() {
    await perform(async () => {
      const result = await clearData();
      showProfiles(result); setAssignments(result.assignments);
      setNotice({ key: 'notice.dataCleared' });
    });
    void refresh(true);  // the last sent settings shown for the mouse are gone too
  }
  function applyProfile(profile: Profile) {
    void perform(async () => {
      const result = await activateProfile(profile.id);
      setDevices(result.devices);
      showProfiles({ profiles, profileState: result.profileState });
      setNotice({ key: `notice.profileActivated.${mode}`, values: { name: result.profile.name } });
    });
  }
  const activeName = profiles.find(item => item.id === profileState?.activeProfileId)?.name;

  const kind = selectedPart ? panelKind(selectedPart) : null;
  const parts = device ? partsOf(device.id) : [];
  const caption = hoveredPart ? partLabel(t, hoveredPart) : t('focus.hint');

  return <I18nContext.Provider value={i18n}><div className="showroom" data-view={partOpen ? 'part' : focused ? 'focus' : 'desk'} style={{ '--panel-space': `${inset.right}px` } as CSSProperties}>
    <div className="stage">
      {!sceneActive ? null : devices.length > 0 ? <DeviceScene key={sceneGeneration} onContextLost={rebuildScene} devices={displayDevices} selectedId={device?.id ?? null} selectedPart={partOpen ? selectedPart : null} onSelect={selectDevice} onSelectPart={selectPart} onHover={setHoveredId} onHoverPart={setHoveredPart} keyboardBacklight={keyboardBacklight} inset={inset} labels={sceneLabels} /> : <div className="stage-empty"><Icon name="grid" size={36} /><p>{loading ? t('scene.loading') : t('scene.offline')}</p></div>}
    </div>

    <header className="showroom-bar"><div className="wordmark">PERIPHERAL<span> / </span><span className="muted">STUDIO</span></div><div className="topbar-right"><button className={`chip-button ${profilesOpen ? 'active' : ''}`} aria-expanded={profilesOpen} onClick={() => { setSettingsOpen(false); setProfilesOpen(open => !open); }}><Icon name="profiles" size={14} />{t('profiles.menu')}</button><button className={`chip-button ${settingsOpen ? 'active' : ''}`} aria-expanded={settingsOpen} onClick={() => { setProfilesOpen(false); setSettingsOpen(open => !open); }}><Icon name="sliders" size={14} />{t('settings.menu')}</button><div className="language-switch" role="group" aria-label={t('language.label')}>{languages.map(code => <button key={code} type="button" lang={code} aria-label={languageNames[code]} aria-pressed={language === code} className={language === code ? 'active' : ''} onClick={() => changeLanguage(code)}>{code.toUpperCase()}</button>)}</div><span className="simulation-badge"><i />{t(`badge.${mode}`)}</span></div></header>
    {settingsOpen && <div className="profiles-menu" role="dialog" aria-label={t('settings.menu')}>
      <div className="profiles-menu-head"><strong>{t('settings.title')}</strong><button className="text-button" onClick={() => setSettingsOpen(false)}>{t('action.close')}</button></div>
      <label className="setting-switch"><input type="checkbox" role="switch" checked={startWithWindows === true} disabled={busy || startWithWindows === undefined} onChange={event => changeStartWithWindows(event.target.checked)} /><span><strong>{t('settings.startup')}</strong><small>{startWithWindows === undefined ? t('settings.startupUnavailable') : t('settings.startupHint')}</small></span></label>
    </div>}
    {profilesOpen && <ProfilesMenu profiles={profiles} state={profileState} busy={busy} dirty={dirty} onPickFile={assignments?.filePicker ? chooseFile : undefined} onActivate={applyProfile} onCreate={(name, trigger) => void saveProfile(name, trigger)} onSaveRule={saveRule} onDelete={removeProfile} onClearData={() => void eraseData()} onClose={() => setProfilesOpen(false)} />}

    <div className="toast-stack">
      {error !== null && <div className="error-banner" role="alert"><strong>{t('error.title')}</strong> {errorText(language, error)}<button className="text-button" onClick={() => void refresh()} disabled={busy}>{t('error.retry')}</button></div>}
      <div className="status-message" role="status" aria-live="polite">{notice ? t(notice.key, notice.values) : ''}</div>
    </div>

    {devices.length > 0 && !partOpen && <div className="desk-overlay" ref={overlayRef}>
      {!focused ? <>
        <div className="desk-caption">
          {hovered ? <><strong>{hovered.name}</strong><span>{hovered.batteryPercent === null ? t('battery.unreadable') : formatPercent(language, hovered.batteryPercent)}{isCharging(hovered) ? ` · ${t('battery.charging')}` : ''} · {t('desk.open')}</span></> : <><strong>{t('desk.hint')}</strong><span>{t('desk.lightingSimulated')}</span></>}
        </div>
        <nav className="device-dock" aria-label={t('desk.devices')}>{ordered.map(item => <button className={`device-card ${item.id === hoveredId ? 'hovered' : ''}`} key={item.id} disabled={busy} onClick={() => selectDevice(item.id)}><Icon name={deviceIcons[item.kind]} size={18} /><strong>{item.name}</strong><Battery value={item.batteryPercent} charging={isCharging(item)} /></button>)}</nav>
      </> : device && <div className="focus-bar">
        <div className="focus-head"><button className="back-button" onClick={goBack}><Icon name="arrow" size={16} />{t('focus.back')}<kbd>Esc</kbd></button><strong>{device.name}</strong><Battery value={device.batteryPercent} charging={isCharging(device)} /></div>
        <p className="focus-caption">{caption}</p>
        <nav className="part-chips" aria-label={t('focus.parts')}>{parts.map(part => <button key={part} className={`part-chip ${part === hoveredPart ? 'hovered' : ''}`} onClick={() => selectPart(part)}>{partLabel(t, part)}</button>)}</nav>
      </div>}
    </div>}

    {device && partOpen && selectedPart && <aside className="settings-panel" aria-label={t('panel.label')} ref={panelRef} key={`${device.id}:${selectedPart}`}>
      <div className="settings-header"><div className="panel-top"><button className="back-button" onClick={goBack}><Icon name="arrow" size={16} />{device.name}<kbd>Esc</kbd></button><Battery value={device.batteryPercent} charging={isCharging(device)} /></div><div className="device-heading"><h2>{partLabel(t, selectedPart)}</h2><Icon name={kind === 'lighting' ? 'sparkles' : kind === 'assignment' ? 'keys' : deviceIcons[device.kind]} size={26} /></div></div>
      <div className="settings-content" id="settings-content">
        {device.connection !== 'simulated' && device.status?.code !== 'connected' && <div className="notice" role="status">{t('device.offline')}</div>}
        {kind === 'lighting' && <LightingPanel device={device} zone={selectedPart.slice(5)} draft={draft} setDraft={setDraft} busy={busy} mode={mode} />}
        {kind === 'general' && <GeneralPanel device={device} draft={draft} setDraft={setDraft} busy={busy} mode={mode} />}
        {kind === 'assignment' && <AssignmentPanel device={device} control={controlOf(selectedPart)} state={assignments} busy={busy} onSave={action => void storeAssignment(action)} onPickFile={chooseFile} />}
      </div>
      {kind !== 'assignment' && <div className="settings-footer"><div><span className="eyebrow">{dirty ? t('footer.unsaved') : activeName ? t('footer.profile') : t('footer.current')}</span><strong>{activeName ?? t('workspace.custom')}</strong></div><div className="footer-actions">{dirty && <button className="text-button" disabled={busy} onClick={() => setDraft(baseline(device))}>{t('action.reset')}</button>}<button className="button primary" disabled={!dirty || busy} onClick={() => void applyChanges()}>{busy ? t('action.working') : t('action.apply')}<Icon name="check" size={16} /></button></div></div>}
    </aside>}
  </div></I18nContext.Provider>;
}

export default App;
