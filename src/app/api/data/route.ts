import { NextResponse } from "next/server";
import { fetchDashboardData } from "@/lib/data";

/**
 * GET /api/data — JSON version of the dashboard's data. Used by
 * workflow steps that want to consume the same data the dashboard UI
 * shows, without scraping HTML.
 *
 * Returns the exact shape from fetchDashboardData(); change the shape
 * in src/lib/data.ts and both surfaces update.
 *
 * force-dynamic prevents Next from statically caching the response —
 * dashboards almost always want fresh-on-every-request behavior.
 */
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const data = await fetchDashboardData();
    return NextResponse.json(data);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    // 502 — the failure is in our upstream (DB query / etc.), not the
    // request itself. Workflow runs treat 5xx as step failures, which
    // is correct here.
    return NextResponse.json(
      { error: "Failed to fetch dashboard data", detail: message },
      { status: 502 },
    );
  }
}
