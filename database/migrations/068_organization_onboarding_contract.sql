-- =====================================================================
-- Migration 068: organization_onboarding_contract()
-- =====================================================================
-- The AI onboarding assistant must populate the REAL backend fields and
-- must not hard-code which questions to ask.  This function introspects
-- the actual creation API and schema at runtime and returns the contract:
--
--   * the create_organization / apply_organization_onboarding signatures
--     (argument names, types, defaults) — so the assistant can only ever
--     populate arguments the backend actually accepts;
--   * the NOT NULL / defaulted columns of `organizations`, and the check
--     constraints that apply to them;
--   * the validation messages raised by the creation function itself
--     (extracted from its source, case-insensitively), i.e. the real
--     validation rules;
--   * every business type with the template it resolves to, and which
--     optional account bundles may be offered for it (with the bundles
--     that are recommended by default).
--
-- It is schema + reference data only: no tenant data is exposed, which is
-- why signed-in users may read it (the onboarding UI shows the same real
-- choices the backend will accept).
-- =====================================================================
create or replace function public.organization_onboarding_contract()
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
DECLARE
  v_signature      text;
  v_apply_sig      text;
  v_def            text;
  v_apply_def      text;
  v_args           jsonb;
  v_create_rules   jsonb;
  v_apply_rules    jsonb;
  v_columns        jsonb;
  v_not_null       jsonb;
  v_checks         jsonb;
  v_defaults       jsonb;
  v_required       jsonb;
  v_business_types jsonb;
  v_groups         jsonb;
  v_groups_by_bt   jsonb;
  v_currencies     jsonb;
