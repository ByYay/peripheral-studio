import { useState } from 'react';
import type { Profile, ProfileState, Trigger } from '../types';
import { useI18n } from '../i18n';
import { Icon } from './Icon';

const defaultTrigger: Trigger = { type: 'time', start: '22:00', end: '07:00' };
const MAX_APPS = 16;
// Windows reports running programs by file name; a chosen path is reduced to it.
const fileName = (path: string) => (path.split(/[\\/]/).pop() ?? '').trim().toLowerCase();
const validTrigger = (trigger: Trigger) => trigger.type === 'time'
  ? /^\d\d:\d\d$/.test(trigger.start) && /^\d\d:\d\d$/.test(trigger.end) && trigger.start !== trigger.end
  : trigger.apps.length > 0;

function RuleFields({ value, onChange, busy, onPickFile }: { value: Trigger; onChange: (trigger: Trigger) => void; busy: boolean; onPickFile?: () => Promise<string | null> }) {
  const { t } = useI18n();
  const [typed, setTyped] = useState('');
  const apps = value.type === 'app' ? value.apps : [];
  const add = (path: string) => {
    const name = fileName(path);
    if (name.endsWith('.exe') && !apps.includes(name) && apps.length < MAX_APPS) onChange({ type: 'app', apps: [...apps, name] });
    setTyped('');
  };
  return <div className="editor-stack compact">
    <div className="segmented" role="group" aria-label={t('profiles.ruleType')}>
      <button type="button" aria-pressed={value.type === 'time'} disabled={busy} onClick={() => { if (value.type !== 'time') onChange(defaultTrigger); }}><Icon name="clock" size={14} />{t('profiles.ruleTypeTime')}</button>
      <button type="button" aria-pressed={value.type === 'app'} disabled={busy} onClick={() => { if (value.type !== 'app') onChange({ type: 'app', apps: [] }); }}><Icon name="bolt" size={14} />{t('profiles.ruleTypeApp')}</button>
    </div>
    {value.type === 'time'
      ? <div className="rule-times">
        <label>{t('profiles.start')}<input type="time" value={value.start} required disabled={busy} onChange={event => onChange({ ...value, start: event.target.value })} /></label>
        <label>{t('profiles.end')}<input type="time" value={value.end} required disabled={busy} onChange={event => onChange({ ...value, end: event.target.value })} /></label>
      </div>
      : <>
        {apps.length > 0 && <div className="rule-apps">{apps.map(name => <span className="rule-app" key={name}>{name}<button type="button" className="icon-button" aria-label={t('profiles.appRemove', { app: name })} title={t('profiles.appRemove', { app: name })} disabled={busy} onClick={() => onChange({ type: 'app', apps: apps.filter(item => item !== name) })}><Icon name="trash" size={12} /></button></span>)}</div>}
        <div className="rule-add">
          <input value={typed} onChange={event => setTyped(event.target.value)} placeholder={t('profiles.appPlaceholder')} aria-label={t('profiles.appName')} maxLength={260} disabled={busy || apps.length >= MAX_APPS}
            onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); add(typed); } }} />
          <button type="button" className="button secondary" disabled={busy || !fileName(typed).endsWith('.exe') || apps.length >= MAX_APPS} onClick={() => add(typed)}>{t('profiles.appAdd')}</button>
          {onPickFile && <button type="button" className="button secondary" disabled={busy || apps.length >= MAX_APPS} onClick={() => { void onPickFile().then(path => { if (path) add(path); }); }}>{t('profiles.appPick')}</button>}
        </div>
        <small className="rule-hint">{t('profiles.appHint')}</small>
      </>}
  </div>;
}

