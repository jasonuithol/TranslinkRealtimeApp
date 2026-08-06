// A Db backed by better-sqlite3, for running the query layer under Node.
//
// This is how the port gets tested without a phone: the same modules the
// app will run, over the same pack the app will download, with their
// answers compared against the Python server's. Not shipped to any device.

import Database from "better-sqlite3";
import type { Db, Params, Row } from "./db.ts";

export function openNodeDb(path: string): Db & { close(): void } {
  const con = new Database(path, { readonly: true, fileMustExist: true });
  return {
    all(sql: string, params: Params = []): Row[] {
      return con.prepare(sql).all(...(params as unknown[])) as Row[];
    },
    get(sql: string, params: Params = []): Row | undefined {
      return con.prepare(sql).get(...(params as unknown[])) as Row | undefined;
    },
    close() { con.close(); },
  };
}
