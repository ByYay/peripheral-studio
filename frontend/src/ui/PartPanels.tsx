import { useEffect, useState, type CSSProperties, type Dispatch, type SetStateAction } from 'react';
import type { Assignment, AssignmentState, ActionType } from '../api';
import type { Device, LightingEffect, LightingSpeed, Settings } from '../types';
import { Icon, type IconName } from './Icon';
import { formatPercent, translateServerMessage, useI18n, type MessageKey } from '../i18n';

type Mode = 'simulation' | 'hardware';
type SetDraft = Dispatch<SetStateAction<Settings>>;
// Fully saturated: pastel values look nearly white on RGB LEDs.
const swatches = ['#ff0000', '#ff6a00', '#ffd000', '#00ff3c', '#00e1ff', '#0050ff', '#8c00ff', '#ff0090', '#ffffff'];
const backlightModes = ['off', 'auto', 'manual'] as const;
const speeds: LightingSpeed[] = ['slow', 'normal', 'fast'];
const actionTypes: { type: ActionType; icon: IconName }[] = [
  { type: 'default', icon: 'refresh' }, { type: 'open', icon: 'grid' }, { type: 'url', icon: 'arrow' },
  { type: 'media', icon: 'sparkles' }, { type: 'shortcut', icon: 'keys' }, { type: 'disabled', icon: 'trash' },
];
const acknowledgedOnly = (device: Device) => device.telemetry?.writeVerification === 'acknowledged';

export function controlOf(part: string) {
  return part.startsWith('key:') ? part.slice(4) : part.startsWith('button:') ? part.slice(7) : part;
}

export function partLabel(t: ReturnType<typeof useI18n>['t'], part: string) {
  if (part.startsWith('zone:')) return t(`part.zone.${part.slice(5)}` as MessageKey);
  if (part.startsWith('key:')) return t('part.key', { key: part === 'key:INS' ? 'Ins' : part.slice(4) });
  if (part.startsWith('button:')) return t(`part.button.${part.slice(7)}` as MessageKey);
  return t(part === 'dpi' ? 'part.dpi' : 'part.body');
}

export function Battery({ value, charging }: { value: number | null; charging?: boolean }) {
  const { language, t } = useI18n();
  if (value === null) return <span className="battery" aria-label={t('battery.unavailableLabel')}>{t('battery.unknown')}</span>;
  const percent = Math.max(0, Math.min(100, value));
  const label = formatPercent(language, percent);
  return <span className={`battery ${percent <= 20 && !charging ? 'low' : ''} ${charging ? 'charging' : ''}`} aria-label={`${t('battery.label', { percent: label })}${charging ? `, ${t('battery.charging')}` : ''}`}>
    <span className="battery-outline"><span style={{ width: `${percent}%` }} /></span>{charging && <Icon name="bolt" size={12} />}<span>{label}</span>
  </span>;
}

function Notice({ children }: { children: string }) {
  return <div className="notice">{children}</div>;
}

