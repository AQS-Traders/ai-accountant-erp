"use client";

/* ==================================================================
   FounderChat — the public-site assistant (home page only).
   Talks to the SAME FastAPI backend as the ERP agent
   (POST /api/public/founder-chat), but that endpoint has NO tools
   and NO database access: it answers only informational questions
   about the product and its founder (Zameer Haider). Data-shaped
   questions are refused server-side before the model is ever called.

   Hidden entirely for logged-in users — they already have the full
   permissioned in-app AI agent.
   ================================================================== */

import { useEffect, useRef, useState } from "react";
import { Bot, Send, Sparkles, X } from "lucide-react";

type Msg = { role: "user" | "assistant"; text: string };

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

const GREETING: Msg = {
  role: "assistant",
  text: "Hi! I'm Ledger — ask me anything about AI Accountant or its founder, Zameer Haider. (Informational questions only — your business data lives safely inside the app.)",
};

/* ---- Multi-colour 3D robot mark -------------------------------------
   A self-contained, resolution-independent 3D-styled robot: layered
   gradients + specular gloss + a contact shade give it real volume, and
   the palette is genuinely multi-colour (white shell · teal ear pods ·
   amber antenna · glowing cyan eyes · indigo visor). Inline SVG, so there
   is no image request, no icon-font dependency and no WebGL cost. */
function RobotMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 48 48" className={className} aria-hidden="true">
      <defs>
        <linearGradient id="pb3dShell" x1="0.1" y1="0" x2="0.5" y2="1">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="48%" stopColor="#eaf7fa" />
          <stop offset="100%" stopColor="#b9dff0" />
        </linearGradient>
        <linearGradient id="pb3dVisor" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#16304f" />
          <stop offset="55%" stopColor="#1d4166" />
          <stop offset="100%" stopColor="#0a1b2e" />
        </linearGradient>
        <linearGradient id="pb3dPod" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#5eead4" />
          <stop offset="55%" stopColor="#14b8a6" />
          <stop offset="100%" stopColor="#0e7490" />
        </linearGradient>
        <linearGradient id="pb3dAntenna" x1="0.2" y1="0" x2="0.8" y2="1">
          <stop offset="0%" stopColor="#fde68a" />
          <stop offset="45%" stopColor="#fbbf24" />
          <stop offset="100%" stopColor="#f97316" />
        </linearGradient>
        <linearGradient id="pb3dStem" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#0e7490" />
          <stop offset="100%" stopColor="#134e5e" />
        </linearGradient>
        <radialGradient id="pb3dEye" cx="0.36" cy="0.3" r="0.85">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="42%" stopColor="#7dd3fc" />
          <stop offset="100%" stopColor="#0e7490" />
        </radialGradient>
        <linearGradient id="pb3dGloss" x1="0" y1="0" x2="0.7" y2="1">
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.95" />
          <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="pb3dShade" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#5b9fc4" stopOpacity="0" />
          <stop offset="100%" stopColor="#5b9fc4" stopOpacity="0.45" />
        </linearGradient>
        <radialGradient id="pb3dGlow" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="#22d3ee" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#22d3ee" stopOpacity="0" />
        </radialGradient>
        {/* interior shading/gloss is clipped to the head silhouette so the
            volume never spills outside the robot */}
        <clipPath id="pb3dHeadClip">
          <rect x="8.6" y="10.6" width="30.8" height="28.6" rx="11.4" />
        </clipPath>
      </defs>
      <circle cx="24" cy="27" r="21" fill="url(#pb3dGlow)" />

      {/* contact shade that grounds the mark */}
      <ellipse cx="24" cy="42.6" rx="12.4" ry="2.5" fill="#0f172a" opacity="0.13" />

      {/* antenna: dark-teal stem + amber ball with a specular dot */}
      <rect x="22.7" y="5.6" width="2.6" height="6.8" rx="1.3" fill="url(#pb3dStem)" />
      <circle cx="24" cy="5.4" r="4.3" fill="url(#pb3dAntenna)" />
      <circle cx="22.5" cy="4.0" r="1.35" fill="#ffffff" opacity="0.8" />

      {/* teal ear pods, each with a darker inner disc */}
      <rect x="4.4" y="18.6" width="6.6" height="12.4" rx="3.3" fill="url(#pb3dPod)" />
      <rect x="37" y="18.6" width="6.6" height="12.4" rx="3.3" fill="url(#pb3dPod)" />
      <ellipse cx="7.7" cy="24.8" rx="2" ry="3.3" fill="#0f766e" opacity="0.5" />
      <ellipse cx="40.3" cy="24.8" rx="2" ry="3.3" fill="#0f766e" opacity="0.5" />

      {/* head shell */}
      <rect x="8.6" y="10.6" width="30.8" height="28.6" rx="11.4" fill="url(#pb3dShell)" />

      {/* volume: bottom interior shade + top specular gloss (clipped) */}
      <g clipPath="url(#pb3dHeadClip)">
        <rect x="8.6" y="28.6" width="30.8" height="10.6" fill="url(#pb3dShade)" />
        <path d="M13.4 12.4c5.8-2.6 15.4-2.6 20.4 0-3.4 2.2-17 2.2-20.4 0z" fill="url(#pb3dGloss)" />
      </g>

      {/* indigo visor + a soft glass streak */}
      <rect x="13.2" y="18.2" width="21.6" height="12.8" rx="6" fill="url(#pb3dVisor)" />
      <path d="M16.6 20.6c4-1.5 11-1.7 14.8-0.5" stroke="#ffffff" strokeOpacity="0.32" strokeWidth="1.5" strokeLinecap="round" fill="none" />

      {/* glowing cyan eyes */}
      <circle cx="19.5" cy="24.4" r="3.2" fill="url(#pb3dEye)" />
      <circle cx="28.5" cy="24.4" r="3.2" fill="url(#pb3dEye)" />

      {/* friendly smile */}
      <path d="M20.6 33.1c1 1.3 2.2 1.9 3.4 1.9s2.4-0.6 3.4-1.9" stroke="#0e7490" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </svg>
  );
}


