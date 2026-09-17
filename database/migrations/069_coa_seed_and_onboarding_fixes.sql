-- =====================================================================
-- Migration 069: chart-of-accounts seeding fixes
-- =====================================================================
-- Two defects were found while verifying the end-to-end onboarding flow
-- introduced by 067.  Both are corrected here (the live database recorded
-- them as the migrations "fix_seed_org_chart_of_accounts_parent_link" and
-- "apply_organization_onboarding_rank_cast").
--
-- 1. seed_org_chart_of_accounts() — parent linking
--    PostgreSQL rejects
--        UPDATE accounts a SET ... FROM x JOIN accounts p ON ... a ...
--    ("invalid reference to FROM-clause entry for table a"): a JOIN's ON
--    clause may not reference the UPDATE target.  Every organization
--    creation aborted here, so no organization was ever left with a
--    partially linked chart and no data repair is required.
--
-- 2. apply_organization_onboarding() — permission check
--    public.has_org_role(target_org uuid, min_rank smallint) resolves
--    strictly: the integer literal 2 does NOT match a smallint parameter,
--    so the call failed with "function public.has_org_role(uuid, integer)
--    does not exist".  The cast is now explicit.
--
-- Only these two functions change; grants are re-stated so the least
-- privilege posture from 067 is preserved.
-- =====================================================================

create or replace function public.seed_org_chart_of_accounts(
  p_organization_id uuid,
  p_business_type   business_type_code,
  p_groups          text[]  default '{}',
  p_onboarding_id   uuid    default null,
  p_rationale       text    default null
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
DECLARE
  v_template_id   uuid;
  v_template_name text;
  v_chart_id      uuid;
  v_groups        text[] := coalesce(p_groups, '{}');
  v_inserted      integer := 0;
BEGIN
  SELECT t.id, t.name INTO v_template_id, v_template_name
  FROM public.account_templates t
  WHERE t.code = public.account_template_for_business_type(p_business_type)
  LIMIT 1;

  IF v_template_id IS NULL THEN
    RETURN 0;
  END IF;

  -- The organization's default chart (the banking screen resolves this row
  -- before it will create a bank GL account).
  SELECT c.id INTO v_chart_id
  FROM public.chart_of_accounts c
  WHERE c.organization_id = p_organization_id
  ORDER BY c.is_default DESC, c.created_at
  LIMIT 1;

  IF v_chart_id IS NULL THEN
    INSERT INTO public.chart_of_accounts (organization_id, name, is_default)
    VALUES (p_organization_id, 'Standard', true)
    RETURNING id INTO v_chart_id;
  END IF;

  INSERT INTO public.accounts (
    organization_id, chart_of_accounts_id, code, name, account_type,
    normal_balance, account_category_id, is_system, is_active, description)
  SELECT p_organization_id, v_chart_id, ti.code, ti.name, ti.account_type,
         CASE WHEN ti.account_type IN ('ASSET','EXPENSE')
              THEN 'DEBIT'::public.normal_balance_code
              ELSE 'CREDIT'::public.normal_balance_code END,
         ti.account_category_id, false, true,
         'Seeded from ' || v_template_name
  FROM public.account_template_items ti
  WHERE ti.template_id = v_template_id
    AND (
      NOT ti.is_optional
      OR EXISTS (
        SELECT 1 FROM public.account_catalog_group_items gi
        WHERE gi.code = ti.code AND gi.group_code = ANY(v_groups)
      )
    )
    AND NOT EXISTS (
      SELECT 1 FROM public.accounts a
      WHERE a.organization_id = p_organization_id AND a.code = ti.code
    );

  GET DIAGNOSTICS v_inserted = ROW_COUNT;

  -- Parent/child hierarchy.  The parent is matched from a comma-joined
  -- FROM list because a JOIN's ON clause may not reference the UPDATE
  -- target (the defect this migration fixes).
  UPDATE public.accounts a
  SET parent_account_id = p.id,
      updated_at = now()
  FROM public.account_template_items ti, public.accounts p
  WHERE p.organization_id = a.organization_id
    AND p.code = ti.suggested_parent_code
    AND a.organization_id = p_organization_id
    AND ti.template_id = v_template_id
    AND ti.code = a.code
    AND ti.suggested_parent_code IS NOT NULL
    AND a.parent_account_id IS DISTINCT FROM p.id;

  -- Audit trail for the bundles the user approved.
  IF p_onboarding_id IS NOT NULL AND array_length(v_groups, 1) IS NOT NULL THEN
    INSERT INTO public.business_account_recommendations (
      organization_id, onboarding_id, template_id, account_name,
      account_type, rationale, is_accepted)
    SELECT p_organization_id, p_onboarding_id, v_template_id, a.name, a.account_type,
           'Approved with the ' || coalesce(g.label, gi.group_code) ||
           ' bundle during onboarding' ||
           coalesce(' — ' || nullif(trim(p_rationale), ''), ''),
           true
    FROM public.account_catalog_group_items gi
    JOIN public.account_catalog_groups g ON g.code = gi.group_code
    JOIN public.accounts a
      ON a.organization_id = p_organization_id AND a.code = gi.code
    WHERE gi.group_code = ANY(v_groups)
      AND NOT EXISTS (
        SELECT 1 FROM public.business_account_recommendations r
        WHERE r.onboarding_id = p_onboarding_id AND r.account_name = a.name
      );
  END IF;

  UPDATE public.organization_onboarding
  SET recommended_accounts_generated = true,
      updated_at = now()
  WHERE organization_id = p_organization_id;

  RETURN v_inserted;