export function LightingPanel({ device, zone, draft, setDraft, busy, mode }: { device: Device; zone: string | null; draft: Settings; setDraft: SetDraft; busy: boolean; mode: Mode }) {
  const { language, t } = useI18n();
  const zones = device.limits.lightingZones ?? [];
  const zoneSupported = !!zone && zones.includes(zone);
  const [scope, setScope] = useState<'zone' | 'all'>(zoneSupported ? 'zone' : 'all');
  useEffect(() => { setScope(zoneSupported ? 'zone' : 'all'); }, [zone, zoneSupported]);
  if (!device.capabilities.lighting) {
    return <div className="empty-state"><Icon name="sparkles" size={34} /><h3>{t('lighting.none.title')}</h3><p>{t('lighting.none.hint')}</p></div>;
  }
  const current = (scope === 'zone' && zone ? draft.lightingZones?.[zone] : undefined) ?? draft.lightingColor ?? '#8ce8cc';
  const effects = device.limits.lightingEffects ?? [];
  function setColor(color: string) {
    setDraft(previous => {
      if (scope === 'all' || !zone) {
        return { ...previous, lightingColor: color, ...(zones.length ? { lightingZones: Object.fromEntries(zones.map(id => [id, color])) } : {}) };
      }
      const filled = Object.fromEntries(zones.map(id => [id, previous.lightingZones?.[id] ?? previous.lightingColor ?? color]));
      return { ...previous, lightingZones: { ...filled, [zone]: color } };
    });
  }
  return <div className="editor-stack">
    {zoneSupported && <div className="segmented" role="group" aria-label={t('zone.scope')}>
      <button type="button" aria-pressed={scope === 'zone'} onClick={() => setScope('zone')}>{t('zone.this')}</button>
      <button type="button" aria-pressed={scope === 'all'} onClick={() => setScope('all')}>{t('zone.all')}</button>
    </div>}
    <div className="color-preview" style={{ '--chosen-color': current } as CSSProperties}><span /><small>{t('lighting.signature')}</small><strong>{current.toUpperCase()}</strong></div>
    <div className="color-swatches">{swatches.map(color => <button key={color} disabled={busy} aria-label={t('lighting.swatch', { color })} aria-pressed={current.toLowerCase() === color} className={current.toLowerCase() === color ? 'chosen' : ''} style={{ backgroundColor: color }} onClick={() => setColor(color)} />)}<label className="custom-color" title={t('lighting.custom')}><Icon name="plus" size={18} /><input type="color" aria-label={t('lighting.custom')} disabled={busy} value={current} onChange={event => setColor(event.target.value)} /></label></div>
    <label className="control-label">{t('lighting.brightness')}<output>{formatPercent(language, draft.lightingBrightness ?? 0)}</output><input type="range" aria-label={t('lighting.brightness')} min="0" max="100" value={draft.lightingBrightness ?? 0} disabled={busy} onChange={event => setDraft(previous => ({ ...previous, lightingBrightness: Number(event.target.value) }))} /></label>
    {effects.length > 1 && <div className="editor-stack compact"><span className="field-label">{t('effect.label')}</span><div className="segmented" role="group" aria-label={t('effect.label')}>{effects.map(effect => <button key={effect} type="button" disabled={busy} aria-pressed={(draft.lightingEffect ?? 'static') === effect} onClick={() => setDraft(previous => ({ ...previous, lightingEffect: effect as LightingEffect }))}>{t(`effect.${effect}` as MessageKey)}</button>)}</div></div>}
    {draft.lightingEffect && device.limits.lightingSpeedEffects?.includes(draft.lightingEffect) && <div className="editor-stack compact"><span className="field-label">{t('speed.label')}</span><div className="segmented" role="group" aria-label={t('speed.label')}>{speeds.map(speed => <button key={speed} type="button" disabled={busy} aria-pressed={(draft.lightingSpeed ?? 'normal') === speed} onClick={() => setDraft(previous => ({ ...previous, lightingSpeed: speed }))}>{t(`speed.${speed}`)}</button>)}</div></div>}
    {device.id === 'aerox-3' && draft.lightingEffect === 'breathing' && mode === 'hardware' && <Notice>{t('effect.softwareNote')}</Notice>}
    {device.id === 'g733' && zones.length > 0 && <Notice>{t('lighting.headsetSides')}</Notice>}
    {device.id === 'g733' && mode === 'hardware' && <Notice>{t('lighting.g733Hint')}</Notice>}
    {acknowledgedOnly(device) && <Notice>{t('verification.acknowledged')}</Notice>}
  </div>;
}

