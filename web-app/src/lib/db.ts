import type { Database } from "sql.js";

export type RecordRow = {
  id: number; created_at: string; device_record_id: number | null; name: string;
  expiry: string; quantity: string; place: string; transcript: string | null; audio_path: string | null;
};
export type ClimateRow = {
  id: number; created_at: string; temp_c: number | null; humidity: number | null; status: string | null;
  reason: string | null; t_min: number | null; t_max: number | null; h_max: number | null;
};

export async function openDb(buf: ArrayBuffer): Promise<Database> {
  const initSqlJs = (await import("sql.js")).default;
  const SQL = await initSqlJs({ locateFile: () => "/sql-wasm.wasm" , useOldExtensionFunctions: false });
  return new SQL.Database(new Uint8Array(buf));
}

function all<T>(db: Database, sql: string): T[] {
  try {
    const res = db.exec(sql)[0];
    if (!res) return [];
    return res.values.map((v) => Object.fromEntries(res.columns.map((c, i) => [c, v[i]])) as T);
  } catch { return []; }
}

export const readRecords = (db: Database) => all<RecordRow>(db, "SELECT * FROM records ORDER BY expiry ASC");
export const readClimate = (db: Database) => all<ClimateRow>(db, "SELECT * FROM climate ORDER BY created_at DESC");

export function daysUntil(date: string): number | null {
  const d = new Date(date);
  if (isNaN(d.getTime())) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return Math.round((d.getTime() - today.getTime()) / 86400000);
}
