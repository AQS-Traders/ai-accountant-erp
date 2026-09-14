"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import {
  getCachedOrgContext,
  clearOrgCache,
  loadOrgContext,
} from "@/lib/hooks/useOrg";
import { User, LogOut, Bell, Search } from "lucide-react";
import Image from "next/image";

export default function TopBar() {
  const [userName, setUserName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [orgLogo, setOrgLogo] = useState<string | null>(null);
  const [showMenu, setShowMenu] = useState(false);
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;

    /* Org name/logo come from the SHARED session cache (useOrg's loader):
       zero duplicate membership queries, zero auth round-trips. The cache
       is usually already warm because every page calls useOrg. */
    const cached = getCachedOrgContext();
    if (cached) {
      setOrgName(cached.org.name);
      setOrgLogo(cached.org.logo_url);
    }

    (async () => {
      const supabase = createClient();
      const { data: { session } } = await supabase.auth.getSession();
      const user = session?.user;
      if (!cancelled && user) {
        setUserName(user.user_metadata?.full_name || user.email || "User");
      }
      if (!cached) {
        try {
          const snap = await loadOrgContext();
          if (!cancelled && snap) {
            setOrgName(snap.org.name);
            setOrgLogo(snap.org.logo_url);
          }
        } catch {
          /* display-only context — page-level useOrg surfaces errors */
        }
      }
    })();

    return () => { cancelled = true; };
  }, []);

  const handleLogout = async () => {
    await createClient().auth.signOut();
    clearOrgCache();
    router.push("/login");
  };

  return (
    <header className="h-14 w-full bg-bg-surface/70 backdrop-blur-xl border-b border-border-subtle flex items-center justify-between px-4 lg:px-6 shrink-0 print:hidden">
      <div className="flex items-center gap-3 min-w-0">
        {orgLogo ? (
          /* Merged with the header background: no box, no border, no shadow
             - the logo sits directly on the surface like native UI. */
          <Image
            src={orgLogo}
            alt={orgName || "Company logo"}
            width={480}
            height={96}
            priority
            className="h-11 w-auto max-w-[260px] object-contain object-left"
          />
        ) : orgName ? (
          <h1 className="text-sm font-semibold text-text-primary truncate">{orgName}</h1>
        ) : null}
      </div>

      <div className="flex items-center gap-1 shrink-0">
        <button className="p-2 rounded-lg text-text-muted hover:text-text-primary hover:bg-bg-muted transition-[color,background-color,transform] duration-150 ease-out hover:scale-110 active:scale-95">
          <Search className="w-4 h-4" />
        </button>
        <button className="relative p-2 rounded-lg text-text-muted hover:text-text-primary hover:bg-bg-muted transition-[color,background-color,transform] duration-150 ease-out hover:scale-110 active:scale-95">
          <Bell className="w-4 h-4" />
          <span className="absolute top-1.5 right-1.5 w-2 h-2 bg-error-500 rounded-full ring-2 ring-bg-surface" />
        </button>

        <div className="relative ml-1">
          <button
            onClick={() => setShowMenu(!showMenu)}
            className="flex items-center gap-2 px-2 py-1.5 rounded-xl text-sm hover:bg-bg-muted transition-colors"
          >
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-ai-100 to-ai-200 flex items-center justify-center shadow-sm">
              <User className="w-4 h-4 text-ai-700" />
            </div>
            <div className="hidden md:flex flex-col items-start">
              <span className="text-sm font-medium text-text-primary max-w-28 truncate leading-tight">{userName}</span>
              <span className="text-[10px] text-text-muted leading-tight">Administrator</span>
            </div>
          </button>

          {showMenu && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setShowMenu(false)} />
              <div className="absolute right-0 top-full mt-1 w-48 bg-bg-surface rounded-xl shadow-lg border border-border-subtle py-1 z-20">
                <button
                  onClick={handleLogout}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text-secondary hover:text-error-600 hover:bg-bg-muted transition-colors"
                >
                  <LogOut className="w-4 h-4" />
                  Sign Out
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