export function GeneralPanel({ device, draft, setDraft, busy, mode }: { device: Device; draft: Settings; setDraft: SetDraft; busy: boolean; mode: Mode }) {
  const { language, t } = useI18n();
  const battery = device.batteryPercent;
  const charging = device.telemetry?.charging === true;
  const millivolts = device.telemetry?.batteryMillivolts;
  const sign = <small>{battery === null ? '' : '%'}</small>;
  const backlight = device.limits.backlightLevel;
  const fnKeys = device.telemetry?.fnKeys;
  return <div className="editor-stack">
    <div className={`battery-card ${charging ? 'charging' : ''}`}><div><span className="eyebrow">{mode === 'simulation' ? t('battery.simulated') : t('battery.device')}</span><strong>{language === 'tr' && sign}{battery ?? '—'}{language !== 'tr' && sign}</strong><span>{battery === null ? t('battery.unreadable') : charging ? t('battery.charging') : typeof millivolts === 'number' ? t('battery.voltageEstimate', { mv: millivolts }) : battery <= 20 ? t('battery.low') : t('battery.reported')}</span></div><Icon name={charging ? 'bolt' : 'battery'} size={52} /></div>
    {device.capabilities.dpi && device.limits.dpi && <label className="control-label">{t('dpi.label')} <output>{draft.dpi ?? '—'} <span>DPI</span></output><input type="range" aria-label={t('dpi.label')} min={device.limits.dpi.min} max={device.limits.dpi.max} step={device.limits.dpi.step} value={draft.dpi ?? device.limits.dpi.min} disabled={busy} onChange={event => setDraft(previous => ({ ...previous, dpi: Number(event.target.value) }))} /><span className="range-labels"><span>{device.limits.dpi.min} DPI</span><span>{device.limits.dpi.max} DPI</span></span></label>}
    {device.capabilities.pollingRate && device.limits.pollingRates && <label>{t('polling.label')}<select value={draft.pollingRate ?? ''} disabled={busy} onChange={event => setDraft(previous => ({ ...previous, pollingRate: Number(event.target.value) }))}>{draft.pollingRate === undefined && <option value="" disabled>—</option>}{device.limits.pollingRates.map(rate => <option key={rate} value={rate}>{rate} Hz</option>)}</select></label>}
    {device.kind === 'keyboard' && (device.capabilities.backlight && backlight ? <>
      <label>{t('backlight.mode')}<select value={draft.backlightMode ?? ''} disabled={busy} onChange={event => setDraft(previous => ({ ...previous, backlightMode: event.target.value as Settings['backlightMode'] }))}>{draft.backlightMode === undefined && <option value="" disabled>—</option>}{backlightModes.map(item => <option key={item} value={item}>{t(`backlight.mode.${item}`)}</option>)}</select></label>
      <label className="control-label">{t('backlight.levelLabel')}<output>{t('backlight.levelValue', { level: draft.backlightLevel ?? '—', max: backlight.max })}</output><input type="range" aria-label={t('backlight.levelLabel')} min={backlight.min} max={backlight.max} step={backlight.step} value={draft.backlightLevel ?? backlight.min} disabled={busy || draft.backlightMode !== 'manual'} onChange={event => setDraft(previous => ({ ...previous, backlightLevel: Number(event.target.value) }))} /></label>
      {draft.backlightMode === 'auto' && device.settings.backlightLevel !== undefined && <p className="empty-hint">{t('backlight.autoLevel', { level: device.settings.backlightLevel, max: backlight.max })}</p>}
      <Notice>{mode === 'simulation' ? t('backlight.simulated') : t('backlight.hardware')}</Notice></>
      : <Notice>{t('backlight.unavailable.hint')}</Notice>)}
    {fnKeys && <div className="fn-mode"><span className="field-label">{t('fn.label')}</span><strong>{t(`fn.${fnKeys}`)}</strong><small>{t('fn.toggle')}</small></div>}
    {acknowledgedOnly(device) && <Notice>{t('verification.acknowledged')}</Notice>}
    <Notice>{mode === 'simulation' ? t('overview.simulationNotice') : device.status?.message ? translateServerMessage(language, device.status.message) : t('overview.hardwareNotice')}</Notice>
  </div>;
}

