-- =====================================================================
-- 076 — ATOMIC DOCUMENT POSTING (invoice header + lines + journal)
-- =====================================================================
-- FINDING (CONFIRMED, High). Invoice creation was three independent REST
-- writes with no shared transaction:
--
--   app/services/invoice_service.py
--       inv_repo.create_invoice(...)      -> INSERT invoices
--       item_repo.add_invoice_items(...)  -> INSERT invoice_items (per line)
--   app/tools/__init__.py
--       auto_journal(...)                 -> INSERT journal_entries + lines
--
-- The problem is structural: "invoice -> items -> journal are
-- separate writes ... partial states possible".  A failure between them
-- leaves a DOCUMENT WITHOUT LINES, or an invoice whose journal never existed,
-- with no way to tell afterwards.
--
-- This function posts the whole document in ONE transaction: either the
-- invoice, its lines and (optionally) its journal entry all exist, or nothing
-- does.  All arithmetic and validation stays in Python — only the writes move
-- inside a transaction — so no accounting logic changes.
-- =====================================================================

create or replace function public.post_invoice_atomic(
  p_organization_id uuid,
  p_header jsonb,
  p_items jsonb default '[]'::jsonb,
  p_journal jsonb default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_invoice public.invoices;
  v_item jsonb;
  v_line jsonb;
  v_idx integer := 0;
  v_items jsonb := '[]'::jsonb;
  v_entry_id uuid;
  v_customer uuid;
begin
  -- ---- 0. The party must belong to THIS organization --------------------
  v_customer := (p_header->>'customer_id')::uuid;
  if v_customer is null then
    raise exception 'customer_id is required' using errcode = '22023';
  end if;
  if not exists (
    select 1 from public.customers c
    where c.id = v_customer and c.organization_id = p_organization_id
  ) then
    raise exception 'customer % does not belong to organization %',
      v_customer, p_organization_id using errcode = '42501';
  end if;

  -- ---- 1. Header (invoice_number assigned by the existing trigger) ------
  insert into public.invoices (
    organization_id, customer_id, invoice_date, due_date, currency_code,
    subtotal, discount_total, tax_total, total, payment_terms_days,
    project_id, quotation_id, notes, terms, created_by
  )
  values (
    p_organization_id,
    v_customer,
    coalesce((p_header->>'invoice_date')::date, current_date),
    (p_header->>'due_date')::date,
    (p_header->>'currency_code')::char(3),
    coalesce((p_header->>'subtotal')::numeric, 0),
    coalesce((p_header->>'discount_total')::numeric, 0),
    coalesce((p_header->>'tax_total')::numeric, 0),
    coalesce((p_header->>'total')::numeric, 0),
    coalesce((p_header->>'payment_terms_days')::smallint, 30),
    (p_header->>'project_id')::uuid,
    (p_header->>'quotation_id')::uuid,
    p_header->>'notes',
    p_header->>'terms',
    (p_header->>'created_by')::uuid
  )
  returning * into v_invoice;

  -- ---- 2. Lines (same transaction: never a header without its lines) ----
  for v_item in select * from jsonb_array_elements(coalesce(p_items, '[]'::jsonb))
  loop
    v_idx := v_idx + 1;
    insert into public.invoice_items (
      organization_id, invoice_id, line_number, description, product_id,
      service_id, project_id, quantity, unit_price, discount_amount,
      tax_rate_id, tax_amount, line_total, revenue_account_id
    )
    values (
      p_organization_id,
      v_invoice.id,
      v_idx,
      coalesce(v_item->>'description', ''),
      (v_item->>'product_id')::uuid,
      (v_item->>'service_id')::uuid,
      (v_item->>'project_id')::uuid,
      coalesce((v_item->>'quantity')::numeric, 1),
      coalesce((v_item->>'unit_price')::numeric, 0),
      coalesce((v_item->>'discount_amount')::numeric, 0),
      (v_item->>'tax_rate_id')::uuid,
      coalesce((v_item->>'tax_amount')::numeric, 0),
      coalesce((v_item->>'line_total')::numeric, 0),
      (v_item->>'revenue_account_id')::uuid
    )
    returning to_jsonb(invoice_items.*) into v_line;
    v_items := v_items || jsonb_build_array(v_line);
  end loop;

  -- ---- 3. Journal (SAME transaction) ------------------------------------
  if p_journal is not null
     and jsonb_typeof(coalesce(p_journal->'lines', 'null'::jsonb)) = 'array'
     and jsonb_array_length(p_journal->'lines') > 0
  then
    -- Refuse to write an unbalanced entry into the transaction at all.
    if (
      select coalesce(sum(coalesce((l->>'debit')::numeric, 0)), 0)
           - coalesce(sum(coalesce((l->>'credit')::numeric, 0)), 0)
      from jsonb_array_elements(p_journal->'lines') l
    ) <> 0 then
      raise exception 'journal for invoice % is not balanced', v_invoice.id
        using errcode = '23514';
    end if;

    insert into public.journal_entries (
      organization_id, transaction_date, description, source_type, source_id,
      currency_code, status, created_by
    )
    values (
      p_organization_id,
      coalesce((p_journal->>'transaction_date')::date, v_invoice.invoice_date),
      coalesce(p_journal->>'description',
               'Invoice ' || v_invoice.invoice_number),
      'invoice',
      v_invoice.id,
      (p_journal->>'currency_code')::char(3),
      'DRAFT',
      (p_header->>'created_by')::uuid
    )
    returning id into v_entry_id;

    for v_line in select * from jsonb_array_elements(p_journal->'lines')
    loop
      insert into public.journal_lines (
        organization_id, entry_id, account_id, description,
        debit, credit, customer_id, project_id
      )
      values (
        p_organization_id,
        v_entry_id,
        (v_line->>'account_id')::uuid,
        v_line->>'description',
        coalesce((v_line->>'debit')::numeric, 0),
        coalesce((v_line->>'credit')::numeric, 0),
        (v_line->>'customer_id')::uuid,
        (v_line->>'project_id')::uuid
      );
    end loop;

    update public.invoices
    set journal_entry_id = v_entry_id
    where id = v_invoice.id
    returning * into v_invoice;
  end if;

  return jsonb_build_object(
    'invoice', to_jsonb(v_invoice),
    'items', v_items,
    'item_count', v_idx,
    'journal_entry_id', v_entry_id
  );
end;
$function$;

comment on function public.post_invoice_atomic(uuid, jsonb, jsonb, jsonb) is
  'Creates an invoice, its lines and (optionally) its journal entry in ONE '
  'transaction. All arithmetic/validation stays in Python; only the writes '
  'are transactional, so a document can never exist without its lines.';

revoke all on function public.post_invoice_atomic(uuid, jsonb, jsonb, jsonb) from public;
revoke all on function public.post_invoice_atomic(uuid, jsonb, jsonb, jsonb) from anon;
revoke all on function public.post_invoice_atomic(uuid, jsonb, jsonb, jsonb) from authenticated;
grant execute on function public.post_invoice_atomic(uuid, jsonb, jsonb, jsonb) to service_role;