END;
$$;

revoke all on function public.seed_org_chart_of_accounts(uuid, business_type_code, text[], uuid, text)
  from public, anon, authenticated;
grant execute on function public.seed_org_chart_of_accounts(uuid, business_type_code, text[], uuid, text)
  to service_role, postgres;

create or replace function public.apply_organization_onboarding(
  p_organization_id uuid,
  p_groups          text[]  default null,
  p_responses       jsonb   default null,
  p_business_type   business_type_code default null,
  p_rationale       text    default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
DECLARE
  v_current_type  business_type_code;
  v_business_type business_type_code;
  v_requested     text[];
  v_applied       text[];
  v_onboarding_id uuid;
  v_inserted      integer := 0;
BEGIN
  SELECT o.business_type INTO v_current_type
  FROM public.organizations o
  WHERE o.id = p_organization_id;

  IF v_current_type IS NULL THEN
    RAISE EXCEPTION 'Organization not found';
  END IF;

  -- has_org_role takes a smallint: the cast is required (see header).
  IF NOT public.has_org_role(p_organization_id, 2::smallint) THEN
    RAISE EXCEPTION 'Only an owner or administrator may apply onboarding account changes';
  END IF;

  v_business_type := coalesce(p_business_type, v_current_type);

  IF v_business_type <> v_current_type THEN
    -- Correcting the business type changes which chart is used, so it is
    -- only safe while the books are still empty.
    IF EXISTS (
      SELECT 1 FROM public.journal_entries j
      WHERE j.organization_id = p_organization_id AND j.status = 'POSTED'
    ) THEN
      RAISE EXCEPTION 'The business type cannot be changed after entries have been posted';
    END IF;

    UPDATE public.organizations
    SET business_type = v_business_type, updated_at = now()
    WHERE id = p_organization_id;

    UPDATE public.organization_onboarding
    SET business_type = v_business_type, updated_at = now()
    WHERE organization_id = p_organization_id;
  END IF;

  -- p_groups = NULL means "accept the recommendations for this business
  -- type"; an explicit (possibly empty) list is taken as the decision.
  IF p_groups IS NULL THEN
    SELECT coalesce(array_agg(g.group_code ORDER BY g.group_code), '{}')
    INTO v_requested
    FROM public.account_catalog_group_business_types g
    WHERE g.business_type = v_business_type
      AND g.is_default;
  ELSE
    v_requested := p_groups;
  END IF;

  -- Ignore any bundle that is not actually offered for this business type
  -- (defence in depth: the client cannot create arbitrary accounts).
  SELECT coalesce(array_agg(g.group_code ORDER BY g.group_code), '{}')
  INTO v_applied
  FROM public.account_catalog_group_business_types g
  WHERE g.business_type = v_business_type
    AND g.group_code = ANY(v_requested);

  SELECT o.id INTO v_onboarding_id
  FROM public.organization_onboarding o
  WHERE o.organization_id = p_organization_id;

  v_inserted := public.seed_org_chart_of_accounts(
    p_organization_id, v_business_type, v_applied, v_onboarding_id, p_rationale);

  IF p_responses IS NOT NULL THEN
    UPDATE public.organization_onboarding
    SET responses = p_responses, updated_at = now()
    WHERE organization_id = p_organization_id;
  END IF;

  RETURN jsonb_build_object(
    'accounts_created', v_inserted,
    'groups_applied', to_jsonb(v_applied),
    'business_type', v_business_type::text
  );
END;
$$;

revoke all on function public.apply_organization_onboarding(uuid, text[], jsonb, business_type_code, text)
  from public, anon;
grant execute on function public.apply_organization_onboarding(uuid, text[], jsonb, business_type_code, text)
  to service_role, postgres, authenticated;

comment on function public.apply_organization_onboarding(uuid, text[], jsonb, business_type_code, text) is
  'Applies the owner-approved onboarding decisions after create_organization(): optional account bundles plus the AI analysis stored on organization_onboarding.responses. p_groups = NULL accepts the recommended bundles for the business type.';