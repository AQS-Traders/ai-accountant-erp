"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { Cinzel, Cormorant_Garamond, Fraunces, DM_Serif_Display, Antic_Didone, Bodoni_Moda } from "next/font/google";
import {
  Brain, BarChart3, Building2, BookOpen, Search, ShieldCheck,
  ArrowRight, MessageSquareText, HelpCircle, Zap, Workflow,
  FileBarChart, PencilLine, Mail, MessageCircle, ChevronRight,
  FileText, Send, Sparkles, Check,
  TrendingUp, TrendingDown,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils/cn";
import { useInView } from "@/lib/hooks/useInView";
import FounderChat from "@/components/welcome/FounderChat";
import dynamic from "next/dynamic";

/* "Ledger" — the hero-stage 3D mascot. Client-only: WebGL never runs on
   the server; renders nothing until the scene is interactive. */
const HeroRobotStage = dynamic(() => import("@/components/hero/HeroRobotStage"), {
  ssr: false,
  loading: () => null,
});


/* Premium editorial type: Cinzel = Roman inscriptional display capitals
   (Trajan-style, extended edges) for the hero; Cormorant Garamond italic
   for the accent word. Body text keeps the app's sans stack. */
const cinzel = Cinzel({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-cinzel",
  display: "swap",
  /* Only the hero face is preloaded. Chrome warned "preloaded but not used
     within a few seconds" for the decorative families, which are swapped in
     further down the page anyway. */
  preload: false,
});
const cormorant = Cormorant_Garamond({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: ["normal", "italic"],
  variable: "--font-cormorant",
  display: "swap",
  preload: false,
});
/* Fraunces — soft premium editorial serif (reference headline face) */
const fraunces = Fraunces({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-fraunces",
  display: "swap",
  preload: false,
});
/* Tagline faces: DM Serif Display (italic didone elegance) + Antic Didone
   (rendered bold via synthetic weight for the thick-hairline didone look) */
const dmSerif = DM_Serif_Display({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-dm-serif",
  display: "swap",
  preload: false,
});
const antic = Antic_Didone({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-antic",
  display: "swap",
  preload: false,
});
/* Bodoni Moda — the hero headline face (high-contrast didone, variable
   weight incl. bold 700 + italic for the power words). */
const bodoni = Bodoni_Moda({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-bodoni",
  display: "swap",
});

/* Brand glyph — lucide-react no longer ships brand icons (Linkedin was
   removed from the library), so the LinkedIn mark is inlined here. */
function LinkedInIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 1 1 0-4.125 2.062 2.062 0 0 1 0 4.125zM7.119 20.452H3.555V9h3.564v11.452z" />
    </svg>
  );
}
/* ---- Rotating hero taglines (the POWER WORDS) --------------------
   Rendered in Bodoni Moda ITALIC with the BRAND colour (teal→cyan —
   the same brand ramp as every CTA and the text-aurora accent), so
   the power words read unmistakably as the brand's voice. Dwell
   times vary 2.8–3.6s for an organic cadence. Rotation pauses under
   prefers-reduced-motion (the first variant then remains). */
const HERO_TAGLINES = [
  { text: "Smarter with AI.", dur: 3000 },
  { text: "Effortless. Intelligent.", dur: 3400 },
  { text: "Always Balanced.", dur: 2800 },
  { text: "Precision on Autopilot.", dur: 3600 },
  { text: "Powered by Intelligence.", dur: 3000 },
  { text: "Built to Grow With You.", dur: 3200 },
] as const;

/* Brand gradient shared by every tagline variant */
const TAGLINE_GRADIENT =
  "linear-gradient(100deg, #0f766e 0%, #0891b2 55%, #06b6d4 100%)";

function HeroTagline() {
  const [idx, setIdx] = useState(0);
  /* The outgoing line lingers behind the incoming one for the crossfade,
     then unmounts once its exit animation has fully played. */
  const [prevIdx, setPrevIdx] = useState<number | null>(null);
  const idxRef = useRef(0);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (mq.matches) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = () => {
      if (cancelled) return;
      const next = (idxRef.current + 1) % HERO_TAGLINES.length;
      setPrevIdx(idxRef.current);
      idxRef.current = next;
      setIdx(next);
      timer = setTimeout(tick, HERO_TAGLINES[next].dur);
    };
    /* First swap after the masked reveal lands + one full dwell */
    timer = setTimeout(tick, HERO_TAGLINES[0].dur + 1200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  /* Retire the outgoing line after its 0.8s exit finishes */
  useEffect(() => {
    if (prevIdx === null) return;
    const id = setTimeout(() => setPrevIdx(null), 850);
    return () => clearTimeout(id);
  }, [prevIdx]);

  const t = HERO_TAGLINES[idx];
  return (
    <span
      className="hero-tagline-stack"
      style={{
        fontFamily: "var(--font-bodoni), Georgia, serif",
        fontStyle: "italic",
        fontWeight: 700,
      }}
    >
      {prevIdx !== null && prevIdx !== idx && (
        <span
          key={`out-${prevIdx}`}
          className="hero-tagline-rotate hero-tagline-out"
          style={{ backgroundImage: TAGLINE_GRADIENT }}
        >
          {HERO_TAGLINES[prevIdx].text}
        </span>
      )}
      <span
        key={idx}
        className="hero-tagline-rotate"
        style={{ backgroundImage: TAGLINE_GRADIENT }}
      >
        {t.text}
      </span>
    </span>
  );
}

/* ---- Ledger's routines -------------------------------------------
   The hero used to carry an on-screen "Commands" launcher + deck of
   click-to-run routines. That UI has been REMOVED from the hero, so the
   stage stays clean: clicking Ledger himself (or letting the ambient loop
   run) is how routines now happen. The robot actor still listens for the
   `ledger-command` event, so any click-to-perform trigger keeps working. */





/* ---- Reusable animated section wrapper ---- */
function Section({
  children,
  className,
  animation = "animate-fade-in-up",
  delay = "0ms",
}: {
  children: React.ReactNode;
  className?: string;
  animation?: string;
  delay?: string;
}) {
  const { ref, inView } = useInView();
  return (
    <div
      ref={ref}
      className={cn(
        "opacity-0",
        inView && animation,
        className,
      )}
      style={{ animationDelay: delay }}
    >
      {children}
    </div>
  );
}

/* ---- Animated counter ---- */
function Counter({ target, suffix = "" }: { target: number; suffix?: string }) {
  const { ref, inView } = useInView();
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!inView) return;
    let start = 0;
    const duration = 1800;
    const step = Math.max(1, Math.floor(target / (duration / 16)));
    const timer = setInterval(() => {
      start += step;
      if (start >= target) {
        setCount(target);
        clearInterval(timer);
      } else {
        setCount(start);
      }
    }, 16);
    return () => clearInterval(timer);
  }, [inView, target]);

  return <span ref={ref}>{count.toLocaleString()}{suffix}</span>;
}