export default function FounderChat() {
  const [open, setOpen] = useState(false);
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([GREETING]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  /* Logged-in users already have the in-app agent — no site chatbot. */
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { createClient } = await import("@/lib/supabase/client");
        const { data } = await createClient().auth.getSession();
        if (alive) setAuthed(!!data.session);
      } catch {
        if (alive) setAuthed(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (open) {
      scrollRef.current?.scrollTo({
        top: scrollRef.current.scrollHeight,
        behavior: "smooth",
      });
    }
  }, [msgs, open, busy]);

  /* ---- Attention engine --------------------------------------------
     The launcher must URGENT the visitor in: a teaser bubble pops up
     ~2.5s after load, hides after ~4.5s, and repeats every ~10s with
     rotating copy. Any click (bubble or launcher) opens the chat and
     silences the teaser for good. */
  const TEASERS = [
    "Any questions? Ask me!",
    "Curious about AI Accountant? Just ask.",
    "Need help? I speak fluent accounting.",
    "Zameer's AI assistant is right here 👋",
  ];
  const [teaser, setTeaser] = useState<string | null>(null);
  const teaserIdxRef = useRef(0);

  useEffect(() => {
    if (open) return; // chat is open — no need to urge
    let hideTimer: ReturnType<typeof setTimeout> | undefined;

    const show = () => {
      const t = TEASERS[teaserIdxRef.current % TEASERS.length];
      teaserIdxRef.current += 1;
      setTeaser(t);
      hideTimer = setTimeout(() => setTeaser(null), 4500);
    };

    const nextTimer = setTimeout(show, 2500);
    const cycle = setInterval(show, 11000);

    return () => {
      clearTimeout(nextTimer);
      clearTimeout(hideTimer);
      clearInterval(cycle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (authed) return null;

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    const history = msgs
      .filter((m) => m !== GREETING)
      .slice(-6)
      .map((m) => ({ role: m.role, content: m.text }));
    setMsgs((m) => [...m, { role: "user", text }]);
    setInput("");
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/api/public/founder-chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history }),
      });
      const data = await res.json().catch(() => ({}));
      const reply =
        (data && data.reply) ||
        "I couldn't reach the assistant just now — please try again in a moment.";
      setMsgs((m) => [...m, { role: "assistant", text: reply }]);
    } catch {
      setMsgs((m) => [
        ...m,
        {
          role: "assistant",
          text: "Network hiccup — please try again in a moment.",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    /* Raised a level off the extreme bottom edge so it reads as part of
       the page's floating UI instead of hugging the viewport border. */
    <div className="fixed bottom-10 right-4 z-50 flex flex-col items-end gap-2.5">
      {open && (
        <div
          className="w-[calc(100vw-2rem)] max-w-sm rounded-3xl bg-white/95 backdrop-blur-2xl border border-white shadow-[0_30px_70px_-24px_rgba(27,42,74,0.5)] overflow-hidden flex flex-col h-[min(480px,70vh)]"
          style={{ animation: "heroRise 0.35s cubic-bezier(0.19,1,0.22,1) both" }}
          role="dialog"
          aria-label="Founder assistant chat"
        >
          {/* header */}
          <div className="flex items-center gap-2.5 px-4 py-3 bg-gradient-to-r from-teal-500 to-cyan-500 text-white">
            <span className="w-8 h-8 rounded-full bg-white/20 flex items-center justify-center shrink-0">
              <Bot className="w-[18px] h-[18px]" />
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-bold leading-tight">Ledger</span>
              <span className="block text-[10px] opacity-90 leading-tight">
                Founder&apos;s assistant · info only
              </span>
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close chat"
              className="ml-auto w-7 h-7 rounded-full hover:bg-white/20 flex items-center justify-center transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* messages */}
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3.5 space-y-2.5">
            {msgs.map((m, i) => (
              <div
                key={i}
                className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <span
                  className={`max-w-[85%] rounded-2xl px-3.5 py-2 text-[13px] leading-relaxed ${
                    m.role === "user"
                      ? "bg-gradient-to-r from-teal-500 to-cyan-500 text-white rounded-br-md"
                      : "bg-[#f1f5f9] text-[#1e293b] rounded-bl-md"
                  }`}
                >
                  {m.text}
                </span>
              </div>
            ))}
            {busy && (
              <div className="flex justify-start">
                <span className="bg-[#f1f5f9] text-[#64748b] rounded-2xl rounded-bl-md px-3.5 py-2 text-[13px]">
                  Ledger is thinking…
                </span>
              </div>
            )}
          </div>

          {/* input */}
          <div className="border-t border-border-default p-2.5 flex items-center gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder="Ask about the product or the founder…"
              maxLength={600}
              className="flex-1 min-w-0 rounded-full border border-border-default bg-white px-4 py-2.5 text-[13px] text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-teal-500/40"
            />
            <button
              type="button"
              onClick={send}
              disabled={busy || !input.trim()}
              aria-label="Send message"
              className="w-10 h-10 rounded-full bg-gradient-to-r from-teal-500 to-cyan-500 text-white flex items-center justify-center shadow-lg shadow-teal-500/30 transition-all hover:-translate-y-0.5 disabled:opacity-40 disabled:translate-y-0 shrink-0"
            >
              <Send className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}

      {/* Teaser bubble — urges the visitor in (clickable) */}
      {teaser && !open && (
        <button
          type="button"
          onClick={() => {
            setTeaser(null);
            setOpen(true);
          }}
          className="chat-teaser-bubble relative mr-1 mb-1 max-w-[15rem] rounded-2xl rounded-br-md bg-white border border-teal-200 shadow-[0_14px_34px_-12px_rgba(13,148,136,0.45)] px-3.5 py-2.5 text-left text-[13px] font-medium text-brand-navy transition-transform hover:scale-[1.03]"
          style={{ animation: "chatTeaserIn 0.4s cubic-bezier(0.19,1,0.22,1) both" }}
          aria-label="Open the assistant"
        >
          <span className="absolute -bottom-1.5 right-6 w-3 h-3 rotate-45 bg-white border-b border-r border-teal-200" />
          {teaser}
        </button>
      )}

      {/* Launcher — sonar rings + bobbing descriptive Bot icon */}
      <div className="relative">
        {/* sonar rings (visual only) */}
        <span
          aria-hidden="true"
          className="chat-pulse-ring pointer-events-none absolute inset-0 rounded-full bg-teal-500/50"
          style={{ animation: "chatPulse 2.4s cubic-bezier(0.22,1,0.36,1) infinite" }}
        />
        <span
          aria-hidden="true"
          className="chat-pulse-ring pointer-events-none absolute inset-0 rounded-full bg-cyan-400/40"
          style={{ animation: "chatPulse 2.4s cubic-bezier(0.22,1,0.36,1) 1.2s infinite" }}
        />
        <button
          type="button"
          onClick={() => {
            setTeaser(null);
            setOpen((o) => !o);
          }}
          aria-label={open ? "Close assistant" : "Open assistant — ask any question about AI Accountant"}
          aria-expanded={open}
          className="chat-launcher-core relative w-16 h-16 sm:w-[70px] sm:h-[70px] rounded-full bg-gradient-to-br from-teal-500 via-cyan-500 to-indigo-500 p-[3px] shadow-2xl shadow-teal-500/45 transition-all hover:-translate-y-1 active:scale-95"
          style={{ animation: "chatBob 3.4s ease-in-out infinite" }}
        >
          {/* Multi-colour 3D robot on a bright core: 40px of layered
              gradient volume (white shell · teal pods · amber antenna ·
              cyan eyes · indigo visor) inside the teal→cyan→indigo ring,
              with the warm amber sparkle accent. */}
          <span className="w-full h-full rounded-full bg-white flex items-center justify-center">
            {open ? (
              <X className="w-8 h-8 text-brand-navy" strokeWidth={2.4} />
            ) : (
              <RobotMark className="w-10 h-10 drop-shadow-[0_3px_6px_rgba(13,148,136,0.35)]" />
            )}
          </span>
          {!open && (
            <span className="absolute -top-0.5 -right-0.5 w-6 h-6 rounded-full bg-gradient-to-br from-amber-400 to-orange-500 shadow-md flex items-center justify-center ring-2 ring-white">
              <Sparkles className="w-3.5 h-3.5 text-white" strokeWidth={2.5} />
            </span>
          )}
        </button>
      </div>
    </div>
  );
}
