import type { NextConfig } from "next";

/**
 * Next.js config for a SHA-hosted dashboard agent.
 *
 * basePath story: SHA's Caddy routes /agents/<slug>/* → this app with
 * strip_prefix=true, so internally the app sees requests at /. That means
 * we do NOT set `basePath` here — internal links + asset URLs work as
 * normal during dev. The catch is that <Link>, fetch(), and router.push()
 * calls that target your own routes need to be prefixed when running
 * behind Caddy. The src/lib/bp.ts helper handles that — it reads
 * NEXT_PUBLIC_BASE_PATH from env and prepends it where needed.
 *
 * If you ever want to use Next's native `basePath` instead (Path B in
 * SHA's docs), set `basePath: process.env.NEXT_PUBLIC_BASE_PATH ?? ""`
 * here AND configure Caddy with `strip_prefix=false`. We default to
 * Path A because it caused fewer redirect loops in practice.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // No basePath here — see comment above.
  // Allow images from common Supabase domains if you store assets there.
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "**.supabase.co" },
      { protocol: "https", hostname: "**.googleusercontent.com" },
    ],
  },
};

export default nextConfig;
