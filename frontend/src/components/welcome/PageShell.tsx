"use client";

/* Shared shell for the marketing pages (Features / How It Works /
   Solutions / Pricing / Resources). Keeps the exact home-page UI
   language: light glass nav, surreal pastel stage, serif display,
   aurora accents, contact footer. */

import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Fraunces, Cormorant_Garamond } from "next/font/google";
import { ArrowRight, Mail, MessageCircle } from "lucide-react";
import { cn } from "@/lib/utils/cn";

const fraunces = Fraunces({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-fraunces",
  display: "swap",
  preload: false,
});
const cormorant = Cormorant_Garamond({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-cormorant",
  display: "swap",
  preload: false,
});

export const TABS = [
  { label: "Features", href: "/features" },
  { label: "How It Works", href: "/how-it-works" },
  { label: "Solutions", href: "/solutions" },
  { label: "Pricing", href: "/pricing" },
  { label: "Resources", href: "/resources" },
];

export function LinkedInIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 1 1 0-4.125 2.062 2.062 0 0 1 0 4.125zM7.119 20.452H3.555V9h3.564v11.452z" />
    </svg>
  );
}
export default function PageShell({
  eyebrow,
  title,
  accent,
  titleTail,
  subtitle,
  children,
}: {
  eyebrow: string;
  title: string;
  accent: string;
  titleTail?: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 40);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className={cn(fraunces.variable, cormorant.variable, "min-h-screen bg-bg-primary")}>
      {/* NAV — identical to home */}
      <nav
        className={cn(
          "fixed top-0 left-0 right-0 z-50 transition-all duration-500",
          scrolled
            ? "bg-white/70 backdrop-blur-2xl border-b border-white/80 shadow-[0_10px_40px_-18px_rgba(27,42,74,0.25)] py-2.5"
            : "bg-transparent py-4",
        )}
      >
        <div className="max-w-6xl mx-auto px-4 sm:px-6 flex items-center justify-between">
          <Link href="/welcome" className="flex items-center">
            <Image
              src="/ai-accountant-logo.svg"
              alt="AI Accountant"
              width={220}
              height={60}
              className="h-12 sm:h-14 w-auto object-contain [filter:none] [box-shadow:none] [border:none]"
              priority
            />
          </Link>
          <div className="hidden lg:flex items-center gap-7 text-sm font-medium text-[#3d4b66]">
            {TABS.map((l) => (
              <Link key={l.label} href={l.href} className="hover:text-brand-navy transition-colors">
                {l.label}
              </Link>
            ))}
          </div>
          <div className="flex items-center gap-3">
            <Link
              href="/login"
              className="px-4 py-2 rounded-xl text-sm font-semibold text-brand-navy hover:text-ai-700 border border-transparent hover:border-border-default hover:bg-white/70 transition-all"
            >
              Login
            </Link>
            <Link
              href="/signup"
              className="lp-btn-primary px-5 py-2 rounded-xl bg-gradient-to-r from-teal-500 to-indigo-500 text-white text-sm font-semibold transition-all shadow-lg shadow-indigo-500/25 hover:shadow-indigo-500/45 hover:-translate-y-px"
            >
              Get Started
            </Link>
          </div>
        </div>
      </nav>
      {/* PAGE HERO — the same surreal pastel stage */}
      <header className="lp-stage relative overflow-hidden">
        <div className="lp-blob lp-blob-a" />
        <div className="lp-blob lp-blob-b" />
        <div className="lp-grain" />
        <div className="relative z-10 max-w-6xl mx-auto px-4 sm:px-6 pt-40 pb-16 text-center">
          <p
            className="hero-eyebrow inline-flex items-center gap-2.5 rounded-full bg-white/80 border border-white shadow-[0_10px_30px_-14px_rgba(27,42,74,0.25)] px-4 py-2 text-[10px] sm:text-[11px] font-bold uppercase tracking-[0.16em] text-brand-navy/80 mb-7"
            style={{ animation: "heroRise 0.9s cubic-bezier(0.19,1,0.22,1) 80ms both" }}
          >
            {eyebrow}
          </p>
          <h1
            className="text-brand-navy text-4xl sm:text-5xl lg:text-6xl leading-[1.1] font-semibold tracking-tight max-w-3xl mx-auto"
            style={{ fontFamily: "var(--font-fraunces), Georgia, serif", animation: "heroRise 1s cubic-bezier(0.19,1,0.22,1) 220ms both" }}
          >
            {title} <span className="text-aurora font-semibold italic">{accent}</span>
            {titleTail ? ` ${titleTail}` : ""}
          </h1>
          <p
            className="mt-6 text-lg text-[#3d4b66] leading-relaxed max-w-2xl mx-auto font-light"
            style={{ animation: "heroRise 1s cubic-bezier(0.19,1,0.22,1) 400ms both" }}
          >
            {subtitle}
          </p>
        </div>
      </header>

      <main className="relative z-10">{children}</main>
      {/* FOOTER — contact the builder */}
      <footer className="relative overflow-hidden border-t border-white/70 bg-white/50 backdrop-blur">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-14 text-center">
          <h3
            className="text-2xl sm:text-3xl font-semibold text-brand-navy"
            style={{ fontFamily: "var(--font-fraunces), Georgia, serif" }}
          >
            Built by <span className="text-aurora italic">Zameer Haider</span>
          </h3>
          <p className="mt-3 text-sm text-[#3d4b66] max-w-md mx-auto">
            Questions, feedback, or a walkthrough? Reach out directly — every message gets a reply.
          </p>
          <div className="mt-7 flex flex-wrap items-center justify-center gap-4">
            <a
              href="https://pk.linkedin.com/in/zameerhaiderchattha"
              target="_blank"
              rel="noopener noreferrer"
              className="lp-btn-primary inline-flex items-center gap-2.5 px-6 py-3 rounded-full bg-gradient-to-r from-blue-600 to-sky-500 text-white text-sm font-semibold shadow-lg shadow-blue-500/30 hover:-translate-y-0.5 transition-all"
            >
              <LinkedInIcon className="w-4 h-4" /> LinkedIn
            </a>
            <a
              href="mailto:zameerchattha0@gmail.com"
              className="inline-flex items-center gap-2.5 px-6 py-3 rounded-full bg-white/90 border border-white text-brand-navy text-sm font-semibold shadow-lg hover:-translate-y-0.5 transition-all"
            >
              <Mail className="w-4 h-4 text-indigo-500" /> zameerchattha0@gmail.com
            </a>
            <a
              href="https://wa.me/923230714288"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2.5 px-6 py-3 rounded-full bg-white/90 border border-white text-brand-navy text-sm font-semibold shadow-lg hover:-translate-y-0.5 transition-all"
            >
              <MessageCircle className="w-4 h-4 text-emerald-500" /> WhatsApp
            </a>
          </div>
          <div className="mt-10 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-[13px] text-[#3d4b66]">
            {TABS.map((l) => (
              <Link key={l.label} href={l.href} className="hover:text-brand-navy transition-colors inline-flex items-center gap-1">
                {l.label} <ArrowRight className="w-3 h-3 opacity-50" />
              </Link>
            ))}
          </div>
          <p className="mt-8 text-xs text-[#64748b]">© {new Date().getFullYear()} AI Accountant — Intelligent ERP. All rights reserved.</p>
        </div>
      </footer>
    </div>
  );
}


