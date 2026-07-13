/**
 * basePath helper. SHA's Caddy serves this app under /agents/<slug>/*
 * with strip_prefix=true, meaning internally the app sees paths from /.
 * But <Link>, fetch(), and router.push() targets need the prefix back
 * when running behind Caddy — otherwise they 404 from the browser's
 * perspective.
 *
 * Usage:
 *
 *   import Link from "next/link";
 *   import { BP } from "@/lib/bp";
 *
 *   <Link href={BP("/some/page")}>...</Link>
 *   fetch(BP("/api/data")).then(...);
 *
 * Reads NEXT_PUBLIC_BASE_PATH from env. Empty (local dev) is a no-op.
 *
 * Why not Next's native `basePath`? Two approaches work; Path A
 * (strip_prefix + manual BP() prefixing) caused fewer redirect loops on
 * the writers-dashboard deploy back in April 2026, so SHA standardized
 * on it. See SHA reference docs in memory for the full story.
 */

const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/+$/, "");

export function BP(path: string): string {
  if (!BASE_PATH) return path;
  if (!path.startsWith("/")) return `${BASE_PATH}/${path}`;
  return `${BASE_PATH}${path}`;
}
