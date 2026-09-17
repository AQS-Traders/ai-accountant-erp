"use client";

import { useState, useEffect, useRef } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import Image from "next/image";
import {
  LayoutDashboard, FileText, ShoppingCart, Users, Truck,
  FolderKanban, Receipt, CreditCard, Landmark, BookOpen,
  BarChart3, Settings, ChevronDown, ChevronRight, Menu, X,
  Sparkles, PanelLeftClose, PanelLeft, Wallet,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils/cn";

interface NavItem {
  label: string;
  href?: string;
  icon: LucideIcon;
  /** Per-module identity colours (multi-colour navigation). */
  tone?: { icon: string; chip: string };
  children?: { label: string; href: string }[];
}

const navItems: NavItem[] = [
  { label: "Dashboard", href: "/", icon: LayoutDashboard,
    tone: { icon: "text-ai-600", chip: "bg-ai-50" } },
  {
    label: "Sales", icon: FileText,
    tone: { icon: "text-success-600", chip: "bg-success-50" },
    children: [
      { label: "Quotations", href: "/sales/quotations" },
      { label: "Invoices", href: "/sales/invoices" },
      { label: "Credit Notes", href: "/sales/credit-notes" },
    ],
  },
  {
    label: "Purchases", icon: ShoppingCart,
    tone: { icon: "text-warning-600", chip: "bg-warning-50" },
    children: [
      { label: "Purchase Bills", href: "/purchases/bills" },
      { label: "Purchase Returns", href: "/purchases/returns" },
    ],
  },
  {
    label: "Accounting", icon: BookOpen,
    tone: { icon: "text-rose-600", chip: "bg-rose-50" },
    children: [
      { label: "Chart of Accounts", href: "/accounting/chart-of-accounts" },
      { label: "Journal", href: "/accounting/journal" },
      { label: "General Ledger", href: "/accounting/general-ledger" },
      { label: "Trial Balance", href: "/accounting/trial-balance" },
    ],
  },
  { label: "Customers", href: "/customers", icon: Users,
    tone: { icon: "text-info-600", chip: "bg-info-50" } },
  { label: "Suppliers", href: "/suppliers", icon: Truck,
    tone: { icon: "text-purple-600", chip: "bg-purple-50" } },
  { label: "Projects", href: "/projects", icon: FolderKanban,
    tone: { icon: "text-pink-600", chip: "bg-pink-50" } },
  { label: "Expenses", href: "/expenses", icon: Receipt,
    tone: { icon: "text-orange-600", chip: "bg-orange-50" } },
  { label: "Payments", href: "/payments", icon: CreditCard,
    tone: { icon: "text-indigo-600", chip: "bg-indigo-50" } },
  { label: "Receipts", href: "/receipts", icon: Wallet,
    tone: { icon: "text-emerald-600", chip: "bg-emerald-50" } },
  { label: "Banking", href: "/banking", icon: Landmark,
    tone: { icon: "text-sky-600", chip: "bg-sky-50" } },
  {
    label: "Reports", icon: BarChart3,
    tone: { icon: "text-fuchsia-600", chip: "bg-fuchsia-50" },
    children: [
      { label: "Profit & Loss", href: "/reports/profit-loss" },
      { label: "Balance Sheet", href: "/reports/balance-sheet" },
      { label: "Cash Flow", href: "/reports/cash-flow" },
      { label: "Aging", href: "/reports/aging" },
      { label: "Project P&L", href: "/reports/project-profitability" },
    ],
  },
  { label: "AI Activity", href: "/ai-activity", icon: Sparkles,
    tone: { icon: "text-ai-700", chip: "bg-ai-100" } },
  { label: "Settings", href: "/settings", icon: Settings,
    tone: { icon: "text-slate-500", chip: "bg-slate-100" } },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [mounted, setMounted] = useState(false);
  const asideRef = useRef<HTMLElement>(null);

  // Entrance animation fires once on initial mount (no artificial delay)
  useEffect(() => {
    setMounted(true);
  }, []);

  const toggleGroup = (label: string) => {
    setOpenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  };

  const isActive = (href: string) => {
    if (href === "/") return pathname === "/";
    return pathname.startsWith(href);
  };

  /* ---- Shared logo block ---- */
  const logoBlock = (isDrawer = false) => (
    <div
      className={cn(
        "flex flex-col items-center justify-center border-b",
        collapsed && !isDrawer ? "px-2 py-4 border-sidebar-border" : "px-5 py-5 border-sidebar-border",
      )}
    >
      {collapsed && !isDrawer ? (
        <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-brand-teal to-brand-navy flex items-center justify-center shadow-sm">
          <span className="text-white font-bold text-[11px]">Ai</span>
        </div>
      ) : (
        <>
          <Image
            src="/ai-accountant-logo.svg"
            alt="AI Accountant"
            width={220}
            height={62}
            className={cn(
              "w-full max-w-[200px] h-auto object-contain",
              !mounted ? "opacity-0" : "logo-enter-subtle",
            )}
            priority
          />
          <span className="text-[10px] text-brand-teal/80 font-medium tracking-wide mt-1.5">
            Developed by Zameer Haider
          </span>
        </>
      )}
    </div>
  );

  /* ---- Shared navigation ---- */
  const navBlock = () => (
    <div className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
      {navItems.map((item) => {
        const active = item.href ? isActive(item.href) : item.children?.some((c) => isActive(c.href));
        const Icon = item.icon;
        const isOpen = openGroups.has(item.label);

        if (item.children) {
          return (
            <div key={item.label}>
              <button
                onClick={() => toggleGroup(item.label)}
                className={cn(
                  "group w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-[13px] font-medium transition-[background-color,color,box-shadow,transform] duration-150 ease-out hover-glow",
                  active
                    ? "text-brand-navy bg-sidebar-active-bg shadow-sm"
                    : "text-sidebar-nav hover:text-brand-navy hover:bg-sidebar-hover hover:translate-x-1",
                )}
              >
                <span
                  className={cn(
                    "icon-pop w-7 h-7 rounded-lg flex items-center justify-center shrink-0 transition-colors",
                    item.tone?.chip ?? "bg-transparent",
                  )}
                >
                  <Icon
                    className={cn(
                      "w-4 h-4 shrink-0",
                      item.tone?.icon ?? (active ? "text-brand-teal" : "")
                    )}
                  />
                </span>
                {!collapsed && (
                  <>
                    <span className="flex-1 text-left transition-transform duration-200 group-hover:translate-x-0.5">{item.label}</span>
                    {isOpen
                      ? <ChevronDown className="w-3.5 h-3.5 text-sidebar-nav/60" />
                      : <ChevronRight className="w-3.5 h-3.5 text-sidebar-nav/60" />
                    }
                  </>
                )}
              </button>
              {isOpen && !collapsed && (
                <div className="ml-7 mt-0.5 space-y-0.5 border-l-2 border-sidebar-border pl-2">
                  {item.children.map((child) => (
                    <Link
                      key={child.href}
                      href={child.href}
                      className={cn(
                        "block px-2.5 py-1.5 rounded-lg text-[13px] transition-colors",
                        isActive(child.href)
                          ? "text-brand-navy font-medium bg-sidebar-active-bg"
                          : "text-sidebar-nav/75 hover:text-brand-navy hover:bg-sidebar-hover/60",
                      )}
                    >
                      {child.label}
                    </Link>
                  ))}
                </div>
              )}
            </div>
          );
        }

        return (
          <Link
            key={item.href!}
            href={item.href!}
            className={cn(
              "group flex items-center gap-2.5 px-3 py-2 rounded-xl text-[13px] font-medium transition-[background-color,color,box-shadow,transform] duration-150 ease-out hover-glow",
              active
                ? "text-brand-navy bg-sidebar-active-bg shadow-sm"
                : "text-sidebar-nav hover:text-brand-navy hover:bg-sidebar-hover hover:translate-x-1",
            )}
          >
            <span
              className={cn(
                "icon-pop w-7 h-7 rounded-lg flex items-center justify-center shrink-0",
                item.tone?.chip ?? "bg-transparent",
              )}
            >
              <Icon
                className={cn(
                  "w-4 h-4 shrink-0",
                  item.tone?.icon ?? (active ? "text-brand-teal" : "")
                )}
              />
            </span>
            {!collapsed && <span className="transition-transform duration-200 group-hover:translate-x-0.5">{item.label}</span>}
          </Link>
        );
      })}
    </div>
  );

  /* ---- Collapse toggle (desktop only) ---- */
  const collapseToggle = () => (
    <button
      onClick={() => setCollapsed(!collapsed)}
      className="px-4 py-2.5 border-t border-sidebar-border text-sidebar-nav/70 hover:text-brand-navy text-xs flex items-center gap-2 transition-colors"
    >
      {collapsed
        ? <PanelLeft className="w-4 h-4" />
        : (
          <>
            <PanelLeftClose className="w-4 h-4" />
            <span>Collapse</span>
          </>
        )
      }
    </button>
  );

  return (
    <>
      {/* Mobile toggle */}
      <button
        onClick={() => setMobileOpen(true)}
        className="lg:hidden fixed top-3 left-3 z-50 p-2 rounded-xl bg-bg-surface shadow-md print:hidden border border-border-subtle"
        aria-label="Open menu"
      >
        <Menu className="w-5 h-5 text-brand-navy" />
      </button>

      {/* Mobile overlay drawer */}
      {mobileOpen && (
        <div className="lg:hidden fixed inset-0 z-40 bg-black/40" onClick={() => setMobileOpen(false)}>
          <aside
            className="w-64 h-full bg-gradient-to-b from-sidebar-bg/90 to-sidebar-bg-end/90 backdrop-blur-xl flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={() => setMobileOpen(false)}
              className="absolute top-3 right-3 p-1 text-sidebar-nav hover:text-brand-navy z-10"
            >
              <X className="w-5 h-5" />
            </button>
            {logoBlock(true)}
            {navBlock()}
          </aside>
        </div>
      )}

      {/* Desktop sidebar */}
      <aside
        ref={asideRef}
        className={cn(
          "hidden lg:flex flex-col bg-gradient-to-b from-sidebar-bg/75 to-sidebar-bg-end/75 backdrop-blur-xl border-r border-sidebar-border transition-[width] duration-200 ease-out shrink-0 print:hidden",
          collapsed ? "w-[68px]" : "w-[252px]",
        )}
      >
        {logoBlock()}
        {navBlock()}
        {collapseToggle()}
      </aside>
    </>
  );
}
