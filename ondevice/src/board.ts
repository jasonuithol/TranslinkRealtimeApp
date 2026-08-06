// The arrivals board, answered from a pack.
//
// The server's version (boards.py) also unions sibling feeds, folds in
// realtime and drops anything it cannot put on the map. None of that
// belongs here: a pack holds one region, realtime arrives separately, and
// the map rules are the shell's. What is left is the part that must agree
// with the server exactly — which services call at this stop, when, and
// on which of two possible service days.

import type { Db } from "./db.ts";
import { serviceDates } from "./time.ts";
import {
  type Departure, type Stop, scheduledDepartures, stopByFeedId, stopFamily,
} from "./timetable.ts";

export interface BoardOptions {
  /** Now, in epoch milliseconds. Injected so tests are not clock-dependent. */
  now?: number;
  /** How far ahead to look, in minutes. */
  lookaheadMinutes?: number;
  /** Most departures to return. */
  limit?: number;
}

export interface Board {
  stop: Stop;
  departures: Departure[];
}

/**
 * What leaves this stop next.
 *
 * Two service days are gathered, not one: at 00:20 the trains still
 * running are yesterday's 24:xx services, and a board that only knew about
 * today would show nothing at the exact hour someone most needs it.
 */
export function board(db: Db, stopId: string, tz: string,
                      opts: BoardOptions = {}): Board | null {
  const stop = stopByFeedId(db, stopId);
  if (!stop) return null;

  const now = Math.floor((opts.now ?? Date.now()) / 1000);
  const lookahead = (opts.lookaheadMinutes ?? 120) * 60;
  const family = stopFamily(db, stop).map((s) => s.stop);

  const all: Departure[] = [];
  for (const ymd of serviceDates((opts.now ?? Date.now()), tz)) {
    all.push(...scheduledDepartures(db, family, ymd, tz));
  }

  // The same physical departure can be reached through both service days
  // (a trip that runs every day appears under yesterday's 25:xx and
  // today's 01:xx). One trip calling once at one stop is one departure.
  const seen = new Map<string, Departure>();
  for (const d of all) {
    const key = `${d.tripId}|${d.stopId}|${d.scheduled}`;
    if (!seen.has(key)) seen.set(key, d);
  }

  const departures = [...seen.values()]
    .filter((d) => d.scheduled >= now - 60 && d.scheduled <= now + lookahead)
    .sort((a, b) => a.scheduled - b.scheduled)
    .slice(0, opts.limit ?? 12);

  return { stop, departures };
}
