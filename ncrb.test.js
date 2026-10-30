import test from 'node:test';
import assert from 'node:assert/strict';
import { citiesOnRoute, awarenessText } from './ncrb.js';

test('ncrb city lookup', () => {
  const delhi = [[28.62, 77.21], [28.63, 77.22]];
  assert.deepEqual(citiesOnRoute(delhi).map((c) => c.name), ['Delhi']);
  assert.match(awarenessText(delhi), /Delhi \(whole city\).*says nothing about this street/);
  assert.equal(citiesOnRoute([[26.9, 75.8], [26.91, 75.81]]).length, 0); // Jaipur: not in the list
  assert.match(awarenessText([[26.9, 75.8]]), /no city-level figure/);
});
