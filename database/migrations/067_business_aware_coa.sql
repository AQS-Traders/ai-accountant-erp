-- =====================================================================
-- Migration 067: business_aware_coa
-- =====================================================================
-- WHY THIS EXISTS
-- ---------------
-- 1. GRANULARITY. Every organization was seeded from one of seven
--    service-industry templates whose asset side ended at a single
--    "Computer Equipment" account.  There was no way to represent
--    Furniture & Fixtures, Office Equipment, Vehicles, Machinery,
--    Buildings or Leasehold Improvements, so fixed-asset reporting had
--    nothing to report on.
-- 2. HIERARCHY. `accounts.parent_account_id` has always existed but was
--    never populated: every seeded account was flat (verified live:
--    0 of 333 accounts had a parent).  Fixed-asset classes therefore had
--    no structure to group by.
-- 3. BUSINESS AWARENESS. Only 7 of the 16 `business_type_code` values
--    had a template.  Onboarding a trading, manufacturing, e-commerce,
--    construction, healthcare or education business silently fell back
--    to GENERAL_SERVICE (verified live: an E_COMMERCE and a
--    MANUFACTURING organization both received the identical service
--    chart).
-- 4. A LATENT DEFECT. `create_organization` never created the
--    organization's `chart_of_accounts` row, so every seeded account had
--    `chart_of_accounts_id = NULL`.  The banking screen resolves that row
--    before it will create a bank GL account and therefore refused to
--    work for every organization created through onboarding.
--
-- HOW IT WORKS
-- ------------
-- `account_catalog` is the SINGLE source of truth for the chart.  A row
-- is either unconditional, restricted to specific business types, or
-- part of an optional group.  `account_catalog_groups` describes those
-- optional bundles (fixed-asset classes, inventory, cost of sales,
-- borrowings, ...) and `account_catalog_group_business_types` records
-- which business types may be offered a bundle and whether it is
-- recommended by default.
--
-- `rebuild_account_template_items()` materialises the catalog into the
-- pre-existing `account_template_items` reference table (per template),
-- so the runtime seeding path is unchanged: it still reads
-- `account_template_items`.  Seeding now also applies
-- `suggested_parent_code` and links `chart_of_accounts`.
--
-- The AI onboarding assistant reads exactly this catalog
-- (`account_template_catalog`) so it can only ever propose real,
-- backend-compatible accounts — it can never invent one.
--
-- EXISTING ORGANIZATIONS ARE NOT TOUCHED.  Accounts already created keep
-- their ids, codes, names and (null) parents: journal lines reference
-- accounts by id and history must stay intact.  Only the reference data
-- used for FUTURE organization creation changes, plus the optional,
-- explicit `apply_organization_onboarding` RPC an owner may call.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. Account category for intangibles (requirement: Intangible Assets
--    must be representable; no existing category covered it).
-- ---------------------------------------------------------------------
insert into public.account_categories (code, name, description, account_type_id)
select 'INTANGIBLE', 'Intangible Assets', 'Non-monetary assets without physical substance', t.id
from public.account_types t
where t.code = 'ASSET'
on conflict (code) do nothing;

-- ---------------------------------------------------------------------
-- 1. Optional account bundles
-- ---------------------------------------------------------------------
create table if not exists public.account_catalog_groups (
  code          text primary key,
  label         text not null,
  description   text,
  sort_order    smallint not null default 0,
  is_active     boolean not null default true
);

insert into public.account_catalog_groups (code, label, description, sort_order) values
  ('FA_FURNITURE',   'Furniture & Fixtures',   'Furniture and fixtures used in the business, with their accumulated depreciation', 10),
  ('FA_OFFICE',      'Office Equipment',       'Office equipment class with accumulated depreciation', 11),
  ('FA_VEHICLES',    'Vehicles',               'Vehicles used in the business, with accumulated depreciation', 12),
  ('FA_MACHINERY',   'Machinery & Plant',      'Production / plant machinery, with accumulated depreciation', 13),
  ('FA_BUILDINGS',   'Buildings',              'Owned buildings, with accumulated depreciation', 14),
  ('FA_LEASEHOLD',   'Leasehold Improvements', 'Improvements to leased premises, with accumulated depreciation', 15),
  ('FA_INTANGIBLES', 'Intangible Assets',      'Software & licences, intellectual property, goodwill and accumulated amortisation', 16),
  ('INVENTORY',      'Inventory',              'Physical stock held for sale or resale', 20),
  ('INVENTORY_MANUFACTURING', 'Inventory - Manufacturing', 'Raw materials, work in progress and finished goods', 21),
  ('COST_OF_SALES',  'Cost of Sales - Goods',  'Purchases, cost of goods sold, freight inward and import duties', 30),
  ('COST_OF_SALES_MANUFACTURING', 'Cost of Sales - Manufacturing', 'Direct materials, direct labour, factory overheads and WIP movement', 31),
  ('COST_OF_SALES_SERVICE', 'Cost of Sales - Services', 'Subcontractor and direct project delivery costs', 32),
  ('CONTRACT_RETENTIONS', 'Contract Retentions', 'Retentions receivable and unbilled contract revenue', 33),
  ('STAFF_BENEFITS', 'Employee Benefits',      'Employee benefits and staff training costs', 40),
  ('TELECOM',        'Telephone & Postage',    'Telephone/mobile and postage & courier costs', 41),
  ('INSURANCE',      'Insurance',              'Business insurance premiums', 42),
  ('CLOUD_HOSTING',  'Cloud Infrastructure',   'Cloud hosting and infrastructure costs', 43),
  ('PETTY_CASH',     'Petty Cash',             'Petty cash float', 44),
  ('BORROWINGS',     'Borrowings',             'Loans, current portion of long-term debt and interest expense', 50),
  ('LEASES',         'Lease Liabilities',      'Lease liability recognised under a lease contract', 51),
  ('WITHHOLDING_TAX','Withholding Tax',        'Withholding tax payable', 52),
  ('INCOME_TAX',     'Income Tax',             'Income tax payable and income tax expense', 53),
  ('DEFERRED_REVENUE','Deferred Revenue',      'Revenue received in advance of delivery', 54),
  ('DRAWINGS_DIVIDENDS','Drawings / Dividends','Owner drawings or shareholder dividends', 55),
  ('OTHER_NON_CURRENT','Other Non-Current Liabilities', 'Non-current liabilities outside borrowings and leases', 56)
on conflict (code) do update
  set label = excluded.label,
      description = excluded.description,
      sort_order = excluded.sort_order;

-- ---------------------------------------------------------------------
-- 2. The account catalog (single source of truth)
--    group_code is null for unconditional accounts; business_types is
--    null when the row applies to every business type.
-- ---------------------------------------------------------------------
create table if not exists public.account_catalog (
  code           text primary key,
  name           text not null,
  account_type   account_type_code not null,
  category_code  text not null references public.account_categories(code),
  parent_code    text references public.account_catalog(code)
                   deferrable initially deferred,
  sort_order     smallint not null default 0,
  business_types business_type_code[],
  description    text,
  check (code <> parent_code)
);

