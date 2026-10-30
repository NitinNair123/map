import test from 'node:test';
import assert from 'node:assert/strict';
import { emergencyText } from './sos.js';

test('emergency text includes profile facts and location', () => {
  const t = emergencyText({ name: 'Asha', blood: 'B+', allergies: 'penicillin' }, [28.61, 77.2]);
  assert.match(t, /Name: Asha\. Blood group: B\+\. Allergies: penicillin\./);
  assert.match(t, /maps\.google\.com\/\?q=28\.61,77\.2/);
  assert.doesNotMatch(t, /Conditions/);
});

test('emergency text works with empty profile and no location', () => {
  assert.match(emergencyText({}, null), /ambulance.*Location unavailable/);
});