/* ---- Feature card — glass pane in a gradient hairline ring ---- */
function FeatureCard({
  icon: Icon,
  title,
  description,
  tone,
  delay,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  tone: { chip: string; icon: string; from: string; to: string };
  delay: string;
}) {
  const { ref, inView } = useInView();
  return (
    <div
      ref={ref}
      className={cn(
        "opacity-0 group relative rounded-3xl p-[1.5px] transition-transform duration-300 hover:-translate-y-1.5",
        inView && "animate-scale-in",
      )}
      style={{
        animationDelay: delay,
        background: `linear-gradient(135deg, ${tone.from}, ${tone.to})`,
      }}
    >
      <div className="glass-panel rounded-[calc(1.5rem-1.5px)] p-6 h-full">
        <div
          className={cn(
            "w-12 h-12 rounded-2xl flex items-center justify-center mb-4 clay-chip transition-transform duration-300 group-hover:scale-110 group-hover:rotate-3",
            tone.chip,
          )}
        >
          <Icon className={cn("w-5 h-5", tone.icon)} />
        </div>
        <h3 className="text-sm font-semibold text-text-primary mb-1.5">{title}</h3>
        <p className="text-sm text-text-secondary leading-relaxed">{description}</p>
      </div>
    </div>
  );
}

/* ---- Workflow step card — one stage of the core AI pipeline ---- */
function WorkflowCard({
  icon: Icon,
  step,
  title,
  description,
  tone,
  delay,
}: {
  icon: LucideIcon;
  step: number;
  title: string;
  description: string;
  tone: { chip: string; icon: string; badge: string; from: string; to: string };
  delay: string;
}) {
  const { ref, inView } = useInView();
  return (
    <div
      ref={ref}
      className={cn(
        "opacity-0 group relative rounded-3xl p-[1.5px] transition-transform duration-300 hover:-translate-y-1.5",
        inView && "animate-scale-in",
      )}
      style={{
        animationDelay: delay,
        background: `linear-gradient(135deg, ${tone.from}, ${tone.to})`,
      }}
    >
      <div className="glass-panel rounded-[calc(1.5rem-1.5px)] p-5 h-full">
        <div className="flex items-center justify-between mb-4">
          <div
            className={cn(
              "w-11 h-11 rounded-xl flex items-center justify-center clay-chip transition-transform duration-300 group-hover:scale-110 group-hover:rotate-3",
              tone.chip,
            )}
          >
            <Icon className={cn("w-5 h-5", tone.icon)} />
          </div>
          <span
            className={cn(
              "w-7 h-7 rounded-full text-white text-xs font-bold flex items-center justify-center shadow-md",
              tone.badge,
            )}
          >
            {step}
          </span>
        </div>
        <h3 className="text-sm font-semibold text-brand-navy mb-1.5">{title}</h3>
        <p className="text-xs text-[#3d4b66] leading-relaxed">{description}</p>
      </div>
    </div>
  );
}

/* ---- Floating glass chip that orbits Ledger's hero stage ---- */
function FloatChip({
  icon: Icon,
  chip,
  icon2,
  title,
  sub,
  note,
  delay,
  target,
}: {
  icon: LucideIcon;
  chip: string;
  icon2: string;
  title: string;
  sub: string;
  note?: string;
  delay: string;
  target?: string;
}) {
  /* SAME GLASS LANGUAGE as the Business Overview card: glass-panel +
     glass-soft, one radius, one fixed width and NO rotation. Previously
     these chips were white cards with individual tilts, which made the
     stage look scattered next to the glass cards. */
  return (
    <div
      data-ledger-target={target}
      className={cn(
        "hero-float glass-panel glass-soft rounded-2xl w-56",
        "shadow-xl px-4 py-3.5 flex items-center gap-3",
      )}
      style={{ animation: `floaty 6s ease-in-out ${delay} infinite` }}
    >
      <span className={cn("w-11 h-11 rounded-2xl clay-chip flex items-center justify-center shrink-0 shadow-sm", chip)}>
        <Icon className={cn("w-5 h-5", icon2)} />
      </span>
      <span className="min-w-0 max-w-[11rem]">
        <span className="block text-[14px] font-bold text-brand-navy">{title}</span>
        <span className="flex items-center gap-1 text-[11px] font-semibold text-emerald-600">
          <Check className="w-3 h-3" strokeWidth={3} />
          {sub}
        </span>
        {note ? <span className="block text-[10px] text-[#64748b] truncate">{note}</span> : null}
      </span>
    </div>
  );
}

