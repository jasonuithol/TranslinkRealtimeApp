// The one thing the query layer needs from a database, and nothing more.
//
// A pack is read by three different SQLite engines depending on where the
// app is running: better-sqlite3 under Node when these queries are being
// tested against the Python server, a native SQLite on the phone, and a
// WASM build in a browser. They agree on very little — but they all run a
// statement with parameters and hand back rows, so that is the whole
// interface. Everything above this file is written once.

export type Params = ReadonlyArray<string | number | null>;
export type Row = Record<string, unknown>;

export interface Db {
  all(sql: string, params?: Params): Row[];
  get(sql: string, params?: Params): Row | undefined;
}

/** `?,?,?` for a list bound by value — SQLite has no array parameter. */
export function marks(n: number): string {
  return new Array(n).fill("?").join(",");
}

/** Read a column that SQLite may hand back as a number, a string, or null. */
export function num(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function str(v: unknown): string | null {
  return v === null || v === undefined ? null : String(v);
}
