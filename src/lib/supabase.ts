/**
 * Supabase client factory — server-only. Reads connection params from
 * env, returns a fresh client per call (the underlying fetch is shared).
 *
 * Two flavors:
 *   - getSupabaseServer()        — uses anon key; respects RLS
 *   - getSupabaseServiceRole()   — uses service key; BYPASSES RLS
 *
 * Service-role is for queries that legitimately need to bypass RLS
 * (admin-level reads). NEVER expose service-role keys to the client.
 * Always call these from server components / route handlers, never
 * from `"use client"` files.
 */

import "server-only";
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let _anonClient: SupabaseClient | null = null;
let _serviceClient: SupabaseClient | null = null;

export function getSupabaseServer(): SupabaseClient {
  if (_anonClient) return _anonClient;

  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_ANON_KEY;
  if (!url || !key) {
    throw new Error(
      "SUPABASE_URL or SUPABASE_ANON_KEY missing. Copy .env.example to .env and fill in.",
    );
  }
  _anonClient = createClient(url, key, {
    auth: { persistSession: false },
  });
  return _anonClient;
}

export function getSupabaseServiceRole(): SupabaseClient {
  if (_serviceClient) return _serviceClient;

  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) {
    throw new Error(
      "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing. Set SUPABASE_SERVICE_ROLE_KEY in .env if you need RLS-bypassing queries.",
    );
  }
  _serviceClient = createClient(url, key, {
    auth: { persistSession: false },
  });
  return _serviceClient;
}
