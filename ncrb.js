// NCRB awareness layer. Display only: never feed this into score.js.
// NCRB publishes crime by state and by metropolitan city, not by street or FIR location,
// so the finest honest level is "this route is inside city X".
import { dist } from './score.js';

export const SOURCE = 'NCRB Crime in India 2022, IPC crime rate per lakh people (figures as reported by ORF; check against NCRB Table 1C before launch)';
export const METRO_AVG = 544; // average of the 19 metropolitan cities NCRB reports

// shortcut: cities are circles around the centre, upgrade to police-commissionerate polygons if borders matter.
export const CITIES = [
  { name: 'Delhi', at: [28.6139, 77.209], km: 25, rate: 1832.6 },
  { name: 'Mumbai', at: [19.076, 72.8777], km: 18, rate: 367.3 },
  { name: 'Ahmedabad', at: [23.0225, 72.5714], km: 12, rate: 360.1 },
  { name: 'Bengaluru', at: [12.9716, 77.5946], km: 18, rate: 337.3 },
  { name: 'Hyderabad', at: [17.385, 78.4867], km: 18, rate: 266.7 },
  { name: 'Pune', at: [18.5204, 73.8567], km: 12, rate: 219.3 },
  { name: 'Surat', at: [21.1702, 72.8311], km: 12, rate: 215.3 },
  { name: 'Chennai', at: [13.0827, 80.2707], km: 14, rate: 211.2 },
  { name: 'Coimbatore', at: [11.0168, 76.9558], km: 10, rate: 211.2 },
  { name: 'Kolkata', at: [22.5726, 88.3639], km: 10, rate: 78.2 },
];

// Cities a route passes through, checked at its start, middle and end.
export function citiesOnRoute(line) {
  const pts = [line[0], line[Math.floor(line.length / 2)], line[line.length - 1]];
  return CITIES.filter((c) => pts.some((p) => dist(p, c.at) <= c.km * 1000));
}

export function awarenessText(line) {
  const found = citiesOnRoute(line);
  if (!found.length) return 'NCRB has no city-level figure for this area.';
  return found.map((c) => `${c.name} (whole city): ${c.rate} IPC crimes per lakh people in 2022, 19-city average ${METRO_AVG}`).join(' · ')
    + '. City-wide, says nothing about this street.';
}
