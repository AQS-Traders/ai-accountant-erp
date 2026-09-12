-- The onboarding form offers business types that did not exist in the
-- business_type_code enum; selecting any of them made create_organization
-- fail with "invalid input value for enum" (HTTP 400 → "Failed to create
-- organization").  Align the enum with the product's business-type list.
ALTER TYPE public.business_type_code ADD VALUE IF NOT EXISTS 'CONSULTING';
ALTER TYPE public.business_type_code ADD VALUE IF NOT EXISTS 'E_COMMERCE';
ALTER TYPE public.business_type_code ADD VALUE IF NOT EXISTS 'MANUFACTURING';
ALTER TYPE public.business_type_code ADD VALUE IF NOT EXISTS 'TRADING';
ALTER TYPE public.business_type_code ADD VALUE IF NOT EXISTS 'CONSTRUCTION';
ALTER TYPE public.business_type_code ADD VALUE IF NOT EXISTS 'HEALTHCARE';
ALTER TYPE public.business_type_code ADD VALUE IF NOT EXISTS 'EDUCATION';