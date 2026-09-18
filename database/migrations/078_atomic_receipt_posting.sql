-- 078 — ATOMIC RECEIPT POSTING (receipt header + journal entry + lines + linkage)
-- Mirrors 077 create_payment_atomic: all validation/arithmetic stays in Python;
-- only the writes are transactional, so a receipt can never exist without its
-- journal and a journal line can never reference a foreign organization.

create or replace function public.create_receipt_atomic(
  p_organization_id uuid,
  p_header jsonb,
  p_journal jsonb default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_receipt public.receipts;
  v_customer uuid;
  v_bank uuid;
  v_cash uuid;
  v_entry_id uuid;
  v_line jsonb;
begin
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

  v_bank := (p_header->>'bank_account_id')::uuid;
  if v_bank is not null and to_regclass('public.bank_accounts') is not null
     and not exists (
       select 1 from public.bank_accounts b
       where b.id = v_bank and b.organization_id = p_organization_id
     ) then
    raise exception 'bank account % does not belong to organization %',
      v_bank, p_organization_id using errcode = '42501';
  end if;
  v_cash := (p_header->>'cash_account_id')::uuid;
  if v_cash is not null and to_regclass('public.bank_accounts') is not null
     and not exists (
       select 1 from public.bank_accounts b
       where b.id = v_cash and b.organization_id = p_organization_id
     ) then
    raise exception 'cash account % does not belong to organization %',
      v_cash, p_organization_id using errcode = '42501';
  end if;

  insert into public.receipts (
    organization_id, receipt_date, payment_method,
    bank_account_id, cash_account_id, customer_id,
    amount, currency_code, reference, notes, created_by
  )
  values (
    p_organization_id,
    coalesce((p_header->>'receipt_date')::date, current_date),
    (p_header->>'payment_method')::payment_method_code,
    v_bank, v_cash, v_customer,
    (p_header->>'amount')::numeric,
    (p_header->>'currency_code')::char(3),
    p_header->>'reference',
    p_header->>'notes',
    (p_header->>'created_by')::uuid
  )
  returning * into v_receipt;

  if p_journal is not null
     and jsonb_typeof(coalesce(p_journal->'lines', 'null'::jsonb)) = 'array'
     and jsonb_array_length(p_journal->'lines') > 0
  then
    if (
      select coalesce(sum(coalesce((l->>'debit')::numeric, 0)), 0)
           - coalesce(sum(coalesce((l->>'credit')::numeric, 0)), 0)
      from jsonb_array_elements(p_journal->'lines') l
    ) <> 0 then
      raise exception 'journal for receipt % is not balanced', v_receipt.id
        using errcode = '23514';
    end if;

    insert into public.journal_entries (
      organization_id, transaction_date, description, reference,
      status, source_type, source_id, currency_code, created_by
    )
    values (
      p_organization_id,
      coalesce((p_journal->>'transaction_date')::date, v_receipt.receipt_date),
      coalesce(p_journal->>'description', 'Receipt ' || v_receipt.receipt_number),
      p_journal->>'reference',
      'DRAFT', 'receipt', v_receipt.id,
      (p_journal->>'currency_code')::char(3),
      (p_header->>'created_by')::uuid
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

    update public.receipts
    set journal_entry_id = v_entry_id
    where id = v_receipt.id
    returning * into v_receipt;
  end if;

  return jsonb_build_object(
    'receipt', to_jsonb(v_receipt),
    'journal_entry_id', v_entry_id
  );
end;
$function$;

comment on function public.create_receipt_atomic(uuid, jsonb, jsonb) is
  'Creates a receipt, its journal entry and lines in ONE transaction. Same contract as 077 create_payment_atomic.';

revoke all on function public.create_receipt_atomic(uuid, jsonb, jsonb) from public;
revoke all on function public.create_receipt_atomic(uuid, jsonb, jsonb) from anon;
revoke all on function public.create_receipt_atomic(uuid, jsonb, jsonb) from authenticated;
grant execute on function public.create_receipt_atomic(uuid, jsonb, jsonb) to service_role;