BEGIN
  SELECT pg_get_function_arguments(p.oid), pg_get_functiondef(p.oid)
    INTO v_signature, v_def
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public' AND p.proname = 'create_organization'
  LIMIT 1;

  SELECT pg_get_function_arguments(p.oid), pg_get_functiondef(p.oid)
    INTO v_apply_sig, v_apply_def
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public' AND p.proname = 'apply_organization_onboarding'
  LIMIT 1;

  -- Argument list (name, type, default).  The split is on ", " which is
  -- safe for these signatures; the raw signature is returned alongside so
  -- nothing is hidden by the parsing.
  SELECT jsonb_agg(jsonb_build_object(
           'argument', a.arg,
           'has_default', a.arg ILIKE '%DEFAULT%',
           'default', nullif(trim(regexp_replace(
                        regexp_replace(split_part(a.arg, 'DEFAULT ', 2), '::[a-z_ ]+$', '', 'i'),
                        '^''|''$', '', 'g')), ''))
         ORDER BY a.ord)
    INTO v_args
  FROM (
    SELECT arg, ordinality AS ord
    FROM unnest(string_to_array(coalesce(v_signature, ''), ', ')) WITH ORDINALITY AS u(arg, ordinality)
  ) a;

  -- The creation function's own validation rules.  NOTE: the pattern must
  -- be case-insensitive (the function writes RAISE EXCEPTION in upper
  -- case); without the 'i' flag this list comes back empty.
  SELECT jsonb_agg(m[1]) INTO v_create_rules
  FROM regexp_matches(coalesce(v_def, ''), 'raise exception ''([^'']+)''', 'gi') AS m;

  SELECT jsonb_agg(m[1]) INTO v_apply_rules
  FROM regexp_matches(coalesce(v_apply_def, ''), 'raise exception ''([^'']+)''', 'gi') AS m;

  -- Column metadata for organizations.
  SELECT jsonb_agg(jsonb_build_object(
           'name', c.column_name,
           'type', c.data_type,
           'not_null', c.is_nullable = 'NO',
           'has_default', c.column_default IS NOT NULL,
           'default', c.column_default) ORDER BY c.ordinal_position),
         jsonb_agg(c.column_name ORDER BY c.ordinal_position)
           FILTER (WHERE c.is_nullable = 'NO' AND c.column_default IS NULL)
    INTO v_columns, v_not_null
  FROM information_schema.columns c
  WHERE c.table_schema = 'public' AND c.table_name = 'organizations';

  -- The check constraints the organization row must satisfy.
  SELECT jsonb_agg(pg_get_constraintdef(con.oid)) INTO v_checks
  FROM pg_constraint con
  JOIN pg_class cl ON cl.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = cl.relnamespace
  WHERE n.nspname = 'public' AND cl.relname = 'organizations' AND con.contype = 'c';

  -- Defaults the backend already applies, keyed by column name.
  SELECT jsonb_object_agg(
           regexp_replace(split_part(a.arg, ' ', 1), '^p_', ''),
           nullif(trim(regexp_replace(
             regexp_replace(split_part(a.arg, 'DEFAULT ', 2), '::[a-z_ ]+$', '', 'i'),
             '^''|''$', '', 'g')), '')
         )
    INTO v_defaults
  FROM (
    SELECT arg FROM unnest(string_to_array(coalesce(v_signature, ''), ', ')) AS u(arg)
  ) a
  WHERE a.arg ILIKE '%DEFAULT%'
    AND split_part(a.arg, 'DEFAULT ', 2) NOT ILIKE 'NULL%';

  -- Fields the user genuinely MUST supply: a create_organization argument
  -- without a default whose column is NOT NULL and not auto-generated.
  SELECT coalesce(jsonb_agg(DISTINCT regexp_replace(split_part(a.arg, ' ', 1), '^p_', '')), '[]'::jsonb)
    INTO v_required
  FROM (
    SELECT arg FROM unnest(string_to_array(coalesce(v_signature, ''), ', ')) AS u(arg)
  ) a
  WHERE a.arg NOT ILIKE '%DEFAULT%'
    AND regexp_replace(split_part(a.arg, ' ', 1), '^p_', '') IN (
      SELECT jsonb_array_elements_text(coalesce(v_not_null, '[]'::jsonb))
    );

  -- Earlier verification found that a case-SENSITIVE pattern silently
  -- returned no rules; keep this list non-empty so a schema change that
  -- removes every validation is noticed rather than silently accepted.
  IF coalesce(jsonb_array_length(v_create_rules), 0) = 0 THEN
    RAISE EXCEPTION 'create_organization exposes no validation rules — contract introspection is broken';
  END IF;

  -- Every business type and the chart it resolves to.
  SELECT jsonb_agg(jsonb_build_object(
           'value', o.business_type::text,
           'template_code', o.template_code,
           'template_name', o.template_name,
           'base_account_count', o.base_account_count,
           'optional_group_count', o.optional_group_count)
         ORDER BY o.business_type::text)
    INTO v_business_types
  FROM public.account_template_options() o;

  -- Optional account bundles (reference data).
  SELECT jsonb_agg(jsonb_build_object(
           'code', g.code, 'label', g.label, 'description', g.description)
         ORDER BY g.sort_order, g.code)
    INTO v_groups
  FROM public.account_catalog_groups g
  WHERE g.is_active;

  -- Which bundles may be offered for which business type, and which of
  -- them are recommended from the business type alone.
  SELECT jsonb_object_agg(bt.business_type::text, bt.bundles) INTO v_groups_by_bt
  FROM (
    SELECT gbt.business_type,
           jsonb_agg(jsonb_build_object('code', gbt.group_code, 'recommended', gbt.is_default)
                     ORDER BY gbt.group_code) AS bundles
    FROM public.account_catalog_group_business_types gbt
    JOIN public.account_catalog_groups g ON g.code = gbt.group_code AND g.is_active
    GROUP BY gbt.business_type
  ) bt;

  SELECT jsonb_agg(jsonb_build_object(
           'code', c.code, 'name', c.name, 'symbol', c.symbol,
           'decimal_places', c.decimal_places)
         ORDER BY c.code)
    INTO v_currencies
  FROM public.currencies c
  WHERE c.is_active;

  RETURN jsonb_build_object(
    'rpc', jsonb_build_object(
      'name', 'create_organization',
      'signature', v_signature,
      'arguments', coalesce(v_args, '[]'::jsonb),
      'validation_rules', coalesce(v_create_rules, '[]'::jsonb)),
    'rpc_follow_up', jsonb_build_object(
      'name', 'apply_organization_onboarding',
      'signature', v_apply_sig,
      'validation_rules', coalesce(v_apply_rules, '[]'::jsonb)),
    'organization_columns', coalesce(v_columns, '[]'::jsonb),
    'organization_checks', coalesce(v_checks, '[]'::jsonb),
    'backend_defaults', coalesce(v_defaults, '{}'::jsonb),
    'required_fields', coalesce(v_required, '[]'::jsonb),
    'business_types', coalesce(v_business_types, '[]'::jsonb),
    'currencies', coalesce(v_currencies, '[]'::jsonb),
    'account_bundles', coalesce(v_groups, '[]'::jsonb),
    'bundles_by_business_type', coalesce(v_groups_by_bt, '{}'::jsonb),
    'fiscal_year_end_month', jsonb_build_object('min', 1, 'max', 12));
END;
$$;

revoke all on function public.organization_onboarding_contract() from public, anon;
grant execute on function public.organization_onboarding_contract() to service_role, postgres, authenticated;

comment on function public.organization_onboarding_contract() is
  'Runtime introspection of the organization-creation API + schema for the AI onboarding assistant: RPC signature/defaults, validation rules, required fields, business types, templates, account bundles. Schema and reference data only — no tenant data.';