import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

/* ONE browser client per tab — memoised.
   createBrowserClient() spins up its own GoTrue auth instance. Building a
   new one on every call (fetchApi does exactly that on every request) left
   several instances racing over the same stored session: whichever one
   refreshed first invalidated the others' token, which surfaced as
   spurious 401s against both PostgREST and our own API. A single shared
   instance owns auto-refresh and hands every caller the same live token. */
let browserClient: SupabaseClient | null = null;

export function createClient(): SupabaseClient {
  if (browserClient) return browserClient;
  browserClient = createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
  );
  return browserClient;
}
