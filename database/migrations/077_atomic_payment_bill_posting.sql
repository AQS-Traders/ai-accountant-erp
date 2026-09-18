-- 077 — ATOMIC PAYMENT / RECEIPT / PURCHASE-BILL POSTING
-- Same contract as 076: document + lines + linked DRAFT journal entry commit
-- in ONE transaction; all arithmetic/validation stays in Python; the app's
-- normal validate/post flow then posts the linked entry.
-- SECURITY DEFINER, fixed search_path, service_role only.

-- payments covers receipts too (direction = 'IN').
create or replace function public.create_payment_atomic(
  p_organization_id uuid,
  p_payment jsonb,
  p_journal jsonb default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_payment public.payments;
  v_supplier uuid;
  v_bank uuid;
  v_cash uuid;
  v_entry_id uuid;
  v_line jsonb;
begin
  v_supplier := (p_payment->>'supplier_id')::uuid;
  if v_supplier is not null and not exists (
    select 1 from public.suppliers s
    where s.id = v_supplier and s.organization_id = p_organization_id
  ) then
    raise exception 'supplier % does not belong to organization %',
      v_supplier, p_organization_id using errcode = '42501';
  end if;

  v_bank := (p_payment->>'bank_account_id')::uuid;
  if v_bank is not null and to_regclass('public.bank_accounts') is not null
     and not exists (
       select 1 from public.bank_accounts b
       where b.id = v_bank and b.organization_id = p_organization_id
     ) then
    raise exception 'bank account % does not belong to organization %',
      v_bank, p_organization_id using errcode = '42501';
  end if;
  v_cash := (p_payment->>'cash_account_id')::uuid;
  if v_cash is not null and to_regclass('public.bank_accounts') is not null
     and not exists (
       select 1 from public.bank_accounts b
       where b.id = v_cash and b.organization_id = p_organization_id
     ) then
    raise exception 'cash account % does not belong to organization %',
      v_cash, p_organization_id using errcode = '42501';
  end if;

  insert into public.payments (
    organization_id, payment_date, direction, payment_method,
    bank_account_id, cash_account_id, supplier_id, expense_id,
    amount, currency_code, reference, notes, is_transfer, created_by
  )
  values (
    p_organization_id,
    coalesce((p_payment->>'payment_date')::date, current_date),
    (p_payment->>'direction')::payment_direction,
    (p_payment->>'payment_method')::payment_method_code,
    v_bank, v_cash, v_supplier,
    (p_payment->>'expense_id')::uuid,
    (p_payment->>'amount')::numeric,
    (p_payment->>'currency_code')::char(3),
    p_payment->>'reference',
    p_payment->>'notes',
    coalesce((p_payment->>'is_transfer')::boolean, false),
    (p_payment->>'created_by')::uuid
  )
  returning * into v_payment;

  if p_journal is not null
     and jsonb_typeof(coalesce(p_journal->'lines', 'null'::jsonb)) = 'array'
     and jsonb_array_length(p_journal->'lines') > 0
  then
    if (
      select coalesce(sum(coalesce((l->>'debit')::numeric, 0)), 0)
           - coalesce(sum(coalesce((l->>'credit')::numeric, 0)), 0)
      from jsonb_array_elements(p_journal->'lines') l
    ) <> 0 then
      raise exception 'journal for payment % is not balanced', v_payment.id
        using errcode = '23514';
    end if;

    insert into public.journal_entries (
      organization_id, transaction_date, description, reference,
      status, source_type, source_id, currency_code, created_by
    )
    values (
      p_organization_id,
      coalesce((p_journal->>'transaction_date')::date, v_payment.payment_date),
      coalesce(p_journal->>'description', 'Payment ' || v_payment.payment_number),
      p_journal->>'reference',
      'DRAFT', 'payment', v_payment.id,
      (p_journal->>'currency_code')::char(3),
      (p_payment->>'created_by')::uuid
    )
    returning id into v_entry_id;

    for v_line in select * from jsonb_array_elements(p_journal->'lines')
    loop
      insert into public.journal_lines (
        organization_id, entry_id, account_id, description,
        debit, credit, supplier_id, customer_id, project_id
      )
      values (
        p_organization_id, v_entry_id,
        (v_line->>'account_id')::uuid,
        v_line->>'description',
        coalesce((v_line->>'debit')::numeric, 0),
        coalesce((v_line->>'credit')::numeric, 0),
        (v_line->>'supplier_id')::uuid,
        (v_line->>'customer_id')::uuid,
        (v_line->>'project_id')::uuid
      );
    end loop;

    update public.payments
    set journal_entry_id = v_entry_id
    where id = v_payment.id
    returning * into v_payment;
  end if;

  return jsonb_build_object(
    'payment', to_jsonb(v_payment),
    'journal_entry_id', v_entry_id
  );
end;
$function$;

-- --------------------------------------------------------- purchase bills --
create or replace function public.create_purchase_bill_atomic(
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
  v_bill public.purchase_bills;
  v_item jsonb;
  v_line jsonb;
  v_idx integer := 0;
  v_items jsonb := '[]'::jsonb;
  v_entry_id uuid;
  v_supplier uuid;
begin
  v_supplier := (p_header->>'supplier_id')::uuid;
  if v_supplier is null then
    raise exception 'supplier_id is required' using errcode = '22023';
  end if;
  if not exists (
    select 1 from public.suppliers s
    where s.id = v_supplier and s.organization_id = p_organization_id
  ) then
    raise exception 'supplier % does not belong to organization %',
      v_supplier, p_organization_id using errcode = '42501';
  end if;

  insert into public.purchase_bills (
    organization_id, supplier_id, bill_date, due_date, payment_terms_days,
    supplier_invoice_ref, currency_code, subtotal, discount_total,
    tax_total, total, notes, created_by
  )
  values (
    p_organization_id, v_supplier,
    coalesce((p_header->>'bill_date')::date, current_date),
    (p_header->>'due_date')::date,
    coalesce((p_header->>'payment_terms_days')::smallint, 30),
    p_header->>'supplier_invoice_ref',
    (p_header->>'currency_code')::char(3),
    coalesce((p_header->>'subtotal')::numeric, 0),
    coalesce((p_header->>'discount_total')::numeric, 0),
    coalesce((p_header->>'tax_total')::numeric, 0),
    coalesce((p_header->>'total')::numeric, 0),
    p_header->>'notes',
    (p_header->>'created_by')::uuid
  )
  returning * into v_bill;

  for v_item in select * from jsonb_array_elements(coalesce(p_items, '[]'::jsonb))
  loop
    v_idx := v_idx + 1;
    insert into public.purchase_bill_items (
      organization_id, bill_id, line_number, description, product_id,
      expense_account_id, project_id, fixed_asset_id, quantity, unit_price,
      discount_amount, tax_rate_id, tax_amount, line_total
    )
    values (
      p_organization_id, v_bill.id, v_idx,
      coalesce(v_item->>'description', ''),
      (v_item->>'product_id')::uuid,
      (v_item->>'expense_account_id')::uuid,
      (v_item->>'project_id')::uuid,
      (v_item->>'fixed_asset_id')::uuid,
      coalesce((v_item->>'quantity')::numeric, 1),
      coalesce((v_item->>'unit_price')::numeric, 0),
      coalesce((v_item->>'discount_amount')::numeric, 0),
      (v_item->>'tax_rate_id')::uuid,
      coalesce((v_item->>'tax_amount')::numeric, 0),
      coalesce((v_item->>'line_total')::numeric, 0)
    )
    returning to_jsonb(purchase_bill_items.*) into v_line;
    v_items := v_items || jsonb_build_array(v_line);
  end loop;

  if p_journal is not null
     and jsonb_typeof(coalesce(p_journal->'lines', 'null'::jsonb)) = 'array'
     and jsonb_array_length(p_journal->'lines') > 0
  then
    if (
      select coalesce(sum(coalesce((l->>'debit')::numeric, 0)), 0)
           - coalesce(sum(coalesce((l->>'credit')::numeric, 0)), 0)
      from jsonb_array_elements(p_journal->'lines') l
    ) <> 0 then
      raise exception 'journal for bill % is not balanced', v_bill.id
        using errcode = '23514';
    end if;

    insert into public.journal_entries (
      organization_id, transaction_date, description, reference,
      status, source_type, source_id, currency_code, created_by
    )
    values (
      p_organization_id,
      coalesce((p_journal->>'transaction_date')::date, v_bill.bill_date),
      coalesce(p_journal->>'description', 'Bill ' || v_bill.bill_number),
      p_journal->>'reference',
      'DRAFT', 'purchase_bill', v_bill.id,
      (p_journal->>'currency_code')::char(3),
      (p_header->>'created_by')::uuid
    )
    returning id into v_entry_id;

    for v_line in select * from jsonb_array_elements(p_journal->'lines')
    loop
      insert into public.journal_lines (
        organization_id, entry_id, account_id, description,
        debit, credit, supplier_id, project_id
      )
      values (
        p_organization_id, v_entry_id,
        (v_line->>'account_id')::uuid,
        v_line->>'description',
        coalesce((v_line->>'debit')::numeric, 0),
        coalesce((v_line->>'credit')::numeric, 0),
        (v_line->>'supplier_id')::uuid,
        (v_line->>'project_id')::uuid
      );
    end loop;

    update public.purchase_bills
    set journal_entry_id = v_entry_id
    where id = v_bill.id
    returning * into v_bill;
  end if;

  return jsonb_build_object(
    'bill', to_jsonb(v_bill),
    'items', v_items,
    'item_count', v_idx,
    'journal_entry_id', v_entry_id
  );
end;
$function$;

revoke all on function public.create_payment_atomic(uuid, jsonb, jsonb) from public;
revoke all on function public.create_payment_atomic(uuid, jsonb, jsonb) from anon;
revoke all on function public.create_payment_atomic(uuid, jsonb, jsonb) from authenticated;
grant execute on function public.create_payment_atomic(uuid, jsonb, jsonb) to service_role;
revoke all on function public.create_purchase_bill_atomic(uuid, jsonb, jsonb, jsonb) from public;
revoke all on function public.create_purchase_bill_atomic(uuid, jsonb, jsonb, jsonb) from anon;
revoke all on function public.create_purchase_bill_atomic(uuid, jsonb, jsonb, jsonb) from authenticated;
grant execute on function public.create_purchase_bill_atomic(uuid, jsonb, jsonb, jsonb) to service_role;
