import assert from 'node:assert/strict';
import { test } from 'node:test';
import { LocalizedError, errorText, formatPercent, translate, translateServerMessage } from './i18n.ts';

test('Turkish writes the percent sign first and interpolates values', () => {
  assert.equal(formatPercent('tr', 75), '%75');
  assert.equal(formatPercent('en', 75), '75%');
  assert.equal(translate('tr', 'notice.lowBattery', { names: 'Logitech G733' }), 'Düşük pil: Logitech G733.');
  assert.equal(translate('en', 'notice.profileSaved', { name: 'Focus' }), '“Focus” saved from applied settings.');
});

test('service messages translate exactly, by pattern, by composite parts and by code', () => {
  assert.equal(translateServerMessage('tr', 'Profile not found.'), 'Profil bulunamadı.');
  assert.equal(translateServerMessage('tr', 'dpi is unavailable for this device.'), 'dpi bu cihazda kullanılamıyor.');
  assert.equal(translateServerMessage('tr', 'key:F8: Unsupported key'), 'key:F8: Desteklenmeyen tuş');
  assert.equal(translateServerMessage('tr', 'missing_dependency: hidapi'), 'Eksik bağımlılık: hidapi');
  assert.equal(translateServerMessage('tr', 'Something new', 'device_not_found'), 'Cihaz bulunamadı.');
  assert.equal(translateServerMessage('tr', 'Something new'), 'Something new');
  assert.equal(translateServerMessage('en', 'Profile not found.'), 'Profile not found.');
  assert.equal(translateServerMessage('tr', 'Choose another main profile before deleting this one.'), 'Bunu silmeden önce başka bir ana profil seçin.');
  assert.equal(translateServerMessage('tr', 'Something new', 'invalid_trigger'), 'Profil kuralı geçersiz.');
});

test('errors render in the language active when shown', () => {
  const localized = new LocalizedError('language.saveFailed');
  assert.equal(errorText('tr', localized), 'Dil tercihi kaydedilemedi.');
  assert.equal(errorText('en', localized), 'Language preference could not be saved.');
  const service = Object.assign(new Error('Profile storage is busy; retry shortly.'), { code: 'profile_store_busy' });
  assert.equal(errorText('tr', service), 'Profil deposu meşgul; biraz sonra tekrar deneyin.');
  assert.equal(errorText('tr', new SyntaxError('Unexpected token')), 'Girilen metin geçerli JSON değil.');
});
