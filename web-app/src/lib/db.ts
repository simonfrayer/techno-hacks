export type RecordRow = {
  id: number;
  created_at: string;
  device_record_id: number | null;
  name: string;
  expiry: string;
  quantity: string;
  place: string;
  transcript: string | null;
  audio_path: string | null;
};
export type ClimateRow = {
  id: number;
  created_at: string;
  temp_c: number | null;
  humidity: number | null;
  status: string | null;
  reason: string | null;
  t_min: number | null;
  t_max: number | null;
  h_max: number | null;
};

const API_BASE_URL = (
  import.meta.env["VITE_MEDTRACKER_API_URL"] || "http://localhost:8080"
).replace(/\/+$/, "");

async function get<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/${path}?limit=5000`);
  } catch {
    throw new Error(
      `Cannot fetch from ${API_BASE_URL}. Check the API URL, server status, and allowed web origins.`,
    );
  }
  if (!response.ok) {
    throw new Error(`Database API returned HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function fetchDashboardData(): Promise<{
  records: RecordRow[];
  climate: ClimateRow[];
}> {
  const [recordData, climateData] = await Promise.all([
    get<{ records: RecordRow[] }>("records"),
    get<{ climate: ClimateRow[] }>("climate"),
  ]);
  return {
    records: recordData.records.sort((a, b) => a.expiry.localeCompare(b.expiry)),
    climate: climateData.climate,
  };
}

export function daysUntil(date: string): number | null {
  const d = new Date(date);
  if (isNaN(d.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((d.getTime() - today.getTime()) / 86400000);
}
