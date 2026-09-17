"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import Image from "next/image";
import { useRouter } from "next/navigation";
import { Eye, EyeOff, KeyRound, Loader2, ShieldAlert, CheckCircle2 } from "lucide-react";
import { createClient } from "@/lib/supabase/client";

/**
 * Reset Password — step 2 of the Supabase recovery flow.
 * The recovery email bounces here with tokens in the URL fragment; the
 * browser client stores the recovery session automatically. We gate the
 * new-password form on a live session (`PASSWORD_RECOVERY` event or an
 * existing session), then call `auth.updateUser({ password })`.
 */
export default function ResetPasswordPage() {
  const router = useRouter();
  const [stage, setStage] = useState<"checking" | "form" | "expired" | "done">("checking");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  /* Gate: the recovery link must have landed us a live session. The
     browser client consumes the URL fragment automatically; we also
     listen for the PASSWORD_RECOVERY event as belt-and-braces. */
  useEffect(() => {
    const supabase = createClient();
    let settled = false;

    const { data: sub } = supabase.auth.onAuthStateChange((event) => {
      if (event === "PASSWORD_RECOVERY" || event === "SIGNED_IN") {
        settled = true;
        setStage("form");
      }
    });

    (async () => {
      // Give the client a beat to process the URL hash tokens.
      await new Promise((r) => setTimeout(r, 1200));
      const { data } = await supabase.auth.getSession();
      if (!settled && data.session) {
        settled = true;
        setStage("form");
      }
      if (!settled) setStage("expired");
    })();

    return () => sub.subscription.unsubscribe();
  }, []);

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    setSaving(true);
    const supabase = createClient();
    const { error: updError } = await supabase.auth.updateUser({ password });
    setSaving(false);
    if (updError) {
      setError(updError.message);
      return;
    }
    setStage("done");
  };

  return (
    <div className="bg-bg-surface/80 backdrop-blur-sm rounded-2xl shadow-lg border border-border-subtle p-8 space-y-6">
      <div className="text-center">
        <div className="flex justify-center mb-5">
          <Image
            src="/ai-accountant-logo.svg"
            alt="AI Accountant"
            width={220}
            height={63}
            className="w-full max-w-[200px] h-auto object-contain"
            priority
          />
        </div>
        <h1 className="text-2xl font-semibold text-brand-navy">
          {stage === "done" ? "Password updated" : "Set a new password"}
        </h1>
        <p className="text-sm text-text-secondary mt-1">
          {stage === "done"
            ? "Your password has been changed successfully."
            : "Choose a strong password for your account."}
        </p>
      </div>

      {stage === "checking" && (
        <div className="flex flex-col items-center gap-3 py-8 text-text-secondary">
          <Loader2 className="w-6 h-6 animate-spin text-brand-teal" />
          <p className="text-sm">Verifying your secure link…</p>
        </div>
      )}

      {stage === "expired" && (
        <div className="space-y-4">
          <div className="flex items-start gap-3 rounded-xl bg-amber-50 border border-amber-200 p-4">
            <ShieldAlert className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
            <div className="text-sm">
              <p className="font-medium text-brand-navy">This reset link is invalid or has expired.</p>
              <p className="text-text-secondary mt-1">
                For your security, password reset links only work once and
                expire quickly. Request a fresh link and try again.
              </p>
            </div>
          </div>
          <Link
            href="/forgot-password"
            className="w-full py-2.5 btn-3d btn-shine rounded-xl bg-brand-teal text-white text-sm font-medium hover:bg-brand-navy focus:outline-none focus:ring-2 focus:ring-brand-teal/30 transition flex items-center justify-center gap-2"
          >
            Request a new reset link
          </Link>
          <p className="text-center text-sm text-text-secondary">
            <Link href="/login" className="text-brand-teal font-medium hover:text-brand-navy">
              Back to sign in
            </Link>
          </p>
        </div>
      )}

      {stage === "form" && (
        <form onSubmit={handleReset} className="space-y-4">
          {error && (
            <div className="p-3 rounded-lg bg-error-50 border border-error-100 text-sm text-error-600">
              {error}
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-brand-navy mb-1.5">New password</label>
            <div className="relative">
              <input
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                className="w-full px-3 py-2.5 pr-10 rounded-xl border border-border-default bg-bg-surface text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-brand-teal/30 focus:border-brand-teal transition"
                placeholder="At least 8 characters"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-secondary"
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-brand-navy mb-1.5">Confirm new password</label>
            <input
              type={showPassword ? "text" : "password"}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              minLength={8}
              className="w-full px-3 py-2.5 rounded-xl border border-border-default bg-bg-surface text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-brand-teal/30 focus:border-brand-teal transition"
              placeholder="Repeat the new password"
            />
          </div>

          <button
            type="submit"
            disabled={saving}
            className="w-full py-2.5 btn-3d btn-shine rounded-xl bg-brand-teal text-white text-sm font-medium hover:bg-brand-navy focus:outline-none focus:ring-2 focus:ring-brand-teal/30 disabled:opacity-60 transition flex items-center justify-center gap-2"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <KeyRound className="w-4 h-4" />}
            {saving ? "Updating password..." : "Update password"}
          </button>
        </form>
      )}

      {stage === "done" && (
        <div className="space-y-4">
          <div className="flex items-start gap-3 rounded-xl bg-success-50 border border-success-100 p-4">
            <CheckCircle2 className="w-5 h-5 text-success-600 shrink-0 mt-0.5" />
            <p className="text-sm text-text-secondary">
              You&apos;re signed in with your new password and can head
              straight to your workspace.
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              router.push("/");
              router.refresh();
            }}
            className="w-full py-2.5 btn-3d btn-shine rounded-xl bg-brand-teal text-white text-sm font-medium hover:bg-brand-navy focus:outline-none focus:ring-2 focus:ring-brand-teal/30 transition"
          >
            Go to dashboard
          </button>
          <p className="text-center text-sm text-text-secondary">
            <Link href="/login" className="text-brand-teal font-medium hover:text-brand-navy">
              Sign in with the new password instead
            </Link>
          </p>
        </div>
      )}
    </div>
  );
}