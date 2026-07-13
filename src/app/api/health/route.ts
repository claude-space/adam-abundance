import { NextResponse } from "next/server";

/**
 * GET /api/health — liveness probe. Returns immediately without
 * touching any external services. Used by SHA's monitoring to detect
 * whether the agent is alive.
 */
export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json({ ok: true });
}