-- Group membership defines OPTIONALITY: a catalog row reachable through
-- any group is seeded only when that bundle is approved, everything else
-- is BASE (seeded for every business type it applies to).  A row may
-- belong to several bundles (the inventory parent, for example, is a
-- member of both Inventory bundles).
create table if not exists public.account_catalog_group_items (
  group_code text not null references public.account_catalog_groups(code) on delete cascade,
  code       text not null references public.account_catalog(code) on delete cascade,
  primary key (group_code, code)
);

-- ---------------------------------------------------------------------
-- 3. Catalog rows — ASSETS
-- ---------------------------------------------------------------------
insert into public.account_catalog
  (code, name, account_type, category_code, parent_code, sort_order, business_types, description)
values
  ('1000','Cash & Cash Equivalents','ASSET','CASH',null,1,null,
     'Grouping account for bank and cash balances'),
  ('1010','Bank','ASSET','BANK','1000',2,null,'Bank current accounts'),
  ('1020','Cash','ASSET','CASH','1000',3,null,'Cash on hand'),
  ('1030','Petty Cash','ASSET','CASH','1000',4,null,'Petty cash float'),
  ('1100','Accounts Receivable','ASSET','RECEIVABLE',null,10,null,
     'Control account debited by credit sales — resolved by name in the accounting engine'),
  ('1110','Retentions Receivable','ASSET','RECEIVABLE','1100',11,null,
     'Amounts withheld by the client until the defect-liability period ends'),
  ('1120','Unbilled Revenue','ASSET','RECEIVABLE','1100',12,null,
     'Contract revenue earned but not yet invoiced'),
  ('1200','Inventory','ASSET','INVENTORY',null,20,null,
     'Stock held for resale; a grouping account when sub-classified'),
  ('1230','Raw Materials','ASSET','INVENTORY','1200',21,null,'Materials awaiting production'),
  ('1240','Work in Progress','ASSET','INVENTORY','1200',22,null,'Part-finished production'),
  ('1250','Finished Goods','ASSET','INVENTORY','1200',23,null,'Completed goods not yet sold'),
  ('1300','Prepayments & Deposits','ASSET','PREPAID',null,30,null,
     'Grouping account for amounts paid in advance'),
  ('1310','Prepaid Expenses','ASSET','PREPAID','1300',31,null,'Expenses paid before they are incurred'),
  ('1320','Advances to Suppliers','ASSET','PREPAID','1300',32,null,'Advance payments to suppliers'),
  ('1330','Security Deposits','ASSET','OTHER_ASSET','1300',33,null,'Refundable deposits held by third parties'),
  ('1500','Property, Plant & Equipment','ASSET','FIXED_ASSET',null,40,null,
     'Grouping account for tangible fixed-asset classes'),
  ('1510','Furniture & Fixtures','ASSET','FIXED_ASSET','1500',41,null,'Furniture and fixtures at cost'),
  ('1515','Accumulated Depreciation - Furniture & Fixtures','ASSET','ACCUM_DEPRECIATION','1500',42,null,
     'Contra-asset: accumulated depreciation of furniture & fixtures'),
  ('1520','Computer Equipment','ASSET','FIXED_ASSET','1500',43,null,'Computers, laptops and peripherals at cost'),
  ('1525','Accumulated Depreciation - Computer Equipment','ASSET','ACCUM_DEPRECIATION','1500',44,null,
     'Contra-asset: accumulated depreciation of computer equipment'),
  ('1530','Office Equipment','ASSET','FIXED_ASSET','1500',45,null,'Office equipment at cost'),
  ('1535','Accumulated Depreciation - Office Equipment','ASSET','ACCUM_DEPRECIATION','1500',46,null,
     'Contra-asset: accumulated depreciation of office equipment'),
  ('1540','Vehicles','ASSET','FIXED_ASSET','1500',47,null,'Vehicles at cost'),
  ('1545','Accumulated Depreciation - Vehicles','ASSET','ACCUM_DEPRECIATION','1500',48,null,
     'Contra-asset: accumulated depreciation of vehicles'),
  ('1550','Machinery & Plant','ASSET','FIXED_ASSET','1500',49,null,'Production and plant machinery at cost'),
  ('1555','Accumulated Depreciation - Machinery & Plant','ASSET','ACCUM_DEPRECIATION','1500',50,null,
     'Contra-asset: accumulated depreciation of machinery & plant'),
  ('1560','Buildings','ASSET','FIXED_ASSET','1500',51,null,'Owned buildings at cost'),
  ('1565','Accumulated Depreciation - Buildings','ASSET','ACCUM_DEPRECIATION','1500',52,null,
     'Contra-asset: accumulated depreciation of buildings'),
  ('1570','Leasehold Improvements','ASSET','FIXED_ASSET','1500',53,null,'Improvements to leased premises at cost'),
  ('1575','Accumulated Depreciation - Leasehold Improvements','ASSET','ACCUM_DEPRECIATION','1500',54,null,
     'Contra-asset: accumulated depreciation of leasehold improvements'),
  ('1580','Other Property, Plant & Equipment','ASSET','FIXED_ASSET','1500',55,null,
     'Other tangible fixed assets not covered by a specific class'),
  ('1600','Intangible Assets','ASSET','INTANGIBLE',null,60,null,
     'Grouping account for non-monetary assets without physical substance'),
  ('1610','Software & Licences','ASSET','INTANGIBLE','1600',61,null,'Capitalised software and licences'),
  ('1620','Intellectual Property & Trademarks','ASSET','INTANGIBLE','1600',62,null,'Capitalised IP, trademarks and patents'),
  ('1630','Goodwill','ASSET','INTANGIBLE','1600',63,null,'Goodwill arising on acquisition'),
  ('1640','Accumulated Amortisation','ASSET','ACCUM_DEPRECIATION','1600',64,null,
     'Contra-asset: accumulated amortisation of intangible assets'),
  ('1700','Other Non-Current Assets','ASSET','OTHER_ASSET',null,70,null,
     'Long-term assets outside fixed assets, intangibles and investments');

-- ---------------------------------------------------------------------
-- 4. Catalog rows — LIABILITIES & EQUITY
--    NOTE: current-year profit/loss is intentionally NOT an account.
--    v_balance_sheet_summary already derives it from the income
--    statement; adding a postable account would double-count the result.
-- ---------------------------------------------------------------------
insert into public.account_catalog
  (code, name, account_type, category_code, parent_code, sort_order, business_types, description)
