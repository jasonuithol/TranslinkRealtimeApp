// Geocoding and walking, held to the server's answers.
//
// Addresses, named places, reverse geocoding and door-to-door walking
// routes, asked of a pack and compared with what geocoding.py and
// walking.py said about the same queries over the national databases.

import { readFileSync } from "node:fs";
import { openNodeDb } from "../src/node-db.ts";
import { gnafGeocode, placesGeocode, reverseGeocode, type Found } from "../src/geocode.ts";
import { walkRoute } from "../src/walk.ts";

const [, , refPath, packDir] = process.argv;
if (!refPath || !packDir) {
  console.error("usage: geo.ts <reference.json> <pack-dir>");
  process.exit(2);
}
const ref = JSON.parse(readFileSync(refPath, "utf8"));
const addresses = openNodeDb(`${packDir}/addresses.sqlite3`);
const places = openNodeDb(`${packDir}/places.sqlite3`);
const walk = openNodeDb(`${packDir}/walk.sqlite3`);

const NEAR = { lat: -27.83308, lon: 153.3695 };
const VIEWBOX: [number, number, number, number] = [152.0, -28.3, 153.6, -26.0];
// A pack is one region; the national databases the server reads are not.
// Only compare answers the pack could possibly hold.
const inPack = (r: { lat: number; lon: number }) =>
  r.lat >= -28.3 && r.lat <= -26.0 && r.lon >= 152.0 && r.lon <= 153.7;

let fails = 0, checks = 0;
const fail = (m: string) => { fails++; console.log(`  FAIL ${m}`); };
const near = (a: number, b: number, tol: number) => Math.abs(a - b) <= tol;

function compare(what: string, got: Found[] | null, want: Found[] | null): void {
  checks++;
  const wanted = (want ?? []).filter(inPack);
  if (!wanted.length) return;                 // outside this pack: not its job
  const g = got ?? [];
  if (!g.length) { fail(`${what}: server found ${wanted.length}, pack found none`); return; }
  // The first result is the one the rider gets, and the only one whose
  // exact rank the app depends on.
  const w = wanted[0]!;
  if (g[0]!.label !== w.label) {
    fail(`${what}: top hit "${g[0]!.label}" vs "${w.label}"`);
  } else if (!near(g[0]!.lat, w.lat, 1e-5) || !near(g[0]!.lon, w.lon, 1e-5)) {
    fail(`${what}: "${w.label}" at ${g[0]!.lat},${g[0]!.lon} vs ${w.lat},${w.lon}`);
  }
}

console.log("addresses (G-NAF house numbers)");
for (const [key, want] of Object.entries(ref.addresses as Record<string, Found[] | null>)) {
  const [q, mode] = key.split("|");
  compare(key, gnafGeocode(addresses, q!, "QLD", 8, mode === "near" ? NEAR : null), want);
}

console.log("places (named POIs)");
for (const [key, want] of Object.entries(ref.places as Record<string, Found[] | null>)) {
  const [q, mode] = key.split("|");
  compare(key, placesGeocode(places, q!, VIEWBOX, 8, mode === "near" ? NEAR : null), want);
}

console.log("reverse geocoding");
for (const [key, want] of Object.entries(
    ref.rgeocode as Record<string, { label: string | null; exact?: boolean }>)) {
  const [lat, lon] = key.split(",").map(Number);
  if (!inPack({ lat: lat!, lon: lon! })) continue;
  checks++;
  const got = reverseGeocode(addresses, lat!, lon!);
  if (got.label !== want.label) fail(`rgeocode ${key}: "${got.label}" vs "${want.label}"`);
  else if (want.label !== null && got.exact !== want.exact) {
    fail(`rgeocode ${key}: exact ${got.exact} vs ${want.exact}`);
  }
}

console.log("walking routes");
for (const [key, want] of Object.entries(
    ref.walks as Record<string, { dist_m?: number; streets?: Array<{ name: string; m: number }>;
                                  error?: string }>)) {
  const [a, b] = key.split("->");
  const [flat, flon] = a!.split(",").map(Number);
  const [tlat, tlon] = b!.split(",").map(Number);
  if (!inPack({ lat: flat!, lon: flon! })) continue;
  checks++;
  const got = walkRoute(walk, flat!, flon!, tlat!, tlon!, { addresses });
  if (want.error) { if (got) fail(`walk ${key}: pack routed where the server refused`); continue; }
  if (!got) { fail(`walk ${key}: server routed ${want.dist_m} m, pack found nothing`); continue; }
  // The A* is deterministic and the graph identical, so the distance must
  // match exactly — a different answer means a different path.
  if (got.distM !== want.dist_m) fail(`walk ${key}: ${got.distM} m vs ${want.dist_m} m`);
  const gotNames = got.streets.map((s) => s.name).join(" > ");
  const wantNames = (want.streets ?? []).map((s) => s.name).join(" > ");
  if (gotNames !== wantNames) fail(`walk ${key}: streets "${gotNames}" vs "${wantNames}"`);
}

console.log(`\n  ${checks - fails}/${checks} checks match the server`);
if (fails) process.exit(1);
