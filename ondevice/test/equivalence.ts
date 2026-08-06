// Does the pack answer the same as the server?
//
// Reads reference.py's JSON — real gtfsdb.py over the national database —
// and asks the ported query layer the identical questions of a pack. Every
// departure must match on trip, minute, route, headsign, platform and stop
// sequence. This is the whole point of the port being a port.

import { readFileSync } from "node:fs";
import { openNodeDb } from "../src/node-db.ts";
import { activeServiceIds, scheduledDepartures, stopByFeedId,
         type Departure } from "../src/timetable.ts";

interface RefDep {
  trip_id: string; scheduled: number; route: string | null;
  route_type: number | null; headsign: string | null;
  platform: string | null; stop_sequence: number;
}
interface Reference {
  region: string; date: string; tz: string;
  service_count: number; stops: Record<string, RefDep[]>;
}

const refPath = process.argv[2];
const packPath = process.argv[3];
if (!refPath || !packPath) {
  console.error("usage: equivalence.ts <reference.json> <timetable.sqlite3>");
  process.exit(2);
}

const ref: Reference = JSON.parse(readFileSync(refPath, "utf8"));
const db = openNodeDb(packPath);

let failures = 0;
const fail = (msg: string) => { failures++; console.log(`  FAIL ${msg}`); };

// 1. The service calendar. Everything downstream is conditioned on this,
//    so a disagreement here would make the rest meaningless rather than wrong.
const services = activeServiceIds(db, ref.date, ref.tz);
if (services.size !== ref.service_count) {
  fail(`services active on ${ref.date}: pack ${services.size}, server ${ref.service_count}`);
} else {
  console.log(`  ok   ${services.size} services active on ${ref.date}`);
}

// 2. Departures, stop by stop.
const key = (d: { trip_id?: string; tripId?: string; scheduled: number }) =>
  `${d.trip_id ?? d.tripId}@${d.scheduled}`;

let stopsChecked = 0, depsChecked = 0, stopsMissing = 0;
for (const [stopId, expected] of Object.entries(ref.stops)) {
  const stop = stopByFeedId(db, stopId);
  if (!stop) {
    // A stop nothing calls at is dropped from the pack on purpose; it is
    // only a failure if the server had departures for it.
    if (expected.length) fail(`${stopId}: absent from the pack, server has ${expected.length} departures`);
    else stopsMissing++;
    continue;
  }
  stopsChecked++;
  const got = scheduledDepartures(db, [stop.stop], ref.date, ref.tz);
  depsChecked += expected.length;

  const gotBy = new Map<string, Departure>();
  for (const d of got) gotBy.set(key(d), d);
  const expBy = new Map<string, RefDep>();
  for (const d of expected) expBy.set(key(d), d);

  for (const [k, e] of expBy) {
    const g = gotBy.get(k);
    if (!g) { fail(`${stopId}: server has ${k} (${e.route}), pack does not`); continue; }
    if (g.route !== e.route) fail(`${stopId} ${k}: route ${g.route} vs ${e.route}`);
    if (g.routeType !== e.route_type) fail(`${stopId} ${k}: route_type ${g.routeType} vs ${e.route_type}`);
    if ((g.headsign ?? null) !== (e.headsign ?? null)) fail(`${stopId} ${k}: headsign ${g.headsign} vs ${e.headsign}`);
    if ((g.platform ?? null) !== (e.platform ?? null)) fail(`${stopId} ${k}: platform ${g.platform} vs ${e.platform}`);
    if (g.stopSequence !== e.stop_sequence) fail(`${stopId} ${k}: seq ${g.stopSequence} vs ${e.stop_sequence}`);
  }
  for (const k of gotBy.keys()) {
    if (!expBy.has(k)) fail(`${stopId}: pack has ${k}, server does not`);
  }
}

console.log(`  ok   ${depsChecked} departures across ${stopsChecked} stops`
          + (stopsMissing ? ` (${stopsMissing} empty stops pruned from the pack)` : ""));
if (failures) {
  console.log(`\n  ${failures} MISMATCH(ES)`);
  process.exit(1);
}
console.log("\n  the pack answers exactly as the server does");