/* ================================================================== */
/* LANDING PAGE                                                         */
/* ================================================================== */
export default function WelcomePage() {
  const [scrolled, setScrolled] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const heroRef = useRef<HTMLElement>(null);
  /* Business Overview chart bars — Ledger re-randomises them when he pokes the card */
  const DEFAULT_BARS = [38, 55, 42, 70, 48, 62, 80, 58, 90, 66, 74, 95, 60, 85, 52, 78, 68, 88];
  const [bars, setBars] = useState<number[]>(DEFAULT_BARS);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 40);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  /* Hamburger menu: close on outside pointer-down or Escape */
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: PointerEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  /* Ledger's interactions: when he pokes a hero element, that element
     pops/glows; the Business Overview chart also gets fresh data. */
  useEffect(() => {
    const onPoke = (e: Event) => {
      const target = (e as CustomEvent<{ target: string }>).detail?.target;
      if (!target) return;
      const el = document.querySelector(`[data-ledger-target="${target}"]`);
      if (el) {
        el.classList.remove("ledger-poked");
        void (el as HTMLElement).offsetWidth; // restart the animation
        el.classList.add("ledger-poked");
        window.setTimeout(() => el.classList.remove("ledger-poked"), 1500);
      }
      if (target === "overview") {
        setBars(Array.from({ length: 18 }, () => Math.round(30 + Math.random() * 65)));
      }
    };
    window.addEventListener("ledger-poke", onPoke);
    return () => window.removeEventListener("ledger-poke", onPoke);
  }, []);

  /* Cursor parallax: writes normalised pointer offsets into --px/--py on
     the hero stage; the aurora glows drift AGAINST the cursor while the
     copy lifts WITH it — a subtle, physical sense of depth. */
  const handlePointerMove = (e: React.PointerEvent) => {
    const el = heroRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--px", ((e.clientX - r.left) / r.width - 0.5).toFixed(3));
    el.style.setProperty("--py", ((e.clientY - r.top) / r.height - 0.5).toFixed(3));
  };
  const handlePointerLeave = () => {
    const el = heroRef.current;
    if (!el) return;
    el.style.setProperty("--px", "0");
    el.style.setProperty("--py", "0");
  };

  const features = [
    {
      icon: Brain,
      title: "AI Accounting Agent",
      description:
        "Tell the AI what happened in plain language. It handles the journal entries, posting, and reconciliation automatically.",
      tone: {
        chip: "bg-gradient-to-br from-teal-100 to-teal-50",
        icon: "text-teal-600",
        from: "#2dd4bf",
        to: "#0ea5e9",
      },
    },
    {
      icon: BarChart3,
      title: "Real-time Financial Reports",
      description:
        "Profit and Loss, Balance Sheet, Cash Flow, Trial Balance. All reports update the moment entries are posted.",
      tone: {
        chip: "bg-gradient-to-br from-indigo-100 to-indigo-50",
        icon: "text-indigo-600",
        from: "#818cf8",
        to: "#6366f1",
      },
    },
    {
      icon: Building2,
      title: "Multi-tenant Architecture",
      description:
        "Each organization gets isolated data with row-level security. Your books stay private and compliant.",
      tone: {
        chip: "bg-gradient-to-br from-rose-100 to-rose-50",
        icon: "text-rose-600",
        from: "#fb7185",
        to: "#e11d48",
      },
    },
    {
      icon: BookOpen,
      title: "Automated Journal Engine",
      description:
        "Documents like invoices and purchase bills automatically generate balanced journal entries. No manual debits and credits.",
      tone: {
        chip: "bg-gradient-to-br from-amber-100 to-amber-50",
        icon: "text-amber-600",
        from: "#fbbf24",
        to: "#f59e0b",
      },
    },
    {
      icon: Search,
      title: "Smart Entity Search",
      description:
        "Find customers, suppliers, accounts, and transactions instantly with fuzzy search across your entire ledger.",
      tone: {
        chip: "bg-gradient-to-br from-sky-100 to-sky-50",
        icon: "text-sky-600",
        from: "#38bdf8",
        to: "#0284c7",
      },
    },
    {
      icon: ShieldCheck,
      title: "Enterprise Compliance",
      description:
        "Role-based permissions, audit trails, confirmation gates for high-risk operations, and full accounting period management.",
      tone: {
        chip: "bg-gradient-to-br from-violet-100 to-violet-50",
        icon: "text-violet-600",
        from: "#a78bfa",
        to: "#7c3aed",
      },
    },
  ];

  /* Core workflow — from the project summary */
  const workflow = [
    {
      icon: MessageSquareText,
      title: "Describe",
      description: "Tell the AI what happened in natural language — no forms, no tabs.",
      tone: { chip: "bg-gradient-to-br from-teal-100 to-teal-50", icon: "text-teal-600", badge: "bg-teal-500", from: "#2dd4bf", to: "#0ea5e9" },
    },
    {
      icon: Brain,
      title: "Understand",
      description: "The AI identifies the transaction and reasons about its accounting nature.",
      tone: { chip: "bg-gradient-to-br from-indigo-100 to-indigo-50", icon: "text-indigo-600", badge: "bg-indigo-500", from: "#818cf8", to: "#6366f1" },
    },
    {
      icon: HelpCircle,
      title: "Clarify",
      description: "Targeted questions for anything missing: amount, purpose, cash or credit, party, asset or expense.",
      tone: { chip: "bg-gradient-to-br from-amber-100 to-amber-50", icon: "text-amber-600", badge: "bg-amber-500", from: "#fbbf24", to: "#f59e0b" },
    },
    {
      icon: ShieldCheck,
      title: "Confirm",
      description: "You approve the understanding and the proposed accounting treatment where required.",
      tone: { chip: "bg-gradient-to-br from-rose-100 to-rose-50", icon: "text-rose-600", badge: "bg-rose-500", from: "#fb7185", to: "#e11d48" },
    },
    {
      icon: Zap,
      title: "Execute",
      description: "The AI creates the journal entry and updates the ledger automatically — no manual tab-by-tab entry.",
      tone: { chip: "bg-gradient-to-br from-sky-100 to-sky-50", icon: "text-sky-600", badge: "bg-sky-500", from: "#38bdf8", to: "#0284c7" },
    },
    {
      icon: Workflow,
      title: "Connect",
      description: "Entries flow through Journal → Ledger → Trial Balance → Chart of Accounts → Financial Statements.",
      tone: { chip: "bg-gradient-to-br from-violet-100 to-violet-50", icon: "text-violet-600", badge: "bg-violet-500", from: "#a78bfa", to: "#7c3aed" },
    },
    {
      icon: FileBarChart,
      title: "Report & Document",
      description: "Invoices, quotations, aging, receivable/payable and financial reports — from the same system.",
      tone: { chip: "bg-gradient-to-br from-emerald-100 to-emerald-50", icon: "text-emerald-600", badge: "bg-emerald-500", from: "#34d399", to: "#059669" },
    },
    {
      icon: PencilLine,
      title: "Manual Recording",
      description: "Prefer traditional control? Record journal entries and transactions manually, anytime.",
      tone: { chip: "bg-gradient-to-br from-pink-100 to-pink-50", icon: "text-pink-600", badge: "bg-pink-500", from: "#f472b6", to: "#db2777" },
    },
  ];

  /* Overall flow strip */
  const FLOW = [
    "Business Activity",
    "AI Input",
    "Reasoning",
    "Clarification",
    "Confirmation",
    "Recording",
    "Ledger",
    "Trial Balance",
    "Financial Statements",
  ];
  const FLOW_DOTS = ["bg-teal-500", "bg-indigo-500", "bg-amber-500", "bg-rose-500", "bg-sky-500", "bg-violet-500", "bg-emerald-500", "bg-pink-500", "bg-teal-500"];

  return (
    <div className={cn(cinzel.variable, cormorant.variable, fraunces.variable, dmSerif.variable, antic.variable, bodoni.variable, "min-h-screen bg-bg-primary")}>
      {/* ============================================================ */}
      {/* NAVBAR — light floating glass                                 */}
      {/* ============================================================ */}
      <nav
        className={cn(
          "fixed top-0 left-0 right-0 z-50 transition-all duration-500",
          scrolled
            ? "bg-white/70 backdrop-blur-2xl border-b border-white/80 shadow-[0_10px_40px_-18px_rgba(27,42,74,0.25)] py-2.5"
            : "bg-transparent py-4",
        )}
      >
        <div className="max-w-6xl mx-auto px-4 sm:px-6 flex items-center justify-between">
          <Link href="/" className="flex items-center">
            <Image
              src="/ai-accountant.png"
              alt="AI Accountant"
              width={220}
              height={60}
              className="h-12 sm:h-14 w-auto object-contain [filter:none] [box-shadow:none] [border:none]"
              priority
            />
          </Link>
          {/* Desktop (lg+): classic header — inline links + auth actions.
              The hamburger is reserved for smaller viewports. */}
          <div className="hidden lg:flex items-center gap-7 text-sm font-medium text-[#3d4b66]">
            {[
              { label: "Features", href: "/features" },
              { label: "How It Works", href: "/how-it-works" },
              { label: "Solutions", href: "/solutions" },
              { label: "Pricing", href: "/pricing" },
              { label: "Resources", href: "/resources" },
            ].map((l) => (
              <Link key={l.label} href={l.href} className="hover:text-brand-navy transition-colors">
                {l.label}
              </Link>
            ))}
            <Link
              href="/login"
              className="px-4 py-2 rounded-full text-sm font-semibold text-brand-navy border border-slate-200/90 bg-white/70 hover:bg-white transition-colors"
            >
              Login
            </Link>
            <Link
              href="/signup"
              className="px-5 py-2 rounded-full bg-gradient-to-r from-teal-500 to-cyan-500 text-white text-sm font-semibold shadow-lg shadow-teal-500/25 hover:shadow-cyan-500/45 transition-all"
            >
              Get Started
            </Link>
          </div>
          {/* Hamburger approach — one menu button below lg holding the nav
              links + auth actions in a glass dropdown. */}
          <div ref={menuRef} className="relative flex items-center lg:hidden">
            <button
              type="button"
              aria-label={menuOpen ? "Close menu" : "Open menu"}
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
              className="w-11 h-11 rounded-xl bg-white/85 backdrop-blur border border-white shadow-[0_10px_30px_-14px_rgba(27,42,74,0.35)] flex flex-col items-center justify-center gap-[5px] transition-all hover:bg-white hover:shadow-[0_14px_34px_-14px_rgba(27,42,74,0.45)] active:scale-95"
            >
              <span className={cn("block w-5 h-[2px] rounded-full bg-brand-navy transition-all duration-300", menuOpen && "translate-y-[7px] rotate-45")} />
              <span className={cn("block w-5 h-[2px] rounded-full bg-brand-navy transition-all duration-300", menuOpen && "opacity-0")} />
              <span className={cn("block w-5 h-[2px] rounded-full bg-brand-navy transition-all duration-300", menuOpen && "-translate-y-[7px] -rotate-45")} />
            </button>

            {menuOpen && (
              <div
                className="absolute right-0 top-[calc(100%+12px)] w-64 rounded-2xl bg-white/95 backdrop-blur-2xl border border-white shadow-[0_30px_70px_-24px_rgba(27,42,74,0.45)] p-2.5 flex flex-col"
                style={{ animation: "heroRise 0.35s cubic-bezier(0.19,1,0.22,1) both" }}
              >
                {[
                  { label: "Features", href: "/features" },
                  { label: "How It Works", href: "/how-it-works" },
                  { label: "Solutions", href: "/solutions" },
                  { label: "Pricing", href: "/pricing" },
                  { label: "Resources", href: "/resources" },
                ].map((l) => (
                  <Link
                    key={l.label}
                    href={l.href}
                    onClick={() => setMenuOpen(false)}
                    className="px-3.5 py-2.5 rounded-xl text-sm font-medium text-[#3d4b66] hover:text-brand-navy hover:bg-teal-50/70 transition-colors"
                  >
                    {l.label}
                  </Link>
                ))}
                <div className="h-px bg-slate-200/80 my-2" />
                <Link
                  href="/login"
                  onClick={() => setMenuOpen(false)}
                  className="px-3.5 py-2.5 rounded-xl text-sm font-semibold text-brand-navy hover:bg-slate-100 transition-colors text-center"
                >
                  Login
                </Link>
                <Link
                  href="/signup"
                  onClick={() => setMenuOpen(false)}
                  className="mt-1 px-3.5 py-2.5 rounded-xl bg-gradient-to-r from-teal-500 to-cyan-500 text-white text-sm font-semibold text-center shadow-lg shadow-teal-500/25 hover:shadow-cyan-500/45 transition-all"
                >
                  Get Started
                </Link>
              </div>
            )}
          </div>
        </div>
      </nav>

      {/* ============================================================ */}
      {/* HERO — surrealist glass-clay light stage                      */}
      {/* ============================================================ */}
      <section
        ref={heroRef}
        onPointerMove={handlePointerMove}
        onPointerLeave={handlePointerLeave}
        className="lp-stage relative min-h-screen flex items-center"
      >
        {/* Editorial backdrop — the bright white-teal office. The photo IS
            the stage; soft white veils keep the copy side readable while the
            city view breathes on the right, where Ledger stands. */}
        <div className="absolute inset-0">
          <Image
            src="/hero-office.png"
            alt=""
            fill
            priority
            sizes="100vw"
            className="object-cover object-center select-none pointer-events-none"
          />
          <div className="absolute inset-0 bg-gradient-to-r from-white/95 via-white/70 to-white/5" />
          <div className="absolute inset-x-0 top-0 h-28 bg-gradient-to-b from-white/85 to-transparent" />
          <div className="absolute inset-x-0 bottom-0 h-36 bg-gradient-to-b from-transparent to-white" />
        </div>

        {/* Whisper of grain */}
        <div className="lp-grain" />

        <div className="lp-copy relative z-10 w-full max-w-6xl mx-auto px-4 sm:px-6 pt-28 pb-24 lg:pt-32 lg:pb-28 lg:self-stretch lg:flex lg:flex-col lg:justify-center">
          <div className="grid grid-cols-1 lg:grid-cols-[1.04fr_0.96fr] gap-6 lg:gap-8 items-center">
            {/* TOP — copy. Ledger lives in the stage below this, never over it. */}
            <div className="max-w-xl min-w-0">
            <p
              className="hero-eyebrow inline-flex items-center gap-2.5 rounded-full bg-white/80 border border-white shadow-[0_10px_30px_-14px_rgba(27,42,74,0.25)] px-4 py-2 text-[10px] sm:text-[11px] font-bold uppercase tracking-[0.16em] text-brand-navy/80 mb-7"
              style={{ animationDelay: "80ms" }}
            >
              <Sparkles className="w-3.5 h-3.5 text-teal-600 shrink-0" />
              <span className="min-w-0">AI-native accounting for modern businesses</span>
            </p>

            <h1
              className="text-brand-navy text-[2.3rem] sm:text-5xl lg:text-[3.4rem] leading-[1.14] tracking-[0.01em] font-bold"
              style={{ fontFamily: "var(--font-bodoni), Georgia, serif" }}
            >
              <span className="sr-only">
                Your Books, {HERO_TAGLINES[0].text}
              </span>
              <span className="reveal-line" aria-hidden="true">
                <span style={{ "--d": "200ms" } as React.CSSProperties}>Your Books,</span>
              </span>
              <span className="reveal-line" aria-hidden="true">
                <span style={{ "--d": "420ms" } as React.CSSProperties}>
                  <HeroTagline />
                </span>
              </span>
            </h1>

            <p
              className="mt-7 text-lg sm:text-xl text-[#3d4b66] leading-relaxed max-w-xl font-light"
              style={{ animation: "heroRise 1s cubic-bezier(0.19,1,0.22,1) 650ms both" }}
            >
              Record transactions in plain English. Get instant financial
              reports. Let the AI handle debits, credits, and compliance
              while you focus on growing your business.
            </p>

            <div
              className="flex flex-wrap items-center gap-4 mt-10"
              style={{ animation: "heroRise 1s cubic-bezier(0.19,1,0.22,1) 820ms both" }}
            >
              <Link
                href="/signup"
                className="lp-btn-primary inline-flex items-center gap-2.5 px-8 py-4 rounded-full bg-gradient-to-r from-teal-500 to-cyan-500 text-white font-semibold text-base transition-all shadow-xl shadow-teal-500/30 hover:shadow-cyan-500/45 hover:-translate-y-0.5"
              >
                Start Free <ArrowRight className="w-4 h-4" />
              </Link>
              <a
                href="https://pk.linkedin.com/in/zameerhaiderchattha"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-3 pl-3 pr-7 py-3 rounded-full bg-white/90 backdrop-blur border border-white text-brand-navy font-semibold text-base transition-all shadow-[0_18px_40px_-18px_rgba(27,42,74,0.35)] hover:-translate-y-0.5 hover:shadow-[0_22px_46px_-18px_rgba(27,42,74,0.45)]"
              >
                <span className="w-9 h-9 rounded-full bg-gradient-to-br from-blue-600 to-sky-500 flex items-center justify-center shadow-md shadow-blue-500/30">
                  <LinkedInIcon className="w-4 h-4 text-white" />
                </span>
                Contact
              </a>
            </div>

              {/* trust badges */}
              <div
                className="flex flex-wrap gap-x-6 gap-y-2.5 mt-8"
                style={{ animation: "heroRise 1s cubic-bezier(0.19,1,0.22,1) 950ms both" }}
              >
                {[["No credit card required"], ["Set up in minutes"], ["Built for startups & SMEs"]].map(([tb]) => (
                  <span key={tb} className="inline-flex items-center gap-2 text-[13px] font-medium text-[#3d4b66]">
                    <span className="w-[18px] h-[18px] rounded-full bg-emerald-500 flex items-center justify-center shrink-0 shadow-sm shadow-emerald-500/40">
                      <Check className="w-3 h-3 text-white" strokeWidth={3.5} />
                    </span>
                    {tb}
                  </span>
                ))}
              </div>
            </div>

            {/* RIGHT / FRONT — Ledger's stage. Below lg it flows under the
                copy; on lg+ it becomes an absolute overlay spanning the FULL
                hero-section height (top edge to bottom edge). His canvas
                bottom therefore sits exactly on the backdrop image's bottom
                border on lg+, and the `-mb-24` below lg cancels the copy
                column's pb-24 so the canvas bottom lands on the image's
                bottom border there too — Ledger's boots meet the floor line
                instead of hovering above it in every layout. DELIBERATELY no
                entrance animation: Ledger is a native part of the hero — he
                is simply there from the first frame, on every load. */}
            <div
              className="relative h-[420px] sm:h-[500px] md:h-[540px] -mb-24 lg:mb-0 -mx-3 sm:mx-0 lg:absolute lg:z-20 lg:inset-y-0 lg:mx-0 lg:h-auto lg:left-1/2 lg:w-[50vw]"
              onPointerDown={(e) => {
                /* Mouse control: click Ledger directly (the canvas) and he
                   responds with an engaging routine. Clicks on cards/deck
                   buttons bubble from their own elements and are ignored. */
                if (!(e.target instanceof HTMLCanvasElement)) return;
                const reactions = ["bow", "robotdance", "wave", "kungfu"] as const;
                const cmd = reactions[Math.floor(Math.random() * reactions.length)];
                window.dispatchEvent(new CustomEvent("ledger-command", { detail: { command: cmd } }));
              }}
            >
              {/* canvas above the chips so Ledger walks IN FRONT of them */}
              <div className="absolute inset-0 z-10 cursor-pointer">
                <HeroRobotStage variant="hero" />
              </div>

              {/* NOTE: the old "Commands" launcher + deck lived here. It was
                  removed from the hero — the stage stays clean; click
                  Ledger himself to see his routines. */}

              {/* MOBILE — same cards as the desktop stage, but laid BEHIND
                  Ledger starting at his SHOULDERS and extending up past his
                  head (canvas z-10 paints the robot over them). */}
              <div className="md:hidden absolute inset-x-2 bottom-[36%] z-0 grid grid-cols-2 gap-3 pointer-events-none">
                <div className="glass-panel glass-soft rounded-2xl shadow-xl px-3.5 py-3">
                  <span className="w-9 h-9 rounded-2xl bg-gradient-to-br from-emerald-100 to-emerald-50 clay-chip flex items-center justify-center mb-2">
                    <FileText className="w-4 h-4 text-emerald-600" />
                  </span>
                  <span className="block text-[12px] font-bold text-brand-navy">Sales Invoice</span>
                  <span className="flex items-center gap-1 text-[10px] font-semibold text-emerald-600">
                    <Check className="w-2.5 h-2.5" strokeWidth={3} /> Created · INV-2025-001
                  </span>
                </div>
                <div className="glass-panel glass-soft rounded-2xl shadow-xl px-3.5 py-3">
                  <span className="w-9 h-9 rounded-2xl bg-gradient-to-br from-indigo-100 to-indigo-50 clay-chip flex items-center justify-center mb-2">
                    <BookOpen className="w-4 h-4 text-indigo-600" />
                  </span>
                  <span className="block text-[12px] font-bold text-brand-navy">Journal Entry</span>
                  <span className="flex items-center gap-1 text-[10px] font-semibold text-emerald-600">
                    <Check className="w-2.5 h-2.5" strokeWidth={3} /> Recorded · 2 Lines
                  </span>
                </div>
                <div className="glass-panel glass-soft rounded-2xl shadow-xl px-3.5 py-3">
                  <span className="w-9 h-9 rounded-2xl bg-gradient-to-br from-sky-100 to-sky-50 clay-chip flex items-center justify-center mb-2">
                    <BarChart3 className="w-4 h-4 text-sky-600" />
                  </span>
                  <span className="block text-[12px] font-bold text-brand-navy">Trial Balance</span>
                  <span className="flex items-center gap-1 text-[10px] font-semibold text-emerald-600">
                    <Check className="w-2.5 h-2.5" strokeWidth={3} /> Updated · As of today
                  </span>
                </div>
                <div className="glass-panel glass-soft rounded-2xl shadow-xl px-3.5 py-3">
                  <span className="w-9 h-9 rounded-2xl bg-gradient-to-br from-violet-100 to-violet-50 clay-chip flex items-center justify-center mb-2">
                    <Sparkles className="w-4 h-4 text-violet-600" />
                  </span>
                  <span className="block text-[12px] font-bold text-brand-navy">Ask anything…</span>
                  <span className="block text-[10px] text-[#64748b] truncate">&quot;Invoice for ABC Tech&quot;</span>
                </div>
              </div>

              {/* document chips — left edge, narrow. Ledger walks over and
                  "posts" each one; the chip pops when his hand lands. */}
              <div className="absolute left-0 top-[14%] hidden md:flex flex-col gap-5 pointer-events-none">
                <FloatChip
                  icon={FileText}
                  chip="bg-gradient-to-br from-emerald-100 to-emerald-50"
                  icon2="text-emerald-600"
                  title="Sales Invoice"
                  sub="Created Successfully"
                  note="INV-2025-001"
                  delay="0s"
                  target="invoice"
                />
                <FloatChip
                  icon={BookOpen}
                  chip="bg-gradient-to-br from-indigo-100 to-indigo-50"
                  icon2="text-indigo-600"
                  title="Journal Entry"
                  sub="Recorded"
                  note="2 Lines Posted"
                  delay="1.1s"
                  target="journal"
                />
                <FloatChip
                  icon={BarChart3}
                  chip="bg-gradient-to-br from-sky-100 to-sky-50"
                  icon2="text-sky-600"
                  title="Trial Balance"
                  sub="Updated"
                  note="As of today"
                  delay="2.2s"
                  target="trial"
                />
              </div>
              {/* Business Overview card — right edge, below the chip zone.
                  Ledger pokes it → card wiggles and the chart refreshes. */}
              <div
                data-ledger-target="overview"
                className="absolute right-0 top-[30%] hidden md:block pointer-events-none"
                style={{ animation: "floaty 7s ease-in-out 0.8s infinite" }}
              >
                <div className="hero-float glass-panel glass-soft rounded-2xl p-4 w-56 shadow-xl">
                  <div className="flex items-center justify-between mb-3">
                    <span className="text-xs font-bold text-brand-navy">Business Overview</span>
                    <span className="text-[10px] font-medium text-[#3d4b66] bg-white/80 border border-white rounded-full px-2 py-0.5">
                      This Month
                    </span>
                  </div>
                  <div className="flex items-end gap-[3px] h-16 mb-3">
                    {bars.map((h, i) => (
                      <span
                        key={i}
                        className="flex-1 rounded-full transition-all duration-700 ease-out"
                        style={{ height: `${h}%`, background: "linear-gradient(180deg, #818cf8, #2dd4bf)" }}
                      />
                    ))}
                  </div>
                  <div className="grid grid-cols-3 gap-2">
                    <div>
                      <div className="text-[11px] font-bold text-brand-navy whitespace-nowrap">PKR 1.25M</div>
                      <div className="text-[9px] text-[#64748b]">Revenue</div>
                      <div className="text-[9px] font-bold text-emerald-600 flex items-center gap-0.5">
                        <TrendingUp className="w-2.5 h-2.5" /> 12%
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] font-bold text-brand-navy whitespace-nowrap">PKR 420K</div>
                      <div className="text-[9px] text-[#64748b]">Expenses</div>
                      <div className="text-[9px] font-bold text-rose-500 flex items-center gap-0.5">
                        <TrendingDown className="w-2.5 h-2.5" /> 8%
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] font-bold text-brand-navy whitespace-nowrap">PKR 830K</div>
                      <div className="text-[9px] text-[#64748b]">Net Profit</div>
                      <div className="text-[9px] font-bold text-emerald-600 flex items-center gap-0.5">
                        <TrendingUp className="w-2.5 h-2.5" /> 18%
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              {/* Ask-anything card — bottom right. Ledger taps it → glow + send pulse. */}
              <div
                data-ledger-target="ask"
                className="absolute right-2 bottom-[7%] hidden md:flex pointer-events-none"
                style={{ animation: "floaty 6.5s ease-in-out 1.4s infinite" }}
              >
                <div className="hero-float glass-panel glass-soft rounded-2xl pl-4 pr-2 py-2 flex items-center gap-3 shadow-xl w-72">
                  <Sparkles className="w-4 h-4 text-indigo-500 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-semibold text-brand-navy">Ask anything…</div>
                    <div className="text-[11px] text-[#3d4b66] truncate">&quot;Create an invoice for ABC Tech&quot;</div>
                  </div>
                  <span className="ledger-send w-8 h-8 rounded-xl bg-gradient-to-br from-teal-500 to-indigo-500 flex items-center justify-center shrink-0 shadow-md">
                    <Send className="w-3.5 h-3.5 text-white" />
                  </span>
                </div>
              </div>

              {/* handwritten whisper — retired with the split-screen hero;
                  kept out of the stacked stage so it matches the reference
                  layout exactly on every screen size */}
              <div className="hidden pointer-events-none">
                <span
                  className="text-2xl text-indigo-500/80 italic"
                  style={{ fontFamily: "var(--font-cormorant), Georgia, serif" }}
                >
                  More time for what matters
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Scroll cue */}
        <div className="absolute bottom-8 left-1/2 -translate-x-1/2 z-10 flex flex-col items-center gap-2 text-brand-navy/50">
          <span
            className="text-[10px] uppercase tracking-[0.3em]"
            style={{ fontFamily: "var(--font-cinzel), Georgia, serif" }}
          >
            Scroll
          </span>
          <span className="lp-scrollcue block w-px h-9 bg-gradient-to-b from-teal-500/70 to-transparent" />
        </div>
      </section>

      {/* ============================================================ */}
      {/* FEATURES                                                      */}
      {/* ============================================================ */}
      <section className="py-24 sm:py-32">
        <div className="max-w-6xl mx-auto px-4 sm:px-6">
          <Section className="text-center mb-16">
            <span className="text-xs font-bold text-ai-700 uppercase tracking-widest mb-3 block">
              Features
            </span>
            <h2 className="bodoni-display text-3xl sm:text-4xl font-bold text-brand-navy mb-4">
              Everything your <span className="text-aurora italic">accountant</span> needs
            </h2>
            <p className="text-[#3d4b66] text-lg max-w-2xl mx-auto">
              A complete ERP built for small businesses that want accurate
              books without the complexity.
            </p>
          </Section>

          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-6">
            {features.map((f, i) => (
              <FeatureCard
                key={f.title}
                {...f}
                delay={`${i * 80}ms`}
              />
            ))}
          </div>
        </div>
      </section>

      {/* ============================================================ */}
      {/* CORE WORKFLOW                                                  */}
      {/* ============================================================ */}
      <section className="relative py-24 sm:py-32">
        {/* Surreal colour wash behind the pipeline */}
        <div className="absolute inset-0 bg-gradient-to-b from-transparent via-brand-aqua-soft/70 to-transparent pointer-events-none" />
        <div className="relative max-w-6xl mx-auto px-4 sm:px-6">
          <Section className="text-center mb-16">
            <span className="text-xs font-bold text-indigo-600 uppercase tracking-widest mb-3 block">
              The Core Workflow
            </span>
            <h2 className="bodoni-display text-3xl sm:text-4xl font-bold text-brand-navy mb-4">
              From <span className="text-aurora italic">words</span> to financial statements
            </h2>
            <p className="text-[#3d4b66] text-lg max-w-2xl mx-auto">
              One connected pipeline — describe what happened, and the AI
              reasons, clarifies, confirms, and records it all the way to
              your financial statements.
            </p>
          </Section>

          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-5">
            {workflow.map((s, i) => (
              <WorkflowCard
                key={s.title}
                {...s}
                step={i + 1}
                delay={`${i * 70}ms`}
              />
            ))}
          </div>

          {/* Overall flow strip */}
          <Section className="mt-14">
            <div className="glass-panel rounded-3xl px-6 py-5 flex flex-wrap items-center justify-center gap-x-1.5 gap-y-3">
              {FLOW.map((label, i) => (
                <span key={label} className="inline-flex items-center gap-1.5">
                  <span className="inline-flex items-center gap-2 rounded-full bg-white/80 border border-white px-3 py-1.5 text-xs font-semibold text-brand-navy shadow-sm">
                    <span className={cn("w-1.5 h-1.5 rounded-full", FLOW_DOTS[i])} />
                    {label}
                  </span>
                  {i < FLOW.length - 1 && (
                    <ChevronRight className="w-3.5 h-3.5 text-brand-navy/30 shrink-0" />
                  )}
                </span>
              ))}
            </div>
          </Section>
        </div>
      </section>

      {/* ============================================================ */}
      {/* STATS                                                          */}
      {/* ============================================================ */}
      <section className="py-24 sm:py-32">
        <div className="max-w-6xl mx-auto px-4 sm:px-6">
          <div className="grid sm:grid-cols-3 gap-6">
            <Section animation="animate-scale-in" delay="0ms">
              <div className="glass-panel rounded-3xl p-8 text-center transition-transform duration-300 hover:-translate-y-1">
                <div
                  className="text-4xl sm:text-5xl font-bold mb-2 text-aurora"
                  style={{ fontFamily: "var(--font-cinzel), Georgia, serif" }}
                >
                  <Counter target={36} />
                </div>
                <p className="text-[#3d4b66] text-sm font-medium">AI-powered tools</p>
              </div>
            </Section>
            <Section animation="animate-scale-in" delay="120ms">
              <div className="glass-panel rounded-3xl p-8 text-center transition-transform duration-300 hover:-translate-y-1">
                <div
                  className="text-4xl sm:text-5xl font-bold mb-2 text-brand-navy"
                  style={{ fontFamily: "var(--font-cinzel), Georgia, serif" }}
                >
                  <Counter target={12} />
                </div>
                <p className="text-[#3d4b66] text-sm font-medium">Financial report types</p>
              </div>
            </Section>
            <Section animation="animate-scale-in" delay="240ms">
              <div className="glass-panel rounded-3xl p-8 text-center transition-transform duration-300 hover:-translate-y-1">
                <div
                  className="text-4xl sm:text-5xl font-bold mb-2 text-brand-navy"
                  style={{ fontFamily: "var(--font-cinzel), Georgia, serif" }}
                >
                  <Counter target={78} />
                </div>
                <p className="text-[#3d4b66] text-sm font-medium">Validated parameters</p>
              </div>
            </Section>
          </div>
        </div>
      </section>

      {/* ============================================================ */}
      {/* CTA                                                            */}
      {/* ============================================================ */}
      <section className="py-24 sm:py-32">
        <div className="max-w-6xl mx-auto px-4 sm:px-6">
          <Section>
            <div className="relative rounded-[2rem] p-[1.5px]" style={{ background: "linear-gradient(135deg, #2dd4bf, #6366f1 45%, #f59e0b 80%, #ec4899)" }}>
              <div className="glass-panel rounded-[calc(2rem-1.5px)] p-10 sm:p-16 text-center relative overflow-hidden">
                {/* Surreal floating accents inside the panel */}
                <div className="absolute -top-16 -right-16 w-56 h-56 rounded-full bg-gradient-to-br from-teal-200/50 to-transparent blur-3xl pointer-events-none" />
                <div className="absolute -bottom-20 -left-16 w-64 h-64 rounded-full bg-gradient-to-tr from-indigo-200/50 to-transparent blur-3xl pointer-events-none" />
                <div className="lp-shape lp-shape-square !w-16 !h-16 !top-8 !left-10 opacity-70" />

                <div className="relative z-10">
                  <h2 className="bodoni-display text-3xl sm:text-4xl font-bold text-brand-navy mb-4">
                    Ready to <span className="text-aurora italic">automate</span> your accounting?
                  </h2>
                  <p className="text-[#3d4b66] text-lg max-w-xl mx-auto mb-8">
                    Join businesses that trust AI Accountant to keep their
                    books balanced, accurate, and audit-ready.
                  </p>
                  <Link
                    href="/signup"
                    className="lp-btn-primary inline-flex items-center gap-2 px-8 py-4 rounded-2xl bg-gradient-to-r from-teal-500 to-indigo-500 text-white font-semibold text-base transition-all shadow-xl shadow-indigo-500/30 hover:shadow-indigo-500/50 hover:-translate-y-0.5"
                  >
                    Get Started Free <ArrowRight className="w-4 h-4" />
                  </Link>
                </div>
              </div>
            </div>
          </Section>
        </div>
      </section>

      {/* ============================================================ */}
      {/* CONTACT                                                        */}
      {/* ============================================================ */}
      <section className="py-24 sm:py-32">
        <div className="max-w-6xl mx-auto px-4 sm:px-6">
          <Section className="text-center mb-12">
            <span className="text-xs font-bold text-rose-600 uppercase tracking-widest mb-3 block">
              Contact
            </span>
            <h2 className="bodoni-display text-3xl sm:text-4xl font-bold text-brand-navy mb-4">
              Built by <span className="text-aurora italic">Zameer Haider</span>
            </h2>
            <p className="text-[#3d4b66] text-lg max-w-xl mx-auto">
              Questions, feedback, or a live demo for your business — reach
              out on whichever channel suits you.
            </p>
          </Section>

          <div className="grid sm:grid-cols-3 gap-6">
            {/* LinkedIn */}
            <a
              href="https://pk.linkedin.com/in/zameerhaiderchattha"
              target="_blank"
              rel="noreferrer"
              className="group relative rounded-3xl p-[1.5px] transition-transform duration-300 hover:-translate-y-1.5"
              style={{ background: "linear-gradient(135deg, #38bdf8, #0284c7)" }}
            >
              <div className="glass-panel rounded-[calc(1.5rem-1.5px)] p-6 h-full flex items-center gap-4">
                <span className="w-12 h-12 rounded-2xl bg-gradient-to-br from-sky-100 to-sky-50 clay-chip flex items-center justify-center shrink-0 transition-transform duration-300 group-hover:scale-110">
                  <LinkedInIcon className="w-5 h-5 text-sky-600" />
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-brand-navy">LinkedIn</span>
                  <span className="block text-xs text-[#3d4b66] truncate">zameerhaiderchattha</span>
                </span>
              </div>
            </a>

            {/* Email */}
            <a
              href="mailto:zameerchattha0@gmail.com"
              className="group relative rounded-3xl p-[1.5px] transition-transform duration-300 hover:-translate-y-1.5"
              style={{ background: "linear-gradient(135deg, #fbbf24, #f59e0b)" }}
            >
              <div className="glass-panel rounded-[calc(1.5rem-1.5px)] p-6 h-full flex items-center gap-4">
                <span className="w-12 h-12 rounded-2xl bg-gradient-to-br from-amber-100 to-amber-50 clay-chip flex items-center justify-center shrink-0 transition-transform duration-300 group-hover:scale-110">
                  <Mail className="w-5 h-5 text-amber-600" />
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-brand-navy">Email</span>
                  <span className="block text-xs text-[#3d4b66] truncate">zameerchattha0@gmail.com</span>
                </span>
              </div>
            </a>

            {/* WhatsApp */}
            <a
              href="https://wa.me/923230714288"
              target="_blank"
              rel="noreferrer"
              className="group relative rounded-3xl p-[1.5px] transition-transform duration-300 hover:-translate-y-1.5"
              style={{ background: "linear-gradient(135deg, #34d399, #059669)" }}
            >
              <div className="glass-panel rounded-[calc(1.5rem-1.5px)] p-6 h-full flex items-center gap-4">
                <span className="w-12 h-12 rounded-2xl bg-gradient-to-br from-emerald-100 to-emerald-50 clay-chip flex items-center justify-center shrink-0 transition-transform duration-300 group-hover:scale-110">
                  <MessageCircle className="w-5 h-5 text-emerald-600" />
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-brand-navy">WhatsApp</span>
                  <span className="block text-xs text-[#3d4b66]">+92 323 0714288</span>
                </span>
              </div>
            </a>
          </div>
        </div>
      </section>

      {/* ============================================================ */}
      {/* FOOTER                                                         */}
      {/* ============================================================ */}
      <footer className="border-t border-border-subtle py-8">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 flex flex-col sm:flex-row items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <Image
              src="/ai-accountant.png"
              alt="AI Accountant"
              width={100}
              height={28}
              loading="eager"
              className="h-6 w-auto object-contain opacity-70"
            />
          </div>
          <p className="text-xs text-text-muted">
            &copy; {new Date().getFullYear()} AI Accountant. Developed by Zameer Haider.
          </p>
          <div className="flex items-center gap-4 text-xs text-text-muted">
            <a
              href="https://pk.linkedin.com/in/zameerhaiderchattha"
              target="_blank"
              rel="noreferrer"
              aria-label="LinkedIn"
              className="hover:text-sky-600 transition-colors"
            >
              <LinkedInIcon className="w-4 h-4" />
            </a>
            <a
              href="mailto:zameerchattha0@gmail.com"
              aria-label="Email"
              className="hover:text-amber-600 transition-colors"
            >
              <Mail className="w-4 h-4" />
            </a>
            <a
              href="https://wa.me/923230714288"
              target="_blank"
              rel="noreferrer"
              aria-label="WhatsApp"
              className="hover:text-emerald-600 transition-colors"
            >
              <MessageCircle className="w-4 h-4" />
            </a>
            <span className="w-px h-4 bg-border-default" />
            <Link href="/login" className="hover:text-text-primary transition-colors">
              Login
            </Link>
            <Link href="/signup" className="hover:text-text-primary transition-colors">
              Sign Up
            </Link>
          </div>
        </div>
      </footer>
      <FounderChat />
    </div>
  );
}
