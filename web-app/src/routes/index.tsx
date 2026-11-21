import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { daysUntil, fetchDashboardData, type RecordRow, type ClimateRow } from "@/lib/db";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "MediStore — medication storage tracker" },
      {
        name: "description",
        content:
          "Track medications, expiry dates, and storage climate from the live MedTracker database.",
      },
      { property: "og:title", content: "MediStore — medication storage tracker" },
      {
        property: "og:description",
        content:
          "Track medications, expiry dates, and storage climate from the live MedTracker database.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Index,
});

const fmt = (s: string) => {
  const d = new Date(s);
  return isNaN(d.getTime()) ? s : d.toLocaleString();
};
const num = (n: number | null, u: string) => (n == null ? "—" : `${n.toFixed(1)}${u}`);

function Index() {
  const [records, setRecords] = useState<RecordRow[]>([]);
  const [climate, setClimate] = useState<ClimateRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [tab, setTab] = useState<"records" | "climate">("records");
  const [q, setQ] = useState("");
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchDashboardData();
      setRecords(data.records);
      setClimate(data.climate);
      setLastUpdated(new Date());
      setLoaded(true);
      setErr("");
    } catch (error) {
      setErr(error instanceof Error ? error.message : "Could not load data from the database API.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  if (!loaded) {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
        <h1 className="font-display text-5xl leading-tight text-primary">MediStore</h1>
        <p className="mt-3 text-muted-foreground">Connecting to the local MedTracker database…</p>
        {loading && !err && (
          <p className="mt-4 text-sm text-muted-foreground">Loading records and climate readings</p>
        )}
        {err && <p className="mt-4 text-sm text-destructive">{err}</p>}
        <button
          onClick={() => void refresh()}
          disabled={loading}
          className="mt-6 rounded-2xl bg-primary px-6 py-4 font-medium text-primary-foreground shadow-lg disabled:opacity-60"
        >
          {loading ? "Connecting…" : "Try again"}
        </button>
        <p className="mt-4 text-xs text-muted-foreground">
          Make sure `techno-hacks serve` is running on this computer.
        </p>
      </main>
    );
  }

  const latest = climate[0];
  const list = records.filter((r) =>
    `${r.name} ${r.place}`.toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <main className="mx-auto min-h-screen max-w-md pb-28">
      <header className="sticky top-0 z-10 bg-background/90 px-5 pb-3 pt-6 backdrop-blur">
        <div className="flex items-center justify-between">
          <h1 className="font-display text-3xl text-primary">
            {tab === "records" ? "Medications" : "Storage climate"}
          </h1>
          <button
            onClick={() => void refresh()}
            disabled={loading}
            className="text-sm text-muted-foreground underline disabled:opacity-60"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {err
            ? `Refresh failed: ${err}`
            : lastUpdated
              ? `Updated ${fmt(lastUpdated.toISOString())} · auto-refresh every 15 s`
              : ""}
        </p>
        {latest && (
          <div className="mt-3 flex gap-2 text-sm">
            <span className="rounded-full bg-secondary px-3 py-1">
              🌡 {num(latest.temp_c, "°C")}
            </span>
            <span className="rounded-full bg-secondary px-3 py-1">
              💧 {num(latest.humidity, "%")}
            </span>
            {latest.status && <StatusPill s={latest.status} />}
          </div>
        )}
        {tab === "records" && (
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search medication or storage place…"
            className="mt-3 w-full rounded-xl border border-input bg-card px-4 py-2.5 outline-none focus:ring-2 focus:ring-ring"
          />
        )}
      </header>

      <section className="space-y-3 px-5">
        {tab === "records" &&
          (list.length === 0 ? (
            <Empty t="No medications" />
          ) : (
            list.map((r) => {
              const d = daysUntil(r.expiry);
              const tone =
                d == null
                  ? "bg-muted"
                  : d < 0
                    ? "bg-destructive text-destructive-foreground"
                    : d <= 3
                      ? "bg-warning text-accent-foreground"
                      : "bg-success text-primary-foreground";
              return (
                <article
                  key={r.id}
                  onClick={() => setOpen(open === r.id ? null : r.id)}
                  className="cursor-pointer rounded-2xl border border-border bg-card p-4"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h2 className="font-semibold">{r.name}</h2>
                      <p className="text-sm text-muted-foreground">
                        {r.quantity} · {r.place}
                      </p>
                    </div>
                    <span
                      className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}
                    >
                      {d == null ? r.expiry : d < 0 ? `${-d}d ago` : d === 0 ? "today" : `${d}d`}
                    </span>
                  </div>
                  {open === r.id && (
                    <dl className="mt-3 space-y-1 border-t border-border pt-3 text-sm">
                      <Row k="Expiry" v={r.expiry} />
                      <Row k="Added" v={fmt(r.created_at)} />
                      {r.device_record_id != null && (
                        <Row k="Device ID" v={String(r.device_record_id)} />
                      )}
                      {r.transcript && (
                        <p className="pt-1 italic text-muted-foreground">“{r.transcript}”</p>
                      )}
                      {r.audio_path && <Row k="Audio" v={r.audio_path} />}
                    </dl>
                  )}
                </article>
              );
            })
          ))}

        {tab === "climate" &&
          (climate.length === 0 ? (
            <Empty t="No readings" />
          ) : (
            climate.map((c) => (
              <article key={c.id} className="rounded-2xl border border-border bg-card p-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">{fmt(c.created_at)}</span>
                  {c.status && <StatusPill s={c.status} />}
                </div>
                <div className="mt-2 flex gap-6">
                  <div>
                    <p className="font-display text-3xl">{num(c.temp_c, "°")}</p>
                    <p className="text-xs text-muted-foreground">
                      allowed {num(c.t_min, "")} – {num(c.t_max, "")} °C
                    </p>
                  </div>
                  <div>
                    <p className="font-display text-3xl">{num(c.humidity, "%")}</p>
                    <p className="text-xs text-muted-foreground">allowed max {num(c.h_max, "%")}</p>
                  </div>
                </div>
                {c.reason && <p className="mt-2 text-sm text-muted-foreground">{c.reason}</p>}
              </article>
            ))
          ))}
      </section>

      <nav className="fixed inset-x-0 bottom-0 mx-auto flex max-w-md gap-2 border-t border-border bg-card p-3">
        {(["records", "climate"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 rounded-xl py-3 font-medium ${tab === t ? "bg-primary text-primary-foreground" : "text-muted-foreground"}`}
          >
            {t === "records" ? `Medications (${records.length})` : `Climate (${climate.length})`}
          </button>
        ))}
      </nav>
    </main>
  );
}

function StatusPill({ s }: { s: string }) {
  const ok = /ok|good|normal/i.test(s);
  return (
    <span
      className={`rounded-full px-3 py-1 text-xs font-medium ${ok ? "bg-success text-primary-foreground" : "bg-warning text-accent-foreground"}`}
    >
      {s}
    </span>
  );
}
const Row = ({ k, v }: { k: string; v: string }) => (
  <div className="flex justify-between gap-4">
    <dt className="text-muted-foreground">{k}</dt>
    <dd className="text-right break-all">{v}</dd>
  </div>
);
const Empty = ({ t }: { t: string }) => (
  <p className="py-12 text-center text-muted-foreground">{t}</p>
);
