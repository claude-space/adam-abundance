/**
 * THIS IS THE ONLY FILE YOU NEED TO EDIT.
 *
 * Defines the query that powers BOTH the dashboard UI (src/app/page.tsx)
 * and the JSON endpoint (src/app/api/data/route.ts). Single source of
 * truth — adjust here, both surfaces update.
 *
 * Server-only — Supabase service-role key (if used) stays on the server.
 * Calling this from a client component will throw.
 */

import "server-only";
import { getSupabaseServer } from "./supabase";

export interface DashboardRow {
  id: string;
  name: string;
  status: "active" | "pending" | "inactive" | string;
  updated_at: string;
}

export interface DashboardSummary {
  total_rows: number;
  active: number;
  needs_attention: number;
}

export interface DashboardData {
  rows: DashboardRow[];
  summary: DashboardSummary;
}

/**
 * Fetch the dashboard's data. Replace the placeholder query with yours.
 *
 * The structure here — rows + summary — is what page.tsx + the /api/data
 * route consume. You can change the shape, but if you do, also update:
 *   - src/app/page.tsx (render)
 *   - src/app/api/data/route.ts (response shape — usually just passes through)
 */
export async function fetchDashboardData(): Promise<DashboardData> {
  // ────────────────────────────────────────────────────────────────
  // REPLACE THIS BLOCK WITH YOUR REAL QUERY
  // ────────────────────────────────────────────────────────────────
  //
  // Typical pattern — query Supabase:
  //
  //   const supabase = getSupabaseServer();
  //   const { data, error } = await supabase
  //     .from("your_table")
  //     .select("id, name, status, updated_at")
  //     .order("updated_at", { ascending: false })
  //     .limit(50);
  //
  //   if (error) throw error;
  //   const rows = (data ?? []) as DashboardRow[];
  //
  //   return {
  //     rows,
  //     summary: {
  //       total_rows: rows.length,
  //       active: rows.filter((r) => r.status === "active").length,
  //       needs_attention: rows.filter((r) => r.status === "pending").length,
  //     },
  //   };
  //
  // ────────────────────────────────────────────────────────────────

  // Stub data so the dashboard renders out-of-the-box. Replace.
  const supabase = getSupabaseServer();
  // Touch the client so import works at build time even if we don't
  // actually call it in the stub — keeps the lint happy and confirms
  // env vars are wired up.
  void supabase;

  const rows: DashboardRow[] = [
    { id: "1", name: "Example row", status: "active", updated_at: "just now" },
    { id: "2", name: "Another row", status: "pending", updated_at: "1m ago" },
  ];

  return {
    rows,
    summary: {
      total_rows: rows.length,
      active: rows.filter((r) => r.status === "active").length,
      needs_attention: rows.filter((r) => r.status === "pending").length,
    },
  };
}