export function AssignmentPanel({ device, control, state, busy, onSave, onPickFile }: { device: Device; control: string; state: AssignmentState | null; busy: boolean; onSave: (action: Assignment) => void; onPickFile: () => Promise<string | null> }) {
  const { t } = useI18n();
  const assignable = state?.devices[device.id]?.controls.includes(control) ?? false;
  const saved = state?.devices[device.id]?.assignments[control] ?? { type: 'default' as ActionType };
  const [choice, setChoice] = useState<Assignment>(saved);
  useEffect(() => { setChoice(saved); }, [device.id, control, JSON.stringify(saved)]);
  if (!assignable) return <div className="empty-state"><Icon name="keys" size={34} /><h3>{t('assign.notYetTitle')}</h3><p>{t('assign.notYet')}</p></div>;
  const media = state?.media ?? [];
  const shortcuts = state?.shortcuts ?? [];
  const complete = choice.type === 'open' ? !!choice.path : choice.type === 'url' ? /^https?:\/\/\S+$/i.test(choice.url ?? '')
    : choice.type === 'media' ? !!choice.key : choice.type === 'shortcut' ? !!choice.preset : true;
  const changed = JSON.stringify(choice) !== JSON.stringify(saved);
  function select(type: ActionType) {
    setChoice(type === saved.type ? saved : type === 'media' ? { type, key: media[0] } : type === 'shortcut' ? { type, preset: shortcuts[0] } : type === 'url' ? { type, url: '' } : type === 'open' ? { type, path: '' } : { type });
  }
  return <div className="editor-stack">
    {!state?.available && <Notice>{t('assign.unavailable')}</Notice>}
    {state?.error && <div className="notice" role="alert">{state.error}</div>}
    <div className="action-grid" role="radiogroup" aria-label={t('assign.title')}>{actionTypes.map(item => <button key={item.type} type="button" role="radio" aria-checked={choice.type === item.type} className={choice.type === item.type ? 'chosen' : ''} disabled={busy} onClick={() => select(item.type)}><Icon name={item.icon} size={18} /><span>{t(`action.${item.type}` as MessageKey)}</span></button>)}</div>
    {choice.type === 'open' && <div className="file-pick"><button className="button secondary" type="button" disabled={busy || !state?.filePicker} onClick={() => void onPickFile().then(path => { if (path) setChoice({ type: 'open', path }); })}><Icon name="plus" size={16} />{t('assign.browse')}</button><span title={choice.path}>{choice.path || t('assign.noFile')}</span></div>}
    {choice.type === 'url' && <label>{t('action.url')}<input value={choice.url ?? ''} placeholder="https://" maxLength={2048} disabled={busy} onChange={event => setChoice({ type: 'url', url: event.target.value.trim() })} /></label>}
    {choice.type === 'media' && <label>{t('action.media')}<select value={choice.key} disabled={busy} onChange={event => setChoice({ type: 'media', key: event.target.value })}>{media.map(key => <option key={key} value={key}>{t(`media.${key}` as MessageKey)}</option>)}</select></label>}
    {choice.type === 'shortcut' && <label>{t('action.shortcut')}<select value={choice.preset} disabled={busy} onChange={event => setChoice({ type: 'shortcut', preset: event.target.value })}>{shortcuts.map(key => <option key={key} value={key}>{t(`shortcut.${key}` as MessageKey)}</option>)}</select></label>}
    {device.id === 'aerox-3' && control === 'dpi' && choice.type !== 'default' && <Notice>{t('assign.dpiNote')}</Notice>}
    {device.kind === 'headset' && <Notice>{t('assign.headsetButtons')}</Notice>}
    {device.kind === 'keyboard' && <Notice>{t('assign.keyNote')}</Notice>}
    {device.kind === 'keyboard' && device.telemetry?.fnKeys === 'standard' && <Notice>{t('assign.keyFnLock')}</Notice>}
    {device.telemetry?.sharedKeys?.includes(control) && <div className="notice" role="alert">{t('assign.keyShared')}</div>}
    <button className="button primary" type="button" disabled={busy || !complete || !changed} onClick={() => onSave(choice)}>{t('assign.save')}<Icon name="check" size={16} /></button>
  </div>;
}