values
  ('2000','Trade Payables','LIABILITY','PAYABLE',null,100,null,
     'Grouping account for supplier liabilities'),
  ('2010','Accounts Payable','LIABILITY','PAYABLE','2000',101,null,
     'Control account credited by credit purchases'),
  ('2100','Tax Payables','LIABILITY','TAX_PAYABLE',null,110,null,
     'Grouping account for taxes owed to authorities'),
  ('2110','Sales Tax Payable','LIABILITY','TAX_PAYABLE','2100',111,null,'Sales tax / GST collected on sales'),
  ('2120','Withholding Tax Payable','LIABILITY','TAX_PAYABLE','2100',112,null,
     'Tax withheld at source and payable to the authority'),
  ('2130','Income Tax Payable','LIABILITY','TAX_PAYABLE','2100',113,null,'Corporate income tax owed'),
  ('2200','Accrued Liabilities','LIABILITY','ACCRUED',null,120,null,
     'Grouping account for expenses incurred but unpaid'),
  ('2210','Accrued Salaries','LIABILITY','ACCRUED','2200',121,null,'Salaries earned but not yet paid'),
  ('2220','Accrued Expenses','LIABILITY','ACCRUED','2200',122,null,'Expenses incurred but not yet invoiced'),
  ('2300','Short-Term Borrowings','LIABILITY','LOAN',null,130,null,
     'Grouping account for borrowings due within twelve months'),
  ('2310','Short-Term Loans','LIABILITY','LOAN','2300',131,null,'Loans repayable within twelve months'),
  ('2320','Current Portion of Long-Term Debt','LIABILITY','LOAN','2300',132,null,
     'Portion of long-term debt falling due within twelve months'),
  ('2400','Unearned Revenue','LIABILITY','UNEARNED_REVENUE',null,140,null,
     'Payments received before delivery of the goods or services'),
  ('2500','Other Current Liabilities','LIABILITY','OTHER_LIABILITY',null,150,null,
     'Current liabilities outside the classes above'),
  ('2600','Long-Term Borrowings','LIABILITY','LOAN',null,160,null,
     'Grouping account for borrowings due after twelve months'),
  ('2610','Long-Term Loans','LIABILITY','LOAN','2600',161,null,'Loans repayable after twelve months'),
  ('2700','Lease Liabilities','LIABILITY','OTHER_LIABILITY',null,170,null,
     'Lease liability recognised under a lease contract'),
  ('2800','Other Non-Current Liabilities','LIABILITY','OTHER_LIABILITY',null,180,null,
     'Non-current liabilities outside the classes above'),
  ('3000','Equity','EQUITY','OWNER_CAPITAL',null,200,null,
     'Grouping account for capital, retained earnings and distributions'),
  ('3010','Owner Capital','EQUITY','OWNER_CAPITAL','3000',201,null,
     'Owner or shareholder contributions to the business'),
  ('3200','Retained Earnings','EQUITY','RETAINED_EARNINGS','3000',202,null,
     'Cumulative profits retained in the business'),
  ('3300','Drawings / Dividends','EQUITY','DRAWINGS','3000',203,null,
     'Distributions to owners or shareholders');

-- ---------------------------------------------------------------------
-- 5. Catalog rows — REVENUE
--    The revenue breakdown follows the organization's actual business
--    model: a software house, a trading business and a clinic do not
--    earn revenue in the same way, so their revenue lines differ.
-- ---------------------------------------------------------------------
insert into public.account_catalog
  (code, name, account_type, category_code, parent_code, sort_order, business_types, description)
values
  ('4000','Operating Revenue','REVENUE','OPERATING_REVENUE',null,300,null,
     'Grouping account for revenue earned from core operations'),
  ('4010','Services Revenue','REVENUE','OPERATING_REVENUE','4000',301,
     '{SERVICE_BUSINESS,OTHER,IT_SERVICES}'::business_type_code[],'Revenue from services delivered to clients'),
  ('4020','Software Development Revenue','REVENUE','OPERATING_REVENUE','4000',302,
     '{SOFTWARE_HOUSE,SMALL_TECH_STARTUP}'::business_type_code[],'Revenue from custom software development'),
  ('4030','Consulting Revenue','REVENUE','OPERATING_REVENUE','4000',303,
     '{SOFTWARE_HOUSE,SMALL_TECH_STARTUP,TECHNOLOGY_CONSULTANT}'::business_type_code[],
     'Revenue from consulting engagements'),
  ('4040','Maintenance & Support Revenue','REVENUE','OPERATING_REVENUE','4000',304,
     '{SOFTWARE_HOUSE,SMALL_TECH_STARTUP,IT_SERVICES}'::business_type_code[],
     'Revenue from maintenance and support contracts'),
  ('4050','Subscription Revenue','REVENUE','OPERATING_REVENUE','4000',305,
     '{SAAS_STARTUP}'::business_type_code[],'Recurring subscription revenue'),
  ('4060','Implementation & Onboarding Revenue','REVENUE','OPERATING_REVENUE','4000',306,
     '{SAAS_STARTUP}'::business_type_code[],'One-off implementation and onboarding fees'),
  ('4070','Managed Services Revenue','REVENUE','OPERATING_REVENUE','4000',307,
     '{IT_SERVICES}'::business_type_code[],'Revenue from managed and support services'),
  ('4080','Advisory Revenue','REVENUE','OPERATING_REVENUE','4000',308,
     '{TECHNOLOGY_CONSULTANT,CONSULTING}'::business_type_code[],'Revenue from advisory engagements'),
  ('4090','Project Revenue','REVENUE','OPERATING_REVENUE','4000',309,
     '{FREELANCER,CONSULTING}'::business_type_code[],'Revenue from fixed-scope projects'),
  ('4100','Creative Services Revenue','REVENUE','OPERATING_REVENUE','4000',310,
     '{DIGITAL_AGENCY}'::business_type_code[],'Revenue from creative production'),
  ('4110','Media Buying Revenue','REVENUE','OPERATING_REVENUE','4000',311,
     '{DIGITAL_AGENCY}'::business_type_code[],'Revenue from media planning and buying'),
  ('4120','Sales Revenue','REVENUE','OPERATING_REVENUE','4000',312,
     '{TRADING,MANUFACTURING,E_COMMERCE}'::business_type_code[],'Revenue from goods sold'),
  ('4130','Online Store Revenue','REVENUE','OPERATING_REVENUE','4000',313,
     '{E_COMMERCE}'::business_type_code[],'Revenue from the online store channel'),
  ('4140','Shipping & Handling Revenue','REVENUE','OPERATING_REVENUE','4000',314,
     '{E_COMMERCE}'::business_type_code[],'Delivery charges recovered from customers'),
  ('4150','Export Sales','REVENUE','OPERATING_REVENUE','4000',315,
     '{TRADING,MANUFACTURING}'::business_type_code[],'Revenue from export sales'),
  ('4160','Contract Revenue','REVENUE','OPERATING_REVENUE','4000',316,
     '{CONSTRUCTION}'::business_type_code[],'Revenue recognised on construction contracts'),
  ('4170','Patient Service Revenue','REVENUE','OPERATING_REVENUE','4000',317,
     '{HEALTHCARE}'::business_type_code[],'Revenue from patient treatment and procedures'),
  ('4180','Consultation Revenue','REVENUE','OPERATING_REVENUE','4000',318,
     '{HEALTHCARE}'::business_type_code[],'Revenue from consultations'),
  ('4190','Tuition Revenue','REVENUE','OPERATING_REVENUE','4000',319,
     '{EDUCATION}'::business_type_code[],'Revenue from tuition and course fees'),
  ('4195','Training & Workshop Revenue','REVENUE','OPERATING_REVENUE','4000',320,
     '{EDUCATION,CONSULTING}'::business_type_code[],'Revenue from training and workshops'),
  ('4900','Other Income','REVENUE','OTHER_INCOME',null,350,null,'Non-operating income');

-- ---------------------------------------------------------------------
-- 6. Catalog rows — EXPENSES
--    Code convention: 6xxx.  6000 stays the FIRST expense account and
--    keeps a generic name ("General Operating Expense") because the
--    accounting engine resolves its default EXPENSE account by name
--    (see migration 038 + app/accounting_engine._resolve_default_account).
-- ---------------------------------------------------------------------
insert into public.account_catalog
  (code, name, account_type, category_code, parent_code, sort_order, business_types, description)
values
  ('6000','General Operating Expense','EXPENSE','OPERATING_EXPENSE',null,400,null,
     'Default operating expense account for unmapped purchases'),
  ('6100','Payroll & Staff Costs','EXPENSE','PAYROLL',null,410,null,'Grouping account for staff costs'),
  ('6110','Salaries','EXPENSE','PAYROLL','6100',411,null,'Salaries and wages'),
  ('6120','Freelancers & Contractors','EXPENSE','PAYROLL','6100',412,null,'Independent contractors and freelancers'),
  ('6130','Employee Benefits','EXPENSE','PAYROLL','6100',413,null,'Benefits, medical cover and contributions'),
  ('6140','Staff Training & Development','EXPENSE','PAYROLL','6100',414,null,'Training and professional development'),
  ('6200','Premises','EXPENSE','OPERATING_EXPENSE',null,420,null,'Grouping account for premises costs'),
  ('6210','Rent','EXPENSE','OPERATING_EXPENSE','6200',421,null,'Office, shop, clinic or factory rent'),
  ('6220','Utilities','EXPENSE','OPERATING_EXPENSE','6200',422,null,'Electricity, gas and water'),
  ('6230','Repairs & Maintenance','EXPENSE','OPERATING_EXPENSE','6200',423,null,
     'Repairs and maintenance of premises and equipment'),
  ('6240','Insurance','EXPENSE','OPERATING_EXPENSE','6200',424,null,'Business insurance premiums'),
  ('6300','Communication','EXPENSE','OPERATING_EXPENSE',null,430,null,'Grouping account for communication costs'),
  ('6310','Internet','EXPENSE','OPERATING_EXPENSE','6300',431,null,'Internet and data connectivity'),
  ('6320','Telephone & Mobile','EXPENSE','OPERATING_EXPENSE','6300',432,null,'Telephone and mobile charges'),
  ('6330','Postage & Courier','EXPENSE','OPERATING_EXPENSE','6300',433,null,'Postage, courier and delivery charges'),
  ('6400','Technology & Software','EXPENSE','OPERATING_EXPENSE',null,440,null,
     'Grouping account for technology and software costs'),
  ('6410','Software Subscriptions','EXPENSE','OPERATING_EXPENSE','6400',441,null,
     'Software subscriptions and SaaS licences'),
  ('6420','Cloud Infrastructure','EXPENSE','OPERATING_EXPENSE','6400',442,null,
     'Cloud hosting, servers and infrastructure'),
  ('6430','Hardware Purchases','EXPENSE','OPERATING_EXPENSE','6400',443,null,
     'Small hardware and IT consumables charged to expense'),
  ('6500','Office & Administration','EXPENSE','OPERATING_EXPENSE',null,450,null,
     'Grouping account for administrative costs'),
  ('6510','Office Supplies','EXPENSE','OPERATING_EXPENSE','6500',451,null,'Consumables and stationery'),
  ('6520','Travel & Entertainment','EXPENSE','OPERATING_EXPENSE','6500',452,null,
     'Business travel, meals and entertainment'),
  ('6530','Professional & Legal Fees','EXPENSE','OPERATING_EXPENSE','6500',453,null,
     'Audit, legal, tax and professional fees'),
  ('6540','Marketing & Advertising','EXPENSE','OPERATING_EXPENSE','6500',454,null,
     'Advertising, marketing and promotion'),
  ('6600','Depreciation & Amortisation','EXPENSE','DEPRECIATION_EXPENSE',null,460,null,
     'Grouping account for depreciation and amortisation'),
  ('6610','Depreciation Expense','EXPENSE','DEPRECIATION_EXPENSE','6600',461,null,
     'Depreciation of property, plant and equipment'),
  ('6620','Amortisation Expense','EXPENSE','DEPRECIATION_EXPENSE','6600',462,null,
     'Amortisation of intangible assets'),
  ('6700','Finance Costs','EXPENSE','OTHER_EXPENSE',null,470,null,
     'Grouping account for finance and other non-operating costs'),
  ('6710','Bank Charges','EXPENSE','OTHER_EXPENSE','6700',471,null,'Bank and payment gateway charges'),
  ('6720','Interest Expense','EXPENSE','OTHER_EXPENSE','6700',472,null,'Interest on borrowings and leases'),
  ('6730','Foreign Exchange Loss','EXPENSE','OTHER_EXPENSE','6700',473,null,
     'Losses on foreign-currency balances');

-- ---------------------------------------------------------------------
-- 8. Which business types may be OFFERED each bundle, and whether the
--    bundle is RECOMMENDED by default for that type.  A bundle with no
--    row for a business type is never proposed for it — this is what
--    keeps a machinery account out of a software consultancy's chart.
-- ---------------------------------------------------------------------
create table if not exists public.account_catalog_group_business_types (
  group_code    text not null references public.account_catalog_groups(code) on delete cascade,
  business_type business_type_code not null,
  is_default    boolean not null default false,
  rationale     text,
  primary key (group_code, business_type)
);

insert into public.account_catalog_group_business_types (group_code, business_type, is_default, rationale) values
  -- Fixed assets: furniture is assumed for almost every business; the
  -- heavier classes are offered everywhere but only recommended where the
  -- nature of the business implies them.
  ('FA_FURNITURE','SOFTWARE_HOUSE',true,null),   ('FA_FURNITURE','SMALL_TECH_STARTUP',true,null),
  ('FA_FURNITURE','IT_SERVICES',true,null),      ('FA_FURNITURE','SAAS_STARTUP',true,null),
  ('FA_FURNITURE','DIGITAL_AGENCY',true,null),   ('FA_FURNITURE','TECHNOLOGY_CONSULTANT',true,null),
  ('FA_FURNITURE','FREELANCER',false,null),      ('FA_FURNITURE','SERVICE_BUSINESS',true,null),
  ('FA_FURNITURE','CONSULTING',true,null),       ('FA_FURNITURE','E_COMMERCE',true,null),
  ('FA_FURNITURE','MANUFACTURING',true,null),    ('FA_FURNITURE','TRADING',true,null),
  ('FA_FURNITURE','CONSTRUCTION',true,null),     ('FA_FURNITURE','HEALTHCARE',true,null),
  ('FA_FURNITURE','EDUCATION',true,null),        ('FA_FURNITURE','OTHER',true,null),
  ('FA_OFFICE','SOFTWARE_HOUSE',false,null),     ('FA_OFFICE','SMALL_TECH_STARTUP',false,null),
  ('FA_OFFICE','IT_SERVICES',false,null),        ('FA_OFFICE','SAAS_STARTUP',false,null),
  ('FA_OFFICE','DIGITAL_AGENCY',false,null),     ('FA_OFFICE','TECHNOLOGY_CONSULTANT',false,null),
  ('FA_OFFICE','FREELANCER',false,null),         ('FA_OFFICE','SERVICE_BUSINESS',true,null),
  ('FA_OFFICE','CONSULTING',false,null),         ('FA_OFFICE','E_COMMERCE',false,null),
  ('FA_OFFICE','MANUFACTURING',false,null),      ('FA_OFFICE','TRADING',false,null),
  ('FA_OFFICE','CONSTRUCTION',false,null),       ('FA_OFFICE','HEALTHCARE',true,null),
  ('FA_OFFICE','EDUCATION',true,null),           ('FA_OFFICE','OTHER',false,null),
  ('FA_VEHICLES','E_COMMERCE',true,null),        ('FA_VEHICLES','MANUFACTURING',true,null),
  ('FA_VEHICLES','TRADING',true,null),           ('FA_VEHICLES','CONSTRUCTION',true,null),
  ('FA_VEHICLES','HEALTHCARE',false,null),       ('FA_VEHICLES','EDUCATION',false,null),
  ('FA_VEHICLES','SERVICE_BUSINESS',false,null), ('FA_VEHICLES','OTHER',false,null),
  ('FA_VEHICLES','SOFTWARE_HOUSE',false,null),   ('FA_VEHICLES','IT_SERVICES',false,null),
  ('FA_VEHICLES','CONSULTING',false,null),       ('FA_VEHICLES','DIGITAL_AGENCY',false,null),
  ('FA_VEHICLES','TECHNOLOGY_CONSULTANT',false,null), ('FA_VEHICLES','SAAS_STARTUP',false,null),
  ('FA_VEHICLES','SMALL_TECH_STARTUP',false,null), ('FA_VEHICLES','FREELANCER',false,null),
  ('FA_MACHINERY','MANUFACTURING',true,null),    ('FA_MACHINERY','CONSTRUCTION',true,null),
  ('FA_MACHINERY','TRADING',false,null),         ('FA_MACHINERY','HEALTHCARE',false,null),
  ('FA_MACHINERY','EDUCATION',false,null),       ('FA_MACHINERY','OTHER',false,null),
  ('FA_BUILDINGS','MANUFACTURING',false,null),   ('FA_BUILDINGS','TRADING',false,null),
  ('FA_BUILDINGS','CONSTRUCTION',false,null),    ('FA_BUILDINGS','HEALTHCARE',false,null),
  ('FA_BUILDINGS','EDUCATION',false,null),       ('FA_BUILDINGS','OTHER',false,null),
  ('FA_BUILDINGS','SERVICE_BUSINESS',false,null),
  ('FA_LEASEHOLD','SOFTWARE_HOUSE',false,null),  ('FA_LEASEHOLD','IT_SERVICES',false,null),
  ('FA_LEASEHOLD','SAAS_STARTUP',false,null),    ('FA_LEASEHOLD','DIGITAL_AGENCY',false,null),
  ('FA_LEASEHOLD','CONSULTING',false,null),      ('FA_LEASEHOLD','E_COMMERCE',false,null),
  ('FA_LEASEHOLD','MANUFACTURING',false,null),   ('FA_LEASEHOLD','TRADING',false,null),
  ('FA_LEASEHOLD','CONSTRUCTION',false,null),    ('FA_LEASEHOLD','HEALTHCARE',false,null),
  ('FA_LEASEHOLD','EDUCATION',false,null),       ('FA_LEASEHOLD','SERVICE_BUSINESS',false,null),
  ('FA_LEASEHOLD','OTHER',false,null),
  ('FA_INTANGIBLES','SOFTWARE_HOUSE',true,null), ('FA_INTANGIBLES','SMALL_TECH_STARTUP',true,null),
  ('FA_INTANGIBLES','SAAS_STARTUP',true,null),   ('FA_INTANGIBLES','IT_SERVICES',false,null),
  ('FA_INTANGIBLES','DIGITAL_AGENCY',false,null),('FA_INTANGIBLES','TECHNOLOGY_CONSULTANT',false,null),
  ('FA_INTANGIBLES','E_COMMERCE',false,null),    ('FA_INTANGIBLES','TRADING',false,null),
  ('FA_INTANGIBLES','MANUFACTURING',false,null), ('FA_INTANGIBLES','CONSTRUCTION',false,null),
  ('FA_INTANGIBLES','HEALTHCARE',false,null),    ('FA_INTANGIBLES','EDUCATION',false,null),
  ('FA_INTANGIBLES','SERVICE_BUSINESS',false,null), ('FA_INTANGIBLES','OTHER',false,null)
on conflict (group_code, business_type) do update
  set is_default = excluded.is_default, rationale = excluded.rationale;

-- Remaining bundles are OFFERED to every business type (offering is not
-- the same as recommending — nothing is created unless it is selected),
-- except the fixed-asset classes above, which are restricted to the
-- business types where they are plausible.
insert into public.account_catalog_group_business_types (group_code, business_type, is_default)
select g.code, bt.value, false
from public.account_catalog_groups g
cross join unnest(enum_range(null::public.business_type_code)) as bt(value)
where g.code not in (
  'FA_FURNITURE','FA_OFFICE','FA_VEHICLES','FA_MACHINERY',
  'FA_BUILDINGS','FA_LEASEHOLD','FA_INTANGIBLES')
on conflict (group_code, business_type) do nothing;

-- Recommendations derived purely from the business type.
update public.account_catalog_group_business_types set is_default = true
where (group_code, business_type) in (
  ('INVENTORY','TRADING'), ('INVENTORY','E_COMMERCE'),
  ('INVENTORY_MANUFACTURING','MANUFACTURING'),
  ('COST_OF_SALES','TRADING'), ('COST_OF_SALES','E_COMMERCE'),
  ('COST_OF_SALES_MANUFACTURING','MANUFACTURING'),
  ('COST_OF_SALES_SERVICE','CONSTRUCTION'),
  ('CONTRACT_RETENTIONS','CONSTRUCTION'),
  ('DEFERRED_REVENUE','SAAS_STARTUP'),
  ('DRAWINGS_DIVIDENDS','FREELANCER'), ('DRAWINGS_DIVIDENDS','SERVICE_BUSINESS'),
  ('DRAWINGS_DIVIDENDS','OTHER'),
  ('CLOUD_HOSTING','SOFTWARE_HOUSE'), ('CLOUD_HOSTING','SMALL_TECH_STARTUP'),
  ('CLOUD_HOSTING','SAAS_STARTUP'), ('CLOUD_HOSTING','IT_SERVICES'),
  ('CLOUD_HOSTING','DIGITAL_AGENCY'), ('CLOUD_HOSTING','TECHNOLOGY_CONSULTANT')
);

-- Telephone & postage is recommended everywhere (a phone line is a given
-- for any operating business), so it is set independently of the list.
update public.account_catalog_group_business_types set is_default = true
where group_code = 'TELECOM';

-- ---------------------------------------------------------------------
-- 9. Templates for the business types that never had one.  Before this
--    migration a TRADING, MANUFACTURING, E_COMMERCE, CONSTRUCTION,
--    HEALTHCARE or EDUCATION organization silently received the generic
--    service chart.
-- ---------------------------------------------------------------------
insert into public.account_templates (code, name, business_type, description, is_active) values
  ('CONSULTING',    'Consulting Firm',        'CONSULTING',    'Advisory and consultancy practices',                  true),
  ('E_COMMERCE',    'E-Commerce Business',    'E_COMMERCE',    'Online retail and marketplace sellers',               true),
  ('MANUFACTURING', 'Manufacturing Business', 'MANUFACTURING', 'Businesses that produce and sell goods',              true),
  ('TRADING',       'Trading Business',       'TRADING',       'Wholesale and retail trading businesses',             true),
  ('CONSTRUCTION',  'Construction Business',  'CONSTRUCTION',  'Contractors and construction companies',              true),
  ('HEALTHCARE',    'Healthcare Practice',    'HEALTHCARE',    'Clinics, practices and healthcare providers',         true),
  ('EDUCATION',     'Education Provider',     'EDUCATION',     'Schools, institutes and training providers',          true)
on conflict (code) do update
  set name = excluded.name,
      business_type = excluded.business_type,
      description = excluded.description,
      is_active = true;

alter table public.account_template_items
  add column if not exists is_optional boolean not null default false;

-- Materialise the catalog into the table the seeding path already reads.
create or replace function public.rebuild_account_template_items()
returns integer
language plpgsql
security definer
set search_path = public
as $$
DECLARE
  v_count integer;
  v_bad   integer;
  v_missing text;
BEGIN
  DELETE FROM public.account_template_items;

  INSERT INTO public.account_template_items (
    template_id, code, name, account_type, account_category_id,
    suggested_parent_code, sort_order, is_optional)
  SELECT t.id, c.code, c.name, c.account_type, ac.id, c.parent_code, c.sort_order,
         EXISTS (SELECT 1 FROM public.account_catalog_group_items gi WHERE gi.code = c.code)
  FROM public.account_templates t
  JOIN public.account_catalog c
    ON (c.business_types IS NULL OR t.business_type = ANY(c.business_types))
  JOIN public.account_categories ac ON ac.code = c.category_code
  WHERE t.is_active;

  GET DIAGNOSTICS v_count = ROW_COUNT;

  -- Every template item's parent must exist in the SAME template, or the
  -- hierarchy would silently become flat again.
  SELECT count(*) INTO v_bad
  FROM public.account_template_items ti
  WHERE ti.suggested_parent_code IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM public.account_template_items p
      WHERE p.template_id = ti.template_id AND p.code = ti.suggested_parent_code
    );
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'account catalog: % template item(s) reference a parent that is not in the same template', v_bad;
  END IF;

  -- Every template must carry the control accounts the accounting engine
  -- resolves by name/behaviour, otherwise invoices and unmapped purchases
  -- would fail to post for organizations created from it.
  SELECT string_agg(t.code, ', ') INTO v_missing
  FROM public.account_templates t
  WHERE t.is_active AND (
       NOT EXISTS (SELECT 1 FROM public.account_template_items ti
                   WHERE ti.template_id = t.id AND ti.name = 'Accounts Receivable')
    OR NOT EXISTS (SELECT 1 FROM public.account_template_items ti
                   WHERE ti.template_id = t.id AND ti.name = 'General Operating Expense')
    OR NOT EXISTS (SELECT 1 FROM public.account_template_items ti
                   WHERE ti.template_id = t.id AND ti.account_type = 'ASSET'
                     AND ti.name IN ('Bank','Cash'))
  );
  IF v_missing IS NOT NULL THEN
    RAISE EXCEPTION 'account catalog: template(s) missing required control accounts: %', v_missing;
  END IF;

  RETURN v_count;
END;
$$;

select public.rebuild_account_template_items();

-- ---------------------------------------------------------------------
-- 10. Business type → template.  Deterministic, so the AI assistant can
--     tell the user exactly which chart their business type would get,
--     and so onboarding never silently falls back to a service chart.
-- ---------------------------------------------------------------------
create or replace function public.account_template_for_business_type(
  p_business_type business_type_code
)
returns text
language sql
stable
as $$
  select coalesce(
    (select t.code from public.account_templates t
      where t.is_active and t.business_type = p_business_type
      order by t.created_at, t.code limit 1),
    (select t.code from public.account_templates t
      where t.is_active
        and t.business_type = (case
              when p_business_type = 'SMALL_TECH_STARTUP' then 'SOFTWARE_HOUSE'
              when p_business_type = 'SERVICE_BUSINESS'   then 'OTHER'
              else p_business_type::text end)::business_type_code
      order by t.created_at, t.code limit 1),
    (select t.code from public.account_templates t
      where t.is_active and t.code = 'GENERAL_SERVICE' limit 1)
  );
$$;

-- Every business type with the template it resolves to, for onboarding.
create or replace function public.account_template_options()
returns table (
  business_type        business_type_code,
  template_code        text,
  template_name        text,
  base_account_count   integer,
  optional_group_count integer
)
language sql
stable
security definer
set search_path = public
as $$
  select bt.value,
         m.template_code,
         t.name,
         (select count(*)::int from public.account_template_items ti
           where ti.template_id = t.id and ti.is_optional = false),
         (select count(*)::int
            from public.account_catalog_group_business_types gbt
           where gbt.business_type = bt.value)
  from unnest(enum_range(null::public.business_type_code)) as bt(value)
  cross join lateral (
    select public.account_template_for_business_type(bt.value) as template_code
  ) m
  left join public.account_templates t on t.code = m.template_code
  order by bt.value;
$$;

