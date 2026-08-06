// The hand-written protobuf reader, held to the generated library.
//
// Real bytes from the live SEQ feeds — 1.8 MB of trip updates and every
// vehicle on the road — decoded both ways and compared field by field.
// Nothing synthetic: an agency's actual output is the only input that
// proves a wire-format reader.

import { readFileSync } from "node:fs";
import { decodeFeed } from "../src/gtfsrt.ts";

interface RefStu { stop_sequence: number | null; stop_id: string | null;
                   time: number | null; delay: number | null }
interface RefTu { trip_id: string; start_date: string | null;
                  schedule_relationship: number | null; stop_time_updates: RefStu[] }
interface RefVp { trip_id: string | null; lat: number | null; lon: number | null }

const dir = process.argv[2]!;
const ref = JSON.parse(readFileSync(`${dir}/ref-rt.json`, "utf8")) as
  Record<string, { bytes: number; trip_updates: RefTu[]; vehicles: RefVp[] }>;

let fails = 0;
const fail = (m: string) => { if (fails++ < 8) console.log(`  FAIL ${m}`); };

for (const [name, want] of Object.entries(ref)) {
  const raw = new Uint8Array(readFileSync(`${dir}/${name}.pb`));
  const t0 = Date.now();
  const got = decodeFeed(raw);
  const ms = Date.now() - t0;
  console.log(`  ${name}: ${(raw.length / 1e6).toFixed(2)} MB decoded in ${ms} ms `
            + `-> ${got.tripUpdates.length} trip updates, ${got.vehicles.length} vehicles`);

  if (got.tripUpdates.length !== want.trip_updates.length) {
    fail(`${name}: ${got.tripUpdates.length} trip updates vs ${want.trip_updates.length}`);
  }
  if (got.vehicles.length !== want.vehicles.length) {
    fail(`${name}: ${got.vehicles.length} vehicles vs ${want.vehicles.length}`);
  }
  const n = Math.min(got.tripUpdates.length, want.trip_updates.length);
  for (let i = 0; i < n; i++) {
    const g = got.tripUpdates[i]!, w = want.trip_updates[i]!;
    if (g.tripId !== w.trip_id) { fail(`${name} tu#${i}: trip ${g.tripId} vs ${w.trip_id}`); continue; }
    if ((g.startDate ?? null) !== w.start_date) fail(`${name} tu#${i}: start ${g.startDate} vs ${w.start_date}`);
    if ((g.scheduleRelationship ?? null) !== w.schedule_relationship) {
      fail(`${name} tu#${i}: rel ${g.scheduleRelationship} vs ${w.schedule_relationship}`);
    }
    if (g.stopTimeUpdates.length !== w.stop_time_updates.length) {
      fail(`${name} tu#${i}: ${g.stopTimeUpdates.length} stop updates vs ${w.stop_time_updates.length}`);
      continue;
    }
    for (let j = 0; j < w.stop_time_updates.length; j++) {
      const a = g.stopTimeUpdates[j]!, b = w.stop_time_updates[j]!;
      if (a.stopSequence !== b.stop_sequence || a.stopId !== b.stop_id
          || a.time !== b.time || a.delay !== b.delay) {
        fail(`${name} tu#${i} stu#${j}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);
      }
    }
  }
  const m = Math.min(got.vehicles.length, want.vehicles.length);
  for (let i = 0; i < m; i++) {
    const g = got.vehicles[i]!, w = want.vehicles[i]!;
    const r5 = (v: number | null) => v === null ? null : Math.round(v * 1e5) / 1e5;
    if (g.tripId !== w.trip_id || r5(g.lat) !== w.lat || r5(g.lon) !== w.lon) {
      fail(`${name} vp#${i}: ${JSON.stringify({ t: g.tripId, lat: r5(g.lat), lon: r5(g.lon) })}`
         + ` vs ${JSON.stringify(w)}`);
    }
  }
}

const stus = Object.values(ref).reduce(
  (n, f) => n + f.trip_updates.reduce((k, t) => k + t.stop_time_updates.length, 0), 0);
const vps = Object.values(ref).reduce((n, f) => n + f.vehicles.length, 0);
console.log(`\n  ${stus.toLocaleString()} stop-time updates and ${vps.toLocaleString()} `
          + "vehicle positions compared");
if (fails) { console.log(`  ${fails} MISMATCH(ES)`); process.exit(1); }
console.log("  the hand-written reader agrees with the protobuf library");
