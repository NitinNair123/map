import test from 'node:test';
import assert from 'node:assert/strict';
import { parseJson, fillShopHours, eventHazards } from './gemini.js';

const reply = (text) => async () => ({ ok: true, json: async () => ({ candidates: [{ content: { parts: [{ text }] } }] }) });

test('parseJson pulls JSON out of chatty replies', () => {
  assert.deepEqual(parseJson('Sure!\n```json\n{"0":"24/7"}\n```'), { 0: '24/7' });
  assert.deepEqual(parseJson('[]'), []);
  assert.equal(parseJson('no idea'), null);
});

test('no key: no requests, nothing changes', async () => {
  globalThis.SAFEROUTE_CONFIG = undefined;
  globalThis.fetch = () => assert.fail('should not call Gemini');
  assert.equal(await fillShopHours([], [], 'x'), 0);
  assert.deepEqual(await eventHazards([28.6, 77.2], [28.61, 77.2], new Date(), 'x'), []);
});

test('fills hours only for named, untimed places near the route, in one request', async () => {
  globalThis.SAFEROUTE_CONFIG = { geminiKey: 'k' };
  let calls = 0;
  globalThis.fetch = async (...a) => { calls++; return reply('{"0":"Mo-Su 08:00-23:00"}')(...a); };
  const route = { line: [[28.600, 77.200], [28.602, 77.200]] };
  const els = [
    { type: 'node', id: 1, lat: 28.601, lon: 77.2001, tags: { shop: 'convenience', name: 'Kirana' } },
    { type: 'node', id: 2, lat: 28.601, lon: 77.2001, tags: { shop: 'bakery', name: 'Has hours', opening_hours: '24/7' } },
    { type: 'node', id: 3, lat: 28.601, lon: 77.2001, tags: { shop: 'bakery' } }, // unnamed
    { type: 'node', id: 4, lat: 28.650, lon: 77.250, tags: { shop: 'bakery', name: 'Far' } },
  ];
  assert.equal(await fillShopHours(els, [route], 'Delhi'), 1);
  assert.equal(calls, 1);
  assert.equal(els[0].tags.opening_hours, 'Mo-Su 08:00-23:00');
  assert.equal(els[3].tags.opening_hours, undefined);
});

test('event hazards are validated and clamped', async () => {
  globalThis.SAFEROUTE_CONFIG = { geminiKey: 'k' };
  globalThis.fetch = reply('[{"lat":28.61,"lon":77.21,"radius_m":99999,"what":"VIP convoy"},{"lat":"x","what":"bad"}]');
  const hz = await eventHazards([28.6, 77.2], [28.62, 77.22], new Date('2026-10-30T18:00'), 'Delhi');
  assert.deepEqual(hz, [{ pt: [28.61, 77.21], r: 1500, what: 'VIP convoy', penalty: 30 }]);
});