-- ---------------------------------------------------------------------
-- 11. The catalog for ONE business type: the base chart plus every
--     optional bundle that may be offered for it.  This is the AI
--     assistant's grounding data — it can only propose accounts that
--     exist here, so it can never invent one.
-- ---------------------------------------------------------------------
create or replace function public.account_template_catalog(
  p_business_type business_type_code
)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  with template as (
    select t.id, t.code, t.name, t.description
    from public.account_templates t
    where t.code = public.account_template_for_business_type(p_business_type)
  ),
  relevant as (
    select c.*
    from public.account_catalog c
    where c.business_types is null or p_business_type = any(c.business_types)
  ),
  base_rows as (
    select r.code, r.name, r.account_type, r.category_code, r.parent_code,
           r.description, r.sort_order
    from relevant r
    where not exists (select 1 from public.account_catalog_group_items gi
                      where gi.code = r.code)
  )
  select jsonb_build_object(
    'business_type', p_business_type::text,
    'template', jsonb_build_object(
      'code', (select code from template),
      'name', (select name from template),
      'description', (select description from template)
    ),
    'base_accounts', coalesce((
      select jsonb_agg(jsonb_build_object(
               'code', b.code, 'name', b.name, 'account_type', b.account_type,
               'category', b.category_code, 'parent_code', b.parent_code,
               'description', b.description)
             order by b.sort_order, b.code)
      from base_rows b), '[]'::jsonb),
    'optional_groups', coalesce((
      select jsonb_agg(jsonb_build_object(
               'code', g.code,
               'label', g.label,
               'description', g.description,
               'recommended', gbt.is_default,
               'accounts', coalesce((
                 select jsonb_agg(jsonb_build_object(
                          'code', r.code, 'name', r.name,
                          'account_type', r.account_type,
                          'category', r.category_code,
                          'parent_code', r.parent_code,
                          'description', r.description)
                        order by r.sort_order, r.code)
                 from public.account_catalog_group_items gi
                 join relevant r on r.code = gi.code
                 where gi.group_code = g.code), '[]'::jsonb),
               'account_count', (
                 select count(*) from public.account_catalog_group_items gi
                 join relevant r on r.code = gi.code
                 where gi.group_code = g.code))
             order by g.sort_order, g.code)
      from public.account_catalog_groups g
      join public.account_catalog_group_business_types gbt
        on gbt.group_code = g.code and gbt.business_type = p_business_type
      where g.is_active), '[]'::jsonb)
  );
$$;

-- ---------------------------------------------------------------------
-- 12. Seed an organization's chart of accounts from its business-aware
--     template.  Base accounts are always created; optionally-grouped
--     accounts are created only for the bundles that were approved.
--     Idempotent: an account whose code already exists is left alone,
--     and only parent links that actually change are written.
-- ---------------------------------------------------------------------
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

  -- The organization's default chart (the banking screen resolves this
  -- row before it will create a bank GL account).
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

  -- Parent/child hierarchy (Account → Parent account).  Written as an
  -- UPDATE so children can be inserted before their parent exists.  The
  -- parent is matched from a comma-joined FROM list because a JOIN's ON
  -- clause may not reference the UPDATE target.
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

  -- Audit trail for the bundles the user approved (the AI's suggestion
  -- and the decision that produced these accounts).
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

