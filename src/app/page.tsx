import { Activity, TrendingUp, AlertCircle } from "lucide-react";
import { StatCard } from "@/components/StatCard";
import { fetchDashboardData } from "@/lib/data";

/**
 * Dashboard home — a server component, so the data fetch happens on the
 * server with no client-side waterfall. The query lives in
 * src/lib/data.ts; this file just renders.
 *
 * `force-dynamic` opts out of Next's static caching — most dashboards
 * want fresh data on every visit. Switch to `revalidate = 60` (or
 * higher) if your data is stable and you want CDN caching.
 */
export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const { rows, summary } = await fetchDashboardData();

  return (
    <main className="container mx-auto px-6 py-8 max-w-6xl">
      <header className="mb-8">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Replace this page with your own. Data comes from
          <code className="mx-1 px-1 py-0.5 rounded bg-muted text-xs font-mono">
            src/lib/data.ts
          </code>
          .
        </p>
      </header>

      {/* Stat cards — example UI. Customize freely. */}
      <section className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <StatCard
          icon={<Activity className="w-4 h-4" />}
          label="Total rows"
          value={summary.total_rows}
        />
        <StatCard
          icon={<TrendingUp className="w-4 h-4" />}
          label="Active"
          value={summary.active}
        />
        <StatCard
          icon={<AlertCircle className="w-4 h-4" />}
          label="Needs attention"
          value={summary.needs_attention}
          variant={summary.needs_attention > 0 ? "warning" : "default"}
        />
      </section>

      {/* Rows table — example UI. */}
      <section className="rounded-lg border border-border bg-card overflow-hidden">
        <div className="px-4 py-3 border-b border-border bg-muted/30">
          <h2 className="text-sm font-medium">Recent rows</h2>
        </div>
        {rows.length === 0 ? (
          <div className="px-4 py-12 text-center text-sm text-muted-foreground">
            No data yet. Wire up the query in
            <code className="mx-1 px-1 py-0.5 rounded bg-muted text-xs font-mono">
              src/lib/data.ts
            </code>
            and refresh.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/20 text-xs uppercase tracking-wider text-muted-foreground">
                  <th className="px-4 py-2 text-left font-medium">ID</th>
                  <th className="px-4 py-2 text-left font-medium">Name</th>
                  <th className="px-4 py-2 text-left font-medium">Status</th>
                  <th className="px-4 py-2 text-left font-medium">Updated</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-border last:border-0 hover:bg-muted/10"
                  >
                    <td className="px-4 py-2 font-mono text-xs">{row.id}</td>
                    <td className="px-4 py-2">{row.name}</td>
                    <td className="px-4 py-2">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                          row.status === "active"
                            ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                            : row.status === "pending"
                              ? "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400"
                              : "bg-muted text-muted-foreground"
                        }`}
                      >
                        {row.status}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-muted-foreground text-xs">
                      {row.updated_at}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <footer className="mt-8 text-xs text-muted-foreground">
        Workflows can also fetch this data as JSON at{" "}
        <code className="px-1 py-0.5 rounded bg-muted font-mono">/api/data</code>
        .
      </footer>
    </main>
  );
}
