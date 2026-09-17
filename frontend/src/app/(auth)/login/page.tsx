"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import { createClient } from "@/lib/supabase/client";
import { clearOrgCache } from "@/lib/hooks/useOrg";
import { getSiteUrl } from "@/lib/site-url";
import { isGoogleProviderEnabled } from "@/lib/auth/google";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import dynamic from "next/dynamic";

/* "Ledger" — 3D mascot, client-only (WebGL never runs on the server) */
const HeroRobotStage = dynamic(() => import("@/components/hero/HeroRobotStage"), {
  ssr: false,
  loading: () => null,
});

/* Google "G" mark — lucide-react no longer ships brand icons */
function GoogleIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
      <path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.19-2.27H12v4.51h6.47c-.29 1.48-1.14 2.73-2.4 3.58v3h3.86c2.26-2.09 3.56-5.17 3.56-8.82z" />
      <path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.86-3c-1.08.72-2.45 1.16-4.07 1.16-3.13 0-5.78-2.11-6.73-4.96H1.29v3.09C3.26 21.3 7.31 24 12 24z" />
      <path fill="#FBBC05" d="M5.27 14.29c-.25-.72-.38-1.49-.38-2.29s.14-1.57.38-2.29V6.62H1.29C.47 8.24 0 10.06 0 12s.47 3.76 1.29 5.38l3.98-3.09z" />
      <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.31 0 3.26 2.7 1.29 6.62l3.98 3.09C6.22 6.86 8.87 4.75 12 4.75z" />
    </svg>
  );
}

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);
  const [googleHint, setGoogleHint] = useState<string | null>(null);
  const router = useRouter();
  const supabase = createClient();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    const { error } = await supabase.auth.signInWithPassword({ email, password });

    if (error) {
      setError(error.message);
      setLoading(false);
      return;
    }

    // Fresh session → drop any previous user's org snapshot so the
    // dashboard re-resolves THIS user's organization.
    clearOrgCache();
    router.push("/");
    router.refresh();
  };

  /* Google OAuth (Supabase Auth). PKCE flow: Google bounces back to
     /auth/callback, where the browser client exchanges the code.
     Pre-checks that the provider is enabled so users get a friendly
     notice instead of a raw "provider is not enabled" JSON page. */
  const handleGoogleSignIn = async () => {
    setError("");
    setGoogleHint(null);
    setGoogleLoading(true);
    const enabled = await isGoogleProviderEnabled();
    if (!enabled) {
      setGoogleLoading(false);
      setGoogleHint(
        "Google sign-in isn't enabled yet — we're finishing the setup. Please sign in with email & password for now."
      );
      return;
    }
    const { data: oauthData, error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${getSiteUrl()}/auth/callback` },
    });
    /* The auth server drops the `sb_flow_id` param from the final redirect;
       persist the flow id so /auth/callback can pick the right PKCE
       verifier slot for the exchange. */
    if (!error && oauthData?.flowId) {
      try {
        sessionStorage.setItem("pkce_flow_id", oauthData.flowId);
      } catch {
        /* storage unavailable — the callback falls back to cookie scanning */
      }
    }
    if (error) {
      setError(error.message);
      setGoogleLoading(false);
    }
  };

  return (
    <div className="relative">
      {/* Ledger peeks over the card's shoulder — larger, uncropped window.
          The bubble pops up at random intervals, never sticking around. */}
      <div className="absolute -top-24 right-0 sm:-right-16 sm:-top-28 z-20 w-48 h-44 sm:w-64 sm:h-60 pointer-events-none select-none">
        <HeroRobotStage variant="compact" />
      </div>

      <div className="bg-bg-surface/80 backdrop-blur-sm rounded-2xl shadow-lg border border-border-subtle p-8 space-y-6">
      <div className="text-center">
        <div className="flex justify-center mb-5 logo-enter">
          <Image
            src="/ai-accountant-logo.svg"
            alt="AI Accountant"
            width={260}
            height={74}
            className="w-full max-w-[240px] h-auto object-contain"
            priority
          />
        </div>
        <h1 className="text-2xl font-semibold text-brand-navy">Welcome back</h1>
        <p className="text-sm text-text-secondary mt-1">Sign in to your AI Accountant</p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div className="p-3 rounded-lg bg-error-50 border border-error-100 text-sm text-error-600">
            {error}
          </div>
        )}

        <div>
          <label className="block text-sm font-medium text-brand-navy mb-1.5">Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="w-full px-3 py-2.5 rounded-xl border border-border-default bg-bg-surface text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-brand-teal/30 focus:border-brand-teal transition"
            placeholder="you@company.com"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-brand-navy mb-1.5">Password</label>
          <div className="relative">
            <input
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full px-3 py-2.5 pr-10 rounded-xl border border-border-default bg-bg-surface text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-brand-teal/30 focus:border-brand-teal transition"
              placeholder="••••••••"
            />
            <button
              type="button"
              onClick={() => setShowPassword(!showPassword)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-secondary"
            >
              {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
          <div className="flex justify-end mt-1.5">
            <Link
              href="/forgot-password"
              className="text-xs font-medium text-brand-teal hover:text-brand-navy transition-colors"
            >
              Forgot password?
            </Link>
          </div>
        </div>

        <button
          type="submit"
          disabled={loading || googleLoading}
          className="w-full py-2.5 btn-3d btn-shine rounded-xl bg-brand-teal text-white text-sm font-medium hover:bg-brand-navy focus:outline-none focus:ring-2 focus:ring-brand-teal/30 disabled:opacity-60 transition flex items-center justify-center gap-2"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
          {loading ? "Signing in..." : "Sign In"}
        </button>
      </form>

      <div className="flex items-center gap-3" aria-hidden="true">
        <span className="h-px flex-1 bg-border-subtle" />
        <span className="text-[11px] uppercase tracking-wider text-text-muted font-medium">or</span>
        <span className="h-px flex-1 bg-border-subtle" />
      </div>

      <button
        type="button"
        onClick={handleGoogleSignIn}
        disabled={loading || googleLoading}
        className="w-full py-2.5 rounded-xl border border-border-default bg-bg-surface text-sm font-medium text-text-primary hover:bg-bg-primary focus:outline-none focus:ring-2 focus:ring-brand-teal/30 disabled:opacity-60 transition flex items-center justify-center gap-2.5"
      >
        {googleLoading ? (
          <Loader2 className="w-4 h-4 animate-spin" />
        ) : (
          <GoogleIcon className="w-4 h-4" />
        )}
        {googleLoading ? "Redirecting to Google..." : "Continue with Google"}
      </button>
      {googleHint && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2 text-center">
          {googleHint}
        </p>
      )}

      <p className="text-center text-sm text-text-secondary">
        Don&apos;t have an account?{" "}
        <Link href="/signup" className="text-brand-teal font-medium hover:text-brand-navy">
          Create one
        </Link>
      </p>

      </div>
    </div>
  );
}