-- ---------------------------------------------------------------------
-- 13. Apply the onboarding decisions AFTER the organization row exists:
--     create the account bundles the user approved for the AI's
--     recommendation, and store the AI analysis + the user's answers on
--     the onboarding record.  Owner/administrator only, and the business
--     type may only be corrected while nothing has been posted.
-- ---------------------------------------------------------------------
create or replace function public.apply_organization_onboarding(
  p_organization_id uuid,
  p_groups          text[]  default '{}',
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
  v_groups        text[] := coalesce(p_groups, '{}');
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

  IF NOT public.has_org_role(p_organization_id, 2::smallint) THEN
    RAISE EXCEPTION 'Only an owner or administrator may apply onboarding account changes';
  END IF;

  v_business_type := coalesce(p_business_type, v_current_type);

  IF v_business_type <> v_current_type THEN
    -- Correcting the business type changes which chart is used.  That is
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

-- ---------------------------------------------------------------------
-- 14. create_organization — SAME SIGNATURE, corrected internals.
--     * the template is resolved by account_template_for_business_type()
--       instead of "fall back to GENERAL_SERVICE for everything";
--     * seeding (including parent links) is delegated to
--       seed_org_chart_of_accounts();
--     * the organization's default chart_of_accounts row is created, so
--       the banking screen can create bank GL accounts.
--     Everything else — slug, membership, settings, onboarding record,
--     financial year and the twelve accounting periods — is unchanged.
-- ---------------------------------------------------------------------
create or replace function public.create_organization(
  p_name                  text,
  p_business_type         business_type_code  default 'OTHER'::business_type_code,
  p_base_currency_code    character           default 'PKR'::bpchar,
  p_country_code          character           default 'PK'::bpchar,
  p_timezone              text                default 'Asia/Karachi'::text,
  p_fiscal_year_end_month smallint            default 6,
  p_legal_name            text                default null,
  p_tax_number            text                default null,
  p_registration_number   text                default null,
  p_core_services         text                default null,
  p_industry_details      text                default null,
  p_fiscal_year_start_year integer            default null
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_user_id uuid := auth.uid();
  v_org_id uuid;
  v_fy_id uuid;
  v_onboarding_id uuid;
  v_start_month int;
  v_start date;
  v_end date;
  v_year int;
  v_month int;
  v_owner_role_id uuid;
  v_slug text;
  v_suffix text;
BEGIN
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required';
  END IF;
  IF p_name IS NULL OR length(trim(p_name)) < 2 THEN
    RAISE EXCEPTION 'Organization name must be at least 2 characters';
  END IF;

  -- ---- Slug (unique, url-safe) ----
  v_slug := lower(regexp_replace(trim(p_name), '[^a-zA-Z0-9]+', '-', 'g'));
  v_slug := regexp_replace(v_slug, '^-+|-+$', '', 'g');
  IF v_slug = '' OR length(v_slug) < 2 THEN v_slug := 'org'; END IF;
  v_suffix := substr(replace(gen_random_uuid()::text, '-', ''), 1, 6);
  v_slug := left(v_slug, 40) || '-' || v_suffix;

  INSERT INTO public.organizations (name, slug, legal_name, business_type,
                                    tax_number, registration_number,
                                    base_currency_code, country_code, timezone,
                                    fiscal_year_end_month)
  VALUES (trim(p_name), v_slug, p_legal_name, p_business_type,
          p_tax_number, p_registration_number,
          p_base_currency_code, p_country_code, p_timezone,
          p_fiscal_year_end_month)
  RETURNING id INTO v_org_id;

  SELECT id INTO v_owner_role_id FROM public.organization_roles
  WHERE code = 'OWNER' LIMIT 1;
  IF v_owner_role_id IS NULL THEN
    RAISE EXCEPTION 'OWNER role not found in organization_roles';
  END IF;
  INSERT INTO public.organization_members (organization_id, user_id, role_id, status, joined_at)
  VALUES (v_org_id, v_user_id, v_owner_role_id, 'ACTIVE', now());

  INSERT INTO public.organization_settings (organization_id, invoice_prefix,
      quotation_prefix, bill_prefix, credit_note_prefix, journal_prefix,
      payment_prefix, receipt_prefix, customer_prefix, supplier_prefix,
      project_prefix, expense_prefix, asset_prefix, return_prefix,
      default_payment_terms_days, settings)
  VALUES (v_org_id, 'INV', 'QUT', 'BIL', 'CRN', 'JV', 'PAY', 'RCT',
      'CUS', 'SUP', 'PRJ', 'EXP', 'AST', 'RET', 30,
      jsonb_build_object('invoice_template', 'modern'));

  INSERT INTO public.organization_onboarding (organization_id, business_type,
      core_services, industry_details, recommended_accounts_generated,
      onboarding_completed_at)
  VALUES (v_org_id, p_business_type,
      CASE WHEN p_core_services IS NOT NULL AND trim(p_core_services) != ''
           THEN to_jsonb(ARRAY[p_core_services])
           ELSE '[]'::jsonb END,
      p_industry_details,
      false, now())
  RETURNING id INTO v_onboarding_id;

  v_start_month := (p_fiscal_year_end_month % 12) + 1;
  IF p_fiscal_year_start_year IS NOT NULL THEN
    v_year := p_fiscal_year_start_year;
  ELSE
    v_year := extract(year from current_date)::int;
    IF v_start_month > extract(month from current_date)::int THEN
      v_year := v_year - 1;
    END IF;
  END IF;
  v_start := make_date(v_year, v_start_month, 1);
  v_end := (v_start + interval '1 year - 1 day')::date;

  INSERT INTO public.financial_years (organization_id, name, start_date, end_date,
                                      status, is_current)
  VALUES (v_org_id,
          'FY ' || to_char(v_start, 'YYYY') || '-' || to_char(v_end, 'YY'),
          v_start, v_end, 'OPEN', true)
  RETURNING id INTO v_fy_id;

  FOR v_month IN 0..11 LOOP
    INSERT INTO public.accounting_periods (organization_id, financial_year_id,
        period_number, name, start_date, end_date, status)
    VALUES (v_org_id, v_fy_id, v_month + 1,
            to_char(v_start + (v_month || ' months')::interval, 'Month YYYY'),
            (v_start + (v_month || ' months')::interval)::date,
            (v_start + ((v_month + 1) || ' months')::interval)::date - 1,
            'OPEN');
  END LOOP;

  -- ---- Chart of accounts: business-aware template, with hierarchy ----
  PERFORM public.seed_org_chart_of_accounts(
    v_org_id, p_business_type, '{}'::text[], v_onboarding_id, null);

  RETURN v_org_id;
END;
$function$;

-- ---------------------------------------------------------------------
-- 15. RLS: the catalog is global reference data.  It mirrors the posture
--     of account_templates / account_categories (RLS on, read-only for
--     authenticated users, writes only through the security-definer
--     functions above).
-- ---------------------------------------------------------------------
-- The catalog's self-referencing parent link is DEFERRABLE so rows can be
-- inserted in any order; flush the checks before the ALTER TABLE below
-- (DDL cannot run with pending trigger events) — this also validates the
-- parent references immediately.
set constraints all immediate;

alter table public.account_catalog enable row level security;
alter table public.account_catalog_groups enable row level security;
alter table public.account_catalog_group_items enable row level security;
alter table public.account_catalog_group_business_types enable row level security;

drop policy if exists account_catalog_read_all on public.account_catalog;
create policy account_catalog_read_all on public.account_catalog
  for select to authenticated using (true);

drop policy if exists account_catalog_groups_read_all on public.account_catalog_groups;
create policy account_catalog_groups_read_all on public.account_catalog_groups
  for select to authenticated using (true);

drop policy if exists account_catalog_group_items_read_all on public.account_catalog_group_items;
create policy account_catalog_group_items_read_all on public.account_catalog_group_items
  for select to authenticated using (true);

drop policy if exists account_catalog_group_bt_read_all on public.account_catalog_group_business_types;
create policy account_catalog_group_bt_read_all on public.account_catalog_group_business_types
  for select to authenticated using (true);

-- ---------------------------------------------------------------------
-- 16. Privileges.  New functions default to EXECUTE for PUBLIC, so the
--     internal helpers are locked down and only the two functions the
--     application calls are opened to signed-in users.
-- ---------------------------------------------------------------------
revoke all on function public.rebuild_account_template_items() from public, anon, authenticated;
revoke all on function public.seed_org_chart_of_accounts(uuid, business_type_code, text[], uuid, text)
  from public, anon, authenticated;
revoke all on function public.account_template_for_business_type(business_type_code) from public, anon;
revoke all on function public.account_template_options() from public, anon;
revoke all on function public.account_template_catalog(business_type_code) from public, anon;
revoke all on function public.apply_organization_onboarding(uuid, text[], jsonb, business_type_code, text)
  from public, anon;

grant execute on function public.rebuild_account_template_items() to service_role, postgres;
grant execute on function public.seed_org_chart_of_accounts(uuid, business_type_code, text[], uuid, text)
  to service_role, postgres;
grant execute on function public.account_template_for_business_type(business_type_code)
  to service_role, postgres, authenticated;
grant execute on function public.account_template_options()
  to service_role, postgres, authenticated;
grant execute on function public.account_template_catalog(business_type_code)
  to service_role, postgres, authenticated;
grant execute on function public.apply_organization_onboarding(uuid, text[], jsonb, business_type_code, text)
  to service_role, postgres, authenticated;

comment on table public.account_catalog is
  'Single source of truth for the chart of accounts. Materialised into account_template_items by rebuild_account_template_items() and read by the AI onboarding assistant via account_template_catalog().';
comment on table public.account_catalog_groups is
  'Optional account bundles (fixed-asset classes, inventory, cost of sales, ...). Membership in a bundle makes a catalog row optional: it is seeded only when the bundle is approved during onboarding.';
comment on function public.seed_org_chart_of_accounts(uuid, business_type_code, text[], uuid, text) is
  'Seeds an organization chart of accounts from its business-aware template, applies the parent/child hierarchy and links the default chart_of_accounts row. Idempotent.';
comment on function public.apply_organization_onboarding(uuid, text[], jsonb, business_type_code, text) is
  'Applies the owner-approved onboarding decisions after create_organization(): optional account bundles plus the AI analysis stored on organization_onboarding.responses.';

-- ---------------------------------------------------------------------
-- 17. Self-check: every active template must expose at least one optional
--     bundle where its business type has one, and the static catalog must
--     contain no orphan parent references.
-- ---------------------------------------------------------------------
do $$
declare
  v_orphans integer;
  v_templates integer;
begin
  select count(*) into v_orphans
  from public.account_catalog c
  where c.parent_code is not null
    and not exists (select 1 from public.account_catalog p where p.code = c.parent_code);

  if v_orphans > 0 then
    raise exception 'account catalog: % orphan parent reference(s)', v_orphans;
  end if;

  select count(*) into v_templates from public.account_templates where is_active;
  raise notice 'business-aware COA installed: % active templates, % catalog accounts, % optional bundles',
    v_templates,
    (select count(*) from public.account_catalog),
    (select count(*) from public.account_catalog_groups);
end $$;