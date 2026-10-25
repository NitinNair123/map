import test from 'node:test';
import assert from 'node:assert/strict';
import { parseOpeningHours, isOpenAt, placeOpen, sample, dist, scoreRoutes } from './score.js';

const at = (iso) => new Date(iso); // local time, no Z
// 2026-10-26 is a Monday.

test('opening hours', () => {
  const r = parseOpeningHours('Mo-Fr 09:00-21:00; Sa 10:00-14:00; Su off');
  assert.equal(isOpenAt(r, at('2026-10-26T10:00')), true);
  assert.equal(isOpenAt(r, at('2026-10-26T22:00')), false);
  assert.equal(isOpenAt(r, at('2026-10-31T13:00')), true); // Sat
  assert.equal(isOpenAt(r, at('2026-11-01T12:00')), false); // Sun off
  const night = parseOpeningHours('18:00-02:00');
  assert.equal(isOpenAt(night, at('2026-10-27T01:30')), true); // spill past midnight
  assert.equal(isOpenAt(parseOpeningHours('24/7'), at('2026-10-26T03:00')), true);
  assert.equal(parseOpeningHours('sunrise-sunset'), null);
  assert.equal(placeOpen({ shop: 'bakery' }, at('2026-10-26T23:30')), false); // default shop hours
  assert.equal(placeOpen({ amenity: 'pharmacy', opening_hours: '24/7' }, at('2026-10-26T23:30')), true);
});

test('sampling spacing', () => {
  const pts = sample([[28.6, 77.2], [28.601, 77.2]], 25); // ~110 m
  assert.equal(pts.length, 5);
  assert.ok(Math.abs(dist(pts[0], pts[1]) - 25) < 0.5);
});

test('lit busy route beats dark isolated one at night', () => {
  // Two parallel ~550 m routes; A has lamps and an open pharmacy, B crosses a construction site.
  const A = { line: [[28.600, 77.200], [28.605, 77.200]], distance: 555, duration: 400 };
  const B = { line: [[28.600, 77.201], [28.605, 77.201]], distance: 560, duration: 405 };
  const els = [];
  for (let i = 0; i <= 10; i++) els.push({ type: 'node', lat: 28.600 + i * 0.0005, lon: 77.2001, tags: { highway: 'street_lamp' } });
  els.push({ type: 'node', lat: 28.6025, lon: 77.2002, tags: { amenity: 'pharmacy', opening_hours: '24/7', name: 'Apollo' } });
  els.push({ type: 'way', tags: { landuse: 'construction' }, geometry: [[28.601, 77.2008], [28.601, 77.2012], [28.604, 77.2012], [28.604, 77.2008], [28.601, 77.2008]].map(([lat, lon]) => ({ lat, lon })) });
  els.push({ type: 'way', tags: { highway: 'residential', lit: 'no' }, geometry: [{ lat: 28.600, lon: 77.201 }, { lat: 28.605, lon: 77.201 }] });

  const [best, worst] = scoreRoutes([A, B], els, at('2026-10-26T22:00'));
  assert.equal(best.distance, 555);
  assert.equal(best.label, 'Safest');
  assert.ok(best.score > worst.score + 20, `${best.score} vs ${worst.score}`);
  assert.ok(best.reasons.some((r) => r.includes('street lamps')));
  assert.ok(worst.warnings.some((w) => w.includes('isolated')));
  assert.ok(worst.warnings.some((w) => w.includes('unlit')));
});

test('informal settlement penalty follows the toggle', () => {
  const R = { line: [[28.600, 77.200], [28.602, 77.200]], distance: 222, duration: 160 };
  const els = [{ type: 'way', tags: { landuse: 'residential', residential: 'informal' }, geometry: [[28.599, 77.199], [28.599, 77.201], [28.603, 77.201], [28.603, 77.199], [28.599, 77.199]].map(([lat, lon]) => ({ lat, lon })) },
    { type: 'node', lat: 28.601, lon: 77.2005, tags: { amenity: 'police' } }];
  const on = scoreRoutes([R], els, at('2026-10-26T14:00'), { avoidInformal: true })[0];
  const off = scoreRoutes([R], els, at('2026-10-26T14:00'), { avoidInformal: false })[0];
  assert.ok(on.score < off.score);
  assert.ok(on.warnings.some((w) => w.includes('informal')));
});