export function ProfilesMenu({ profiles, state, busy, dirty, onPickFile, onActivate, onCreate, onSaveRule, onDelete, onClearData, onClose }: {
  profiles: Profile[]; state: ProfileState | null; busy: boolean; dirty: boolean; onPickFile?: () => Promise<string | null>;
  onActivate: (profile: Profile) => void; onCreate: (name: string, trigger?: Trigger) => void; onSaveRule: (profile: Profile, trigger: Trigger) => void;
  onDelete: (profile: Profile) => void; onClearData: () => void; onClose: () => void;
}) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  const [kind, setKind] = useState<Profile['kind']>('main');
  const [trigger, setTrigger] = useState<Trigger>(defaultTrigger);
  // A profile id, 'data', or null: deleting always asks once more.
  const [confirming, setConfirming] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ id: string; trigger: Trigger } | null>(null);
  const active = state?.activeProfileId ?? null;
  const rule = (value: Trigger) => value.type === 'time' ? t('profiles.ruleTime', { start: value.start, end: value.end }) : t('profiles.ruleApps', { apps: value.apps.join(', ') });
  const badge = (profile: Profile) => profile.id === active ? t(state?.activeBy === 'manual' ? 'profiles.activeManual' : 'profiles.active')
    : profile.id === state?.mainProfileId ? t('profiles.chosen') : null;
  // An automatic profile needs a main profile to return to.
  const locked = (profile: Profile) => profile.id === state?.mainProfileId && profiles.length > 1;

  const row = (profile: Profile) => {
    if (confirming === profile.id) return <div className="profile-confirm" key={profile.id} role="group" aria-label={t('profiles.deleteConfirm', { name: profile.name })}><span>{t('profiles.deleteConfirm', { name: profile.name })}</span><button className="text-button danger" disabled={busy} onClick={() => { setConfirming(null); onDelete(profile); }}>{t('action.delete')}</button><button className="text-button" onClick={() => setConfirming(null)}>{t('action.cancel')}</button></div>;
    if (editing?.id === profile.id) return <div className="profile-edit" key={profile.id}>
      <strong>{profile.name}</strong>
      <RuleFields value={editing.trigger} onChange={next => setEditing({ id: profile.id, trigger: next })} busy={busy} onPickFile={onPickFile} />
      <div className="footer-actions"><button className="button secondary" disabled={busy || !validTrigger(editing.trigger)} onClick={() => { onSaveRule(profile, editing.trigger); setEditing(null); }}><Icon name="check" size={16} />{t('profiles.saveRule')}</button><button className="text-button" onClick={() => setEditing(null)}>{t('action.cancel')}</button></div>
    </div>;
    const label = badge(profile);
    return <div className="profile-row" key={profile.id}>
      <button className={`profile-card ${profile.id === active ? 'active' : ''}`} disabled={busy || dirty} onClick={() => onActivate(profile)}>
        <span className="feature-icon"><Icon name={profile.kind === 'main' ? 'profiles' : profile.trigger?.type === 'app' ? 'bolt' : 'clock'} size={18} /></span>
        <span><strong>{profile.name}</strong>{profile.trigger && <small>{rule(profile.trigger)}</small>}</span>
        {label ? <span className="profile-badge">{label}</span> : <Icon name="arrow" size={18} />}
      </button>
      {profile.trigger && <button className="icon-button" aria-label={t('profiles.editRule', { name: profile.name })} title={t('profiles.editRule', { name: profile.name })} disabled={busy} onClick={() => setEditing({ id: profile.id, trigger: profile.trigger! })}><Icon name="edit" size={16} /></button>}
      <button className="icon-button" aria-label={t('profiles.delete', { name: profile.name })} title={locked(profile) ? t('profiles.mainLocked') : t('profiles.delete', { name: profile.name })} disabled={busy || locked(profile)} onClick={() => setConfirming(profile.id)}><Icon name="trash" size={16} /></button>
    </div>;
  };
  const section = (title: string, hint: string, items: Profile[]) => items.length > 0 && <section className="profile-section">
    <div className="section-head"><span className="eyebrow">{title}</span><small>{hint}</small></div>
    <div className="profile-list">{items.map(row)}</div>
  </section>;
  const ready = !!name.trim() && (kind === 'main' || validTrigger(trigger));

  return <div className="profiles-menu" role="dialog" aria-label={t('profiles.menu')}>
    <div className="profiles-menu-head"><strong>{t('profiles.title')}</strong><button className="text-button" onClick={onClose}>{t('action.close')}</button></div>
    {profiles.length === 0 && <p className="empty-hint">{t('profiles.empty')}</p>}
    {section(t('profiles.main'), t('profiles.mainHint'), profiles.filter(profile => profile.kind === 'main'))}
    {section(t('profiles.automatic'), t('profiles.automaticHint'), profiles.filter(profile => profile.kind === 'automatic'))}
    <form className="editor-stack compact" onSubmit={event => { event.preventDefault(); if (ready) { onCreate(name.trim(), kind === 'automatic' ? trigger : undefined); setName(''); setTrigger(defaultTrigger); } }}>
      <span className="field-label">{t('profiles.new')}</span>
      <div className="segmented" role="group" aria-label={t('profiles.kind')}>
        <button type="button" aria-pressed={kind === 'main'} disabled={busy} onClick={() => setKind('main')}>{t('profiles.kindMain')}</button>
        <button type="button" aria-pressed={kind === 'automatic'} disabled={busy} onClick={() => setKind('automatic')}>{t('profiles.kindAutomatic')}</button>
      </div>
      <input value={name} onChange={event => setName(event.target.value)} maxLength={64} placeholder={t(kind === 'main' ? 'profiles.placeholder' : 'profiles.placeholderAutomatic')} aria-label={t('profiles.nameLabel')} required disabled={busy} />
      {kind === 'automatic' && <RuleFields value={trigger} onChange={setTrigger} busy={busy} onPickFile={onPickFile} />}
      <small className="rule-hint">{t(kind === 'main' ? 'profiles.newMainHint' : 'profiles.newAutomaticHint')}</small>
      <button className="button secondary" disabled={busy || dirty || !ready}><Icon name="plus" size={16} />{t('profiles.save')}</button>
    </form>
    {dirty && <div className="notice">{t('profiles.dirty')}</div>}
    <div className="data-reset">{confirming === 'data'
      ? <><p>{t('data.confirm')}</p><div className="footer-actions"><button className="button danger" disabled={busy} onClick={() => { setConfirming(null); onClearData(); }}>{t('data.clearAll')}</button><button className="text-button" onClick={() => setConfirming(null)}>{t('action.cancel')}</button></div></>
      : <button className="text-button danger" disabled={busy} onClick={() => setConfirming('data')}><Icon name="trash" size={14} />{t('data.clear')}</button>}</div>
  </div>;
}
