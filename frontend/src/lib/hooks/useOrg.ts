"use client";

import { useCallback, useEffect, useState } from "react";
import { createClient } from "@/lib/supabase/client";

export interface OrgContext {
  organization_id: string;
  name: string;
  slug: string;
  base_currency_code: string;
  country_code: string | null;
  timezone: string;
  fiscal_year_end_month: number;
  business_type: string;
  legal_name: string | null;
  tax_number: string | null;
  registration_number: string | null;
  logo_url: string | null;
}

export interface UseOrgResult {
  org: OrgContext | null;
  roleCode: string;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

/* ---- Session-wide org cache (performance core) ---------------------
   Every dashboard page calls useOrg(); the layout's TopBar needs the
   org too. Without a cache, EACH navigation pays: one network
   round-trip to Supabase Auth (getUser) + one membership query — per
   consumer. With the cache: the membership query runs ONCE per
   session (deduped while in-flight), `getSession()` reads the user
   from the local JWT instantly (no network), and pages render
   synchronously from the snapshot. Visuals unchanged — just no lag. */
interface OrgSnapshot {
  org: OrgContext;
  roleCode: string;
}

let orgCache: OrgSnapshot | null = null;
let orgInflight: Promise<OrgSnapshot | null> | null = null;

/* A stale-but-not-yet-expired access token (e.g. signed by a rotated JWT
   signing key) is rejected as 401 by PostgREST, and supabase-js will not
   refresh a token it still considers valid — so the rejected token kept
   being sent and every dashboard load logged
   "organization_members ... 401". The signal differs across supabase-js
   releases (HTTP status, PGRST301, or an auth-worded message), so accept
   any of them before forcing exactly one refresh + retry. */
function isUnauthorized(err: unknown): boolean {
  const e = err as { status?: number; code?: string; message?: string } | null;
  if (!e) return false;
  if (e.status === 401) return true;
  if (e.code === "PGRST301" || e.code === "401") return true;
  return /jwt|unauthor|invalid api key/i.test(e.message ?? "");
}

export function getCachedOrgContext(): OrgSnapshot | null {
  return orgCache;
}

export function clearOrgCache(): void {
  orgCache = null;
  orgInflight = null;
}

export async function loadOrgContext(force = false): Promise<OrgSnapshot | null> {
  if (!force && orgCache) return orgCache;
  if (orgInflight) return orgInflight;

  orgInflight = (async () => {
    try {
      const supabase = createClient();
      // getSession() reads the LOCAL session (instant, zero network) —
      // the membership row itself is RLS-scoped, which is the authority
      // that matters here; no server-side JWT re-verification needed for
      // display context.
      const { data: { session } } = await supabase.auth.getSession();
      const user = session?.user;
      if (!user) return null;

      const runQuery = () =>
        supabase
          .from("organization_members")
          .select(
            `organization_id,
           status,
           organization:organizations(
             id, name, slug, business_type, base_currency_code, country_code,
             timezone, fiscal_year_end_month, legal_name, tax_number,
             registration_number, logo_url
           ),
           role:organization_roles(code)`
          )
          .eq("user_id", user.id)
          .eq("status", "ACTIVE")
          .order("created_at");

      let result = await runQuery();
      if (result.error && isUnauthorized(result.error)) {
        const { data: refreshed } = await supabase.auth.refreshSession();
        if (refreshed.session) result = await runQuery();
      }
      const data = result.data;
      const dbError = result.error;

      if (dbError) throw dbError;

      const membership = data?.[0];
      if (!membership) return null;

      const orgRow = (membership.organization as unknown as OrgContext[])?.[0]
        ?? (membership.organization as unknown as OrgContext);
      const role = (membership.role as unknown as { code: string }[])?.[0]?.code
        ?? (membership.role as unknown as { code: string })?.code
        ?? "";

      orgCache = { org: { ...orgRow, organization_id: membership.organization_id }, roleCode: role };
      return orgCache;
    } catch (e) {
      // Surface the failure to the caller but DON'T poison the cache.
      throw e instanceof Error ? e : new Error("Failed to load organization");
    } finally {
      orgInflight = null;
    }
  })();

  return orgInflight;
}

/**
 * Resolves the signed-in user's ACTIVE organization membership (with role).
 * All tenant-scoped queries key off `org.organization_id`.
 *
 * Serves from the session cache when available (instant paint), fetches
 * otherwise, and dedupes concurrent loads across every page + TopBar.
 */
export function useOrg(): UseOrgResult {
  const [snapshot, setSnapshot] = useState<OrgSnapshot | null>(() => orgCache);
  const [loading, setLoading] = useState(!orgCache);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;

    // Cache hit → paint immediately, then revalidate quietly.
    if (orgCache) {
      setSnapshot(orgCache);
      setLoading(false);
      setError(null);
    }

    (async () => {
      try {
        const snap = await loadOrgContext();
        if (cancelled) return;
        if (snap) {
          setSnapshot(snap);
          setError(null);
        } else if (!orgCache) {
          setSnapshot(null);
        }
      } catch (e) {
        if (!cancelled && !orgCache) {
          setError(e instanceof Error ? e.message : "Failed to load organization");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [tick]);

  return { org: snapshot?.org ?? null, roleCode: snapshot?.roleCode ?? "", loading, error, refresh };
}
