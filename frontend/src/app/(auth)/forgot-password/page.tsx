"use client";

import { useState } from "react";
import Link from "next/link";
import Image from "next/image";
import { MailCheck, Loader2, Mail } from "lucide-react";
import { createClient } from "@/lib/supabase/client";
import { getSiteUrl } from "@/lib/site-url";

/**
 * Forgot Password — step 1 of the Supabase recovery flow.
 * Sends the recovery email via `auth.resetPasswordForEmail`; the email's
 * link bounces back to /reset-password, where the user sets a new
 * password (`auth.updateUser` — step 2).
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [resending, setResending] = useState(false);
  const [error, setError] = useState("");

  const sendReset = async (isResend: boolean) => {
    setError("");
    if (isResend) setResending(true);
    else setSending(true);

    const { error: resetError } = await supabase.auth.resetPasswordForEmail(
      email.trim(),
      { redirectTo: `${getSiteUrl()}/reset-password` },
    );

    setSending(false);
    setResending(false);
    if (resetError) {
      setError(resetError.message);
      return;
    }
    setSent(true);
  };

  const supabase = createClient();

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
          {sent ? "Check your inbox" : "Forgot your password?"}
        </h1>
        <p className="text-sm text-text-secondary mt-1">
          {sent
            ? "We've sent a password reset link to your email."
            : "Enter your email and we'll send you a secure reset link."}
        </p>
      </div>

      {sent ? (
        <>
          <div className="flex items-start gap-3 rounded-xl bg-success-50 border border-success-100 p-4">
            <MailCheck className="w-5 h-5 text-success-600 shrink-0 mt-0.5" />
            <div className="text-sm">
              <p className="font-medium text-brand-navy">
                Reset link sent to <span className="font-semibold">{email}</span>
              </p>
              <p className="text-text-secondary mt-1">
                Open the email and follow the link to choose a new password.
                The link expires shortly for your security — if it&apos;s not in
                your inbox, check the spam folder.
              </p>
            </div>
          </div>

          <button
            type="button"
            onClick={() => sendReset(true)}
            disabled={resending}
            className="w-full py-2.5 rounded-xl border border-border-default bg-bg-surface text-sm font-medium text-text-primary hover:bg-bg-primary focus:outline-none focus:ring-2 focus:ring-brand-teal/30 disabled:opacity-60 transition flex items-center justify-center gap-2"
          >
            {resending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Mail className="w-4 h-4" />}
            {resending ? "Resending..." : "Didn't get it? Resend email"}
          </button>
        </>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); sendReset(false); }} className="space-y-4">
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

          <button
            type="submit"
            disabled={sending}
            className="w-full py-2.5 btn-3d btn-shine rounded-xl bg-brand-teal text-white text-sm font-medium hover:bg-brand-navy focus:outline-none focus:ring-2 focus:ring-brand-teal/30 disabled:opacity-60 transition flex items-center justify-center gap-2"
          >
            {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
            {sending ? "Sending reset link..." : "Send reset link"}
          </button>
        </form>
      )}

      <p className="text-center text-sm text-text-secondary">
        Remembered it?{" "}
        <Link href="/login" className="text-brand-teal font-medium hover:text-brand-navy">
          Back to sign in
        </Link>
      </p>
    </div>
  );
}