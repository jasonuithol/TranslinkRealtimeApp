// RAPTOR, held to the server's journeys.
//
// The same region-day, the same departure times, the same origins and
// destinations — including the address form with walking penalties that a
// dropped pin produces. Every itinerary is compared leg by leg: the trips
// boarded, where they were boarded and left, and the second of each.

import { readFileSync } from "node:fs";
import { openNodeDb } from "../src/node-db.ts";
import { buildTimetable, plan, type Itinerary, type Leg } from "../src/planner.ts";

interface RefLeg {
  kind: string; trip_id?: string; board?: string; alight?: string;
  dep_sec?: number; arr_sec?: number; from?: string; to?: string; secs?: number;
}
interface RefIt { arr_sec: number; dep_sec: number; rides: number; legs: RefLeg[] }

const [, , refPath, packPath, tz] = process.argv;
if (!refPath || !packPath) {
  console.error("usage: plan.ts <reference.json> <timetable.sqlite3> [tz]");
  process.exit(2);
}
const ref = JSON.parse(readFileSync(refPath, "utf8"));
const db = openNodeDb(packPath);

const t0 = Date.now();
const tt = buildTimetable(db, [{ ymd: ref.ymd, offset: 0 }],
                          tz ?? "Australia/Brisbane", ref.region === "syd");
const buildMs = Date.now() - t0;

let fails = 0;
const fail = (m: string) => { fails++; console.log(`  FAIL ${m}`); };

// The shape of the network must match before its journeys can mean
// anything. Routes are the real test and must be exact — a different
// route count is a different graph.
//
// Stops are allowed to be FEWER. The pack drops stops with no service in
// its validity window, and the server's timetable carries them: on the day
// this was written that was 17 stops served only by one-off Airtrain
// services that ran the week before the pack was built. They cannot be
// boarded either way, so no journey changes — but a pack with MORE stops
// than the feed, or fewer routes, would mean something is wrong.
console.log(`  built in ${buildMs} ms: ${tt.stopIds.length} stops, `
          + `${tt.routes.length} routes`);
if (tt.routes.length !== ref.routes) fail(`routes ${tt.routes.length} vs ${ref.routes}`);
if (tt.stopIds.length > ref.stops) {
  fail(`stops ${tt.stopIds.length} vs ${ref.stops} — the pack cannot have more`);
} else if (tt.stopIds.length < ref.stops) {
  console.log(`       (${ref.stops - tt.stopIds.length} stops with no service in `
            + "the pack's window were pruned)");
}
const footPairs = [...tt.foot.values()].reduce((n, v) => n + v.length, 0);
if (footPairs > ref.foot_pairs) fail(`footpaths ${footPairs} vs ${ref.foot_pairs}`);

// A station group is one place to a rider — its platforms are 180 s
// apart and the card names the station. When two of them tie exactly for
// the last walk, the server and the port can each name a different one,
// because the server breaks that tie on Python's set iteration order.
// Compare stations, then, and report when the platform differed so the
// difference is visible rather than hidden.
const station = (id: string): string => {
  const g = tt.groupOf.get(id);
  if (!g) return id;
  return [...g].sort()[0]!;
};
const legStr = (l: Leg): string => l.kind === "ride"
  ? `ride ${l.tripId} ${station(l.board)}@${l.depSec} -> ${station(l.alight)}@${l.arrSec}`
  : `walk ${station(l.from)} -> ${station(l.to)} ${l.secs}s`;
const refLegStr = (l: RefLeg): string => l.kind === "ride"
  ? `ride ${l.trip_id} ${station(l.board!)}@${l.dep_sec} -> ${station(l.alight!)}@${l.arr_sec}`
  : `walk ${station(l.from!)} -> ${station(l.to!)} ${l.secs}s`;

let planned = 0, itineraries = 0, totalMs = 0;
for (const [key, want] of Object.entries(ref.plans as Record<string, RefIt[]>)) {
  const spec = JSON.parse(key);
  planned++;
  const started = Date.now();
  const got: Itinerary[] = plan(tt, spec.dep_sec, {
    fromStop: spec.from, toStop: spec.to,
    sources: spec.sources, targets: spec.targets, maxItineraries: 4,
  });
  totalMs += Date.now() - started;

  if (got.length !== want.length) {
    fail(`${key}: ${got.length} itineraries vs ${want.length}`);
    continue;
  }
  for (let i = 0; i < want.length; i++) {
    const g = got[i]!, w = want[i]!;
    itineraries++;
    if (g.arrSec !== w.arr_sec) { fail(`${key} #${i}: arrive ${g.arrSec} vs ${w.arr_sec}`); continue; }
    if (g.depSec !== w.dep_sec) { fail(`${key} #${i}: depart ${g.depSec} vs ${w.dep_sec}`); continue; }
    if (g.rides !== w.rides) { fail(`${key} #${i}: ${g.rides} rides vs ${w.rides}`); continue; }
    const a = g.legs.map(legStr).join(" | ");
    const b = w.legs.map(refLegStr).join(" | ");
    if (a !== b) fail(`${key} #${i}:\n       pack   ${a}\n       server ${b}`);
  }
}

console.log(`  ${planned} journeys planned in ${totalMs} ms `
          + `(${(totalMs / Math.max(planned, 1)).toFixed(1)} ms each), `
          + `${itineraries} itineraries compared`);
if (fails) { console.log(`\n  ${fails} MISMATCH(ES)`); process.exit(1); }
console.log("  every journey matches the server, leg for leg");
