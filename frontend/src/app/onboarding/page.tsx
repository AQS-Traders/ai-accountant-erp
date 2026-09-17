"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { cn } from "@/lib/utils/cn";
import Image from "next/image";
import {
  Building2, Settings2, Coins, FileCheck, Check, Landmark, Upload, X, Sparkles, Wand2,
} from "lucide-react";
import AIOrganizationWizard, {
  type OnboardingApplyPayload,
} from "@/components/onboarding/AIOrganizationWizard";
import { onboardingSchema } from "@/lib/api/client";
import type { OnboardingSchema } from "@/lib/types/api";

const BUSINESS_TYPES = [
  { value: "SOFTWARE_HOUSE", label: "Software House" },
  { value: "IT_SERVICES", label: "IT Services" },
  { value: "CONSULTING", label: "Consulting" },
  { value: "E_COMMERCE", label: "E-Commerce" },
  { value: "MANUFACTURING", label: "Manufacturing" },
  { value: "TRADING", label: "Trading" },
  { value: "CONSTRUCTION", label: "Construction" },
  { value: "HEALTHCARE", label: "Healthcare" },
  { value: "EDUCATION", label: "Education" },
  { value: "OTHER", label: "Other" },
];

const CURRENCIES = ["PKR", "USD", "EUR", "GBP", "AED", "SAR", "INR"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const STEPS = [
  { label: "Business Identity", icon: Building2 },
  { label: "Details", icon: FileCheck },
  { label: "Financial Config", icon: Coins },
  { label: "Document Prefixes", icon: Settings2 },
  { label: "Review & Create", icon: Check },
];

interface FormState {
  name: string;
  business_type: string;
  legal_name: string;
  tax_number: string;
  registration_number: string;
  core_services: string;
  industry_details: string;
  base_currency_code: string;
  country_code: string;
  timezone: string;
  fiscal_year_end_month: number;
  fiscal_year_start_year: number;
  invoice_prefix: string;
  quotation_prefix: string;
  bill_prefix: string;
  journal_prefix: string;
}

const INITIAL: FormState = {
  name: "",
  business_type: "SOFTWARE_HOUSE",
  legal_name: "",
  tax_number: "",
  registration_number: "",
  core_services: "",
  industry_details: "",
  base_currency_code: "PKR",
  country_code: "PK",
  timezone: "Asia/Karachi",
  fiscal_year_end_month: 6,
  fiscal_year_start_year: new Date().getFullYear(),
  invoice_prefix: "INV",
  quotation_prefix: "QUT",
  bill_prefix: "BIL",
  journal_prefix: "JV",
};

/* ---- Small field primitives ---- */

function AiChip() {
  return (
    <span className="inline-flex items-center gap-1 ml-1.5 px-1.5 py-0.5 rounded-full bg-ai-100 text-ai-700 text-[10px] font-medium align-middle">
      <Sparkles className="w-2.5 h-2.5" />
      AI
    </span>
  );
}

function Field({
  label,
  children,
  hint,
  aiFilled,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
  /** Marks a value the assistant proposed (still fully editable). */
  aiFilled?: boolean;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-text-secondary">
        {label}
        {aiFilled && <AiChip />}
      </span>
      <div className="mt-1.5">{children}</div>
      {hint && <span className="text-[11px] text-text-muted mt-1 block">{hint}</span>}
    </label>
  );
}

const inputCls =
  "w-full px-3 py-2 rounded-xl bg-bg-primary border border-border-default text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-100 focus:border-ai-300 transition-colors";

export default function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<FormState>(INITIAL);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [logoPreview, setLogoPreview] = useState<string | null>(null);
  const logoInputRef = useRef<HTMLInputElement>(null);

  /* ---- AI-assisted setup -------------------------------------------------
     The assistant fills this same form, so the user still reviews and edits
     every field.  Two things it also decides are kept separately because the
     form has no field for them: the account bundles to create, and the
     analysis to store as the onboarding record. */
  const [wizardOpen, setWizardOpen] = useState(false);
  const [aiFields, setAiFields] = useState<string[]>([]);
  const [aiSources, setAiSources] = useState<Record<string, string>>({});
  const [aiSummary, setAiSummary] = useState<string | null>(null);
  const [aiGroups, setAiGroups] = useState<string[] | null>(null);
  const [aiGroupLabels, setAiGroupLabels] = useState<{ code: string; label: string }[]>([]);
  const [aiResponses, setAiResponses] = useState<Record<string, unknown> | null>(null);

  /* Real backend-compatible choices (business types with the chart each one
     produces, currencies).  Falls back to the built-in list when the API is
     unreachable, so onboarding never dead-ends. */
  const [schema, setSchema] = useState<OnboardingSchema | null>(null);

  useEffect(() => {
    let cancelled = false;
    onboardingSchema()
      .then((result) => {
        if (!cancelled && result.available) setSchema(result);
      })
      .catch(() => {
        /* keep the built-in fallback */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const businessTypeOptions = useMemo(() => {
    if (!schema?.business_types?.length) return BUSINESS_TYPES;
    return schema.business_types.map((t) => ({
      value: t.value,
      label:
        BUSINESS_TYPES.find((f) => f.value === t.value)?.label ??
        t.template_name ??
        t.value.replace(/_/g, " "),
    }));
  }, [schema]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const slugPreview = useMemo(
    () => form.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, ""),
    [form.name]
  );

  const canNext = () => {
    if (step === 0) return form.name.trim().length >= 2;
    return true;
  };

  const handleCreate = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const supabase = createClient();
      const { data, error: rpcError } = await supabase.rpc("create_organization", {
        p_name: form.name.trim(),
        p_business_type: form.business_type,
        p_base_currency_code: form.base_currency_code,
        p_country_code: form.country_code,
        p_timezone: form.timezone,
        p_fiscal_year_end_month: form.fiscal_year_end_month,
        p_legal_name: form.legal_name.trim() || null,
        p_tax_number: form.tax_number.trim() || null,
        p_registration_number: form.registration_number.trim() || null,
        p_core_services: form.core_services.trim() || null,
        p_industry_details: form.industry_details.trim() || null,
        p_fiscal_year_start_year: form.fiscal_year_start_year,
      });
      if (rpcError) throw rpcError;
      if (!data) throw new Error("Organization creation returned no id");
      const organizationId = data as string;

      // Apply the reviewed onboarding decisions: the account bundles the user
      // approved (or, when the assistant was never used, NULL = the bundles
      // recommended for this business type) plus the analysis record.  This
      // is a no-op for the chart when no optional bundle is selected.
      try {
        const { error: onboardingError } = await supabase.rpc("apply_organization_onboarding", {
          p_organization_id: organizationId,
          p_groups: aiGroups,
          p_responses: aiResponses,
        });
        if (onboardingError) {
          // The organization exists with its base chart either way; never
          // block the user on the optional account bundles.
          console.warn("Optional account bundles were not applied:", onboardingError.message);
        }
      } catch (e) {
        console.warn("Optional account bundles were not applied", e);
      }

      // Persist prefix preferences on the created org's settings row.
      await supabase
        .from("organization_settings")
        .update({
          invoice_prefix: form.invoice_prefix.trim() || "INV",
          quotation_prefix: form.quotation_prefix.trim() || "QUT",
          bill_prefix: form.bill_prefix.trim() || "BIL",
          journal_prefix: form.journal_prefix.trim() || "JV",
        })
        .eq("organization_id", organizationId);

      // Upload logo if selected
      if (logoFile) {
        try {
          const ext = logoFile.name.split(".").pop() || "png";
          const path = `${organizationId}/${Date.now()}.${ext}`;
          const { error: uploadErr } = await supabase.storage.from("org-logos").upload(path, logoFile);
          if (!uploadErr) {
            const { data: urlData } = supabase.storage.from("org-logos").getPublicUrl(path);
            await supabase.from("organizations").update({ logo_url: urlData.publicUrl }).eq("id", organizationId);
          }
        } catch {
          // Logo upload is optional, continue anyway
          console.warn("Optional logo upload failed");
        }
      }

      router.push("/");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create organization");
      setSubmitting(false);
    }
  };

  /* Merge the reviewed proposal into THIS form.  Only fields the backend
     accepts are ever written, and every one of them stays editable. */
  const applyAiProposal = useCallback(
    (payload: OnboardingApplyPayload) => {
      const { fields } = payload;
      const numbers = ["fiscal_year_end_month", "fiscal_year_start_year"];
      const texts = [
        "name",
        "business_type",
        "legal_name",
        "tax_number",
        "registration_number",
        "core_services",
        "industry_details",
        "base_currency_code",
        "country_code",
        "timezone",
      ];

      setForm((current) => {
        const next = { ...current };
        for (const field of texts) {
          const value = fields[field];
          if (typeof value === "string" && value.trim()) {
            next[field as "name"] = value.trim() as never;
          }
        }
        for (const field of numbers) {
          const value = fields[field];
          if (value !== undefined && value !== null && value !== "") {
            next[field as "fiscal_year_end_month"] = Number(value) as never;
          }
        }
        return next;
      });

      setAiFields(Object.keys(fields));
      setAiSources((payload.responses.field_sources as Record<string, string>) ?? {});
      setAiSummary((payload.responses.summary as string) ?? null);
      setAiGroups(payload.groups);
      setAiResponses(payload.responses);
      const groups = (payload.responses.account_groups as { code: string; label?: string }[]) ?? [];
      setAiGroupLabels(groups.map((g) => ({ code: g.code, label: g.label ?? g.code })));
      setStep(0);
    },
    [],
  );

  const aiFilledCount = aiFields.length;

  const businessTypeLabel =
    businessTypeOptions.find((t) => t.value === form.business_type)?.label ?? form.business_type;

  const showAiBadge = (field: string) => aiFields.includes(field);

  /* Where the assistant got each value from, for the final review list. */
  const sourceLabel = (field: string): string | null => {
    const source = aiSources[field];
    if (!source) return null;
    return (
      {
        inferred: "from your description",
        backend_default: "backend default — change it if it is wrong",
        inferred_from_stated_country: "from the country you mentioned",
      }[source] ?? "set by the assistant"
    );
  };

  return (
    <div className="flex-1 flex flex-col items-center px-4 py-10">
      <div className="w-full max-w-2xl">
        {/* Header */}
        <div className="text-center mb-8">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-brand-teal to-brand-navy flex items-center justify-center mx-auto mb-4 shadow-lg shadow-brand-teal/20">
            <Landmark className="w-7 h-7 text-white" />
          </div>
          <h1 className="text-2xl font-semibold text-text-primary">Set up your organization</h1>
          <p className="text-sm text-text-secondary mt-1">
            This creates your chart of accounts, financial year and document
            numbering - everything needed to start bookkeeping.
          </p>
        </div>

        {/* AI-assisted setup — a first-class entry point, not a hidden option.
            The assistant fills this same form; the user still reviews and
            confirms every value before anything is created. */}
        <div className="relative mb-6 rounded-2xl p-[1.5px] bg-gradient-to-r from-ai-500 via-brand-teal to-brand-navy shadow-lg shadow-ai-500/10">
          <div className="rounded-[calc(1rem-1.5px)] bg-bg-surface px-5 py-4">
            <div className="flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4">
              <div className="flex-1 min-w-0">
                <p className="text-sm font-semibold text-text-primary flex items-center gap-2">
                  <Sparkles className="w-4 h-4 text-ai-600" />
                  Create Organization with AI
                </p>
                <p className="text-xs text-text-secondary mt-1 leading-relaxed">
                  Describe your business in your own words — what you sell, how you sell it,
                  whether you hold stock or own machinery. The assistant fills in this setup
                  and proposes the accounts your business actually needs. You review and
                  adjust everything before it is created.
                </p>
                {aiFilledCount > 0 && (
                  <p className="text-[11px] text-ai-700 mt-1.5 flex items-center flex-wrap">
                    The assistant filled {aiFilledCount} field
                    {aiFilledCount === 1 ? "" : "s"} below — each is marked
                    <AiChip /> and you can change any of them.
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => setWizardOpen(true)}
                className="shrink-0 px-4 py-2.5 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium transition-colors flex items-center justify-center gap-2"
              >
                <Wand2 className="w-4 h-4" />
                {aiFilledCount > 0 ? "Describe it again" : "Start with AI"}
              </button>
            </div>
          </div>
        </div>

        {/* Stepper */}
        <div className="flex items-center justify-between mb-8 px-2">
          {STEPS.map((s, i) => {
            const Icon = s.icon;
            const done = i < step;
            const active = i === step;
            return (
              <div key={s.label} className="flex items-center flex-1 last:flex-none">
                <div className="flex flex-col items-center gap-1.5">
                  <div
                    className={cn(
                      "w-9 h-9 rounded-full flex items-center justify-center border-2 transition-colors",
                      done && "bg-ai-500 border-ai-500 text-white",
                      active && "border-ai-500 text-ai-600 bg-bg-surface",
                      !done && !active && "border-border-default text-text-muted bg-bg-surface"
                    )}
                  >
                    {done ? <Check className="w-4 h-4" /> : <Icon className="w-4 h-4" />}
                  </div>
                  <span
                    className={cn(
                      "text-[10px] font-medium hidden sm:block",
                      active ? "text-text-primary" : "text-text-muted"
                    )}
                  >
                    {s.label}
                  </span>
                </div>
                {i < STEPS.length - 1 && (
                  <div
                    className={cn(
                      "flex-1 h-0.5 mx-2 -mt-5 rounded",
                      i < step ? "bg-ai-500" : "bg-border-default"
                    )}
                  />
                )}
              </div>
            );
          })}
        </div>

        {/* Card */}
        <div className="clay bg-bg-surface rounded-2xl p-6 space-y-5">
          {step === 0 && (
            <>
              <Field
                label="Organization name *"
                hint={`Your workspace slug will be: ${slugPreview || "…"}`}
                aiFilled={showAiBadge("name")}
              >
                <input
                  className={inputCls}
                  value={form.name}
                  onChange={(e) => set("name", e.target.value)}
                  placeholder="e.g. Intelligent Soft Enterprise"
                  autoFocus
                />
              </Field>
              <Field
                label="Business type"
                aiFilled={showAiBadge("business_type")}
                hint={
                  schema
                    ? "This decides which chart of accounts your organization is created with."
                    : undefined
                }
              >
                <select
                  className={inputCls}
                  value={form.business_type}
                  onChange={(e) => set("business_type", e.target.value)}
                >
                  {businessTypeOptions.map((t) => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                  ))}
                </select>
              </Field>
              <p className="text-xs text-text-muted">
                We&apos;ll seed your chart of accounts from the template matching
                this business type — a software house, a trading business and a
                factory each get the accounts their work actually needs.
              </p>
              <div>
                <span className="text-xs font-medium text-text-secondary">Logo (optional)</span>
                <p className="text-[11px] text-text-muted mt-0.5 mb-2">
                  Upload your organization logo to display on reports and the dashboard.
                </p>
                {logoPreview ? (
                  <div className="relative inline-flex items-center gap-3 p-3 rounded-xl border border-border-subtle bg-white">
                    <Image src={logoPreview} alt="Logo preview" width={80} height={32} className="h-8 w-auto object-contain" />
                    <button
                      type="button"
                      onClick={() => { setLogoFile(null); setLogoPreview(null); }}
                      className="text-text-muted hover:text-error-500 transition-colors"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => logoInputRef.current?.click()}
                    className="flex items-center gap-2 px-4 py-2.5 rounded-xl border-2 border-dashed border-border-default hover:border-ai-200 text-xs text-text-secondary hover:text-text-primary transition-colors"
                  >
                    <Upload className="w-4 h-4" />
                    Upload logo
                  </button>
                )}
                <input
                  ref={logoInputRef}
                  type="file"
                  accept="image/png,image/jpeg,image/svg+xml,image/webp"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) {
                      setLogoFile(file);
                      setLogoPreview(URL.createObjectURL(file));
                    }
                  }}
                />
              </div>
            </>
          )}

          {step === 1 && (
            <>
              <Field
                label="Legal name"
                hint="Registered company name, if different"
                aiFilled={showAiBadge("legal_name")}
              >
                <input
                  className={inputCls}
                  value={form.legal_name}
                  onChange={(e) => set("legal_name", e.target.value)}
                  placeholder="e.g. Intelligent Soft (Private) Limited"
                />
              </Field>
              <div className="grid grid-cols-2 gap-4">
                <Field label="Tax number (NTN)" aiFilled={showAiBadge("tax_number")}>
                  <input
                    className={inputCls}
                    value={form.tax_number}
                    onChange={(e) => set("tax_number", e.target.value)}
                  />
                </Field>
                <Field label="Registration number" aiFilled={showAiBadge("registration_number")}>
                  <input
                    className={inputCls}
                    value={form.registration_number}
                    onChange={(e) => set("registration_number", e.target.value)}
                  />
                </Field>
              </div>
              <Field label="Core services" aiFilled={showAiBadge("core_services")}>
                <textarea
                  className={cn(inputCls, "min-h-20 resize-y")}
                  value={form.core_services}
                  onChange={(e) => set("core_services", e.target.value)}
                  placeholder="What does your business sell or provide?"
                />
              </Field>
              <Field label="Industry details" aiFilled={showAiBadge("industry_details")}>
                <input
                  className={inputCls}
                  value={form.industry_details}
                  onChange={(e) => set("industry_details", e.target.value)}
                  placeholder="Optional context for the AI agent"
                />
              </Field>
            </>
          )}

          {step === 2 && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <Field label="Base currency" aiFilled={showAiBadge("base_currency_code")}>
                  <select
                    className={inputCls}
                    value={form.base_currency_code}
                    onChange={(e) => set("base_currency_code", e.target.value)}
                  >
                    {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </Field>
                <Field label="Country code" aiFilled={showAiBadge("country_code")}>
                  <input
                    className={inputCls}
                    maxLength={2}
                    value={form.country_code}
                    onChange={(e) => set("country_code", e.target.value.toUpperCase())}
                  />
                </Field>
              </div>
              <Field label="Timezone" aiFilled={showAiBadge("timezone")}>
                <select
                  className={inputCls}
                  value={form.timezone}
                  onChange={(e) => set("timezone", e.target.value)}
                >
                  {["Asia/Karachi", "Asia/Dubai", "Asia/Riyadh", "Europe/London", "America/New_York", "UTC"].map((tz) => (
                    <option key={tz} value={tz}>{tz}</option>
                  ))}
                </select>
              </Field>
              <Field
                label="Fiscal year ends in"
                aiFilled={showAiBadge("fiscal_year_end_month")}
              >
                <select
                  className={inputCls}
                  value={form.fiscal_year_end_month}
                  onChange={(e) => set("fiscal_year_end_month", Number(e.target.value))}
                >
                  {MONTHS.map((m, i) => (
                    <option key={m} value={i + 1}>{m}</option>
                  ))}
                </select>
              </Field>
              <Field
                label="Starting from year"
                hint={`FY runs ${MONTHS[(form.fiscal_year_end_month % 12)]} ${form.fiscal_year_start_year} \u2013 ${MONTHS[form.fiscal_year_end_month - 1]} ${form.fiscal_year_start_year + 1}`}
                aiFilled={showAiBadge("fiscal_year_start_year")}
              >
                <select
                  className={inputCls}
                  value={form.fiscal_year_start_year}
                  onChange={(e) => set("fiscal_year_start_year", Number(e.target.value))}
                >
                  {Array.from({ length: 5 }, (_, i) => new Date().getFullYear() - 2 + i).map((y) => (
                    <option key={y} value={y}>{y}</option>
                  ))}
                </select>
              </Field>
            </>
          )}

          {step === 3 && (
            <>
              <p className="text-xs text-text-muted">
                Prefixes are used to auto-number documents (INV-00001, QUT-00001…).
                Defaults are fine to keep.
              </p>
              <div className="grid grid-cols-2 gap-4">
                <Field label="Invoice prefix">
                  <input className={inputCls} maxLength={8} value={form.invoice_prefix}
                    onChange={(e) => set("invoice_prefix", e.target.value.toUpperCase())} />
                </Field>
                <Field label="Quotation prefix">
                  <input className={inputCls} maxLength={8} value={form.quotation_prefix}
                    onChange={(e) => set("quotation_prefix", e.target.value.toUpperCase())} />
                </Field>
                <Field label="Purchase bill prefix">
                  <input className={inputCls} maxLength={8} value={form.bill_prefix}
                    onChange={(e) => set("bill_prefix", e.target.value.toUpperCase())} />
                </Field>
                <Field label="Journal prefix">
                  <input className={inputCls} maxLength={8} value={form.journal_prefix}
                    onChange={(e) => set("journal_prefix", e.target.value.toUpperCase())} />
                </Field>
              </div>
            </>
          )}

          {step === 4 && (
            <div className="space-y-3">
              <h3 className="text-sm font-semibold text-text-primary">Review</h3>

              {/* What the assistant understood, and what it deliberately left
                  alone.  Nothing here is created until the user confirms. */}
              {aiSummary && (
                <div className="rounded-xl border border-ai-100 bg-ai-50/60 p-3 space-y-2">
                  <p className="text-xs font-medium text-ai-700 flex items-center gap-1.5">
                    <Sparkles className="w-3.5 h-3.5" />
                    From your description
                  </p>
                  <p className="text-xs text-ai-700 leading-relaxed">{aiSummary}</p>
                  {aiFields.length > 0 && (
                    <p className="text-[11px] text-ai-700/90">
                      Marked <AiChip /> fields below came from what you told the assistant.
                      Check each one — you can change any of them on the earlier steps.
                    </p>
                  )}
                  {aiGroupLabels.length > 0 && (
                    <div>
                      <p className="text-[11px] font-medium text-ai-700">
                        Accounts to be created beyond the base chart:
                      </p>
                      <ul className="mt-1 space-y-0.5">
                        {aiGroupLabels.map((g) => (
                          <li key={g.code} className="text-[11px] text-ai-700/90">
                            • {g.label}
                          </li>
                        ))}
                      </ul>
                      <button
                        type="button"
                        onClick={() => setWizardOpen(true)}
                        className="mt-1 text-[11px] text-ai-700 underline hover:text-ai-600"
                      >
                        Change the accounts… (reopen the assistant to amend them)
                      </button>
                    </div>
                  )}
                </div>
              )}
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2.5 text-sm">
                {([
                  ["Name", form.name, "name"],
                  ["Business type", businessTypeLabel, "business_type"],
                  ["Legal name", form.legal_name || "-", "legal_name"],
                  ["Tax number", form.tax_number || "-", "tax_number"],
                  ["Currency", form.base_currency_code, "base_currency_code"],
                  ["Country", form.country_code, "country_code"],
                  ["Timezone", form.timezone, "timezone"],
                  [
                    "Fiscal year",
                    `${MONTHS[(form.fiscal_year_end_month % 12)]} ${form.fiscal_year_start_year} - ${MONTHS[form.fiscal_year_end_month - 1]} ${form.fiscal_year_start_year + 1}`,
                    "fiscal_year_end_month",
                  ],
                  ["Prefixes", `${form.invoice_prefix} · ${form.quotation_prefix} · ${form.bill_prefix} · ${form.journal_prefix}`, ""],
                ] as [string, string, string][]).map(([label, value, field]) => (
                  <div key={label} className="flex flex-col">
                    <dt className="text-[11px] uppercase tracking-wide text-text-muted">{label}</dt>
                    <dd className="text-text-primary font-medium truncate">{value}</dd>
                    {field && sourceLabel(field) && (
                      <dd className="text-[10px] text-ai-600">{sourceLabel(field)}</dd>
                    )}
                  </div>
                ))}
              </dl>
              <div className="rounded-xl bg-ai-50 border border-ai-100 p-3 text-xs text-ai-700 leading-relaxed">
                Creating now will set you up as <strong>Owner</strong> and generate:
                your organization, a {MONTHS[(form.fiscal_year_end_month % 12)]}{" "}
                {form.fiscal_year_start_year}-{MONTHS[form.fiscal_year_end_month - 1]}{" "}
                {form.fiscal_year_start_year + 1} financial year with 12 monthly
                periods, default document numbering, and a chart of accounts from
                the {businessTypeLabel} template.
              </div>
              {error && (
                <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2">{error}</p>
              )}
            </div>
          )}

          {/* Nav */}
          <div className="flex items-center justify-between pt-2 border-t border-border-subtle">
            <button
              onClick={() => setStep((s) => Math.max(0, s - 1))}
              disabled={step === 0 || submitting}
              className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              Back
            </button>
            {step < STEPS.length - 1 ? (
              <button
                onClick={() => canNext() && setStep((s) => s + 1)}
                disabled={!canNext()}
                className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Continue
              </button>
            ) : (
              <button
                onClick={handleCreate}
                disabled={submitting || form.name.trim().length < 2}
                className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              >
                {submitting && (
                  <span className="w-3.5 h-3.5 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                )}
                {submitting ? "Creating…" : "Create organization"}
              </button>
            )}
          </div>
        </div>

        {/* The AI setup assistant.  It only ever fills this same form (plus
            the reviewed account bundles) — creating the organization remains
            the explicit "Create organization" action above. */}
        <AIOrganizationWizard
          open={wizardOpen}
          onClose={() => setWizardOpen(false)}
          businessType={form.business_type}
          onApply={applyAiProposal}
        />
      </div>
    </div>
  );
}
