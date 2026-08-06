// The clock, held to the server's answer.
//
// Every scheduled time on the board is a service date plus an offset, and
// twice a year that sum is not what arithmetic says it is. These are the
// server's own epochs for the awkward cases; the port must reproduce them
// exactly, or a board silently runs an hour out for a day.

import { readFileSync } from "node:fs";
import { serviceEpoch } from "../src/time.ts";

interface Case { tz: string; ymd: string; secs: number; hms: string; epoch: number }
const cases: Case[] = JSON.parse(readFileSync(process.argv[2]!, "utf8"));

let bad = 0;
for (const c of cases) {
  const got = serviceEpoch(c.ymd, c.secs, c.tz);
  if (got !== c.epoch) {
    bad++;
    const d = got - c.epoch;
    console.log(`  FAIL ${c.tz} ${c.ymd} ${c.hms}: ${got} vs ${c.epoch} (${d > 0 ? "+" : ""}${d}s)`);
  }
}
console.log(`  ${cases.length - bad}/${cases.length} clock times match the server`);
if (bad) process.exit(1);
