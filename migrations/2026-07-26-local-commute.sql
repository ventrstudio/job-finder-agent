-- Local-jobs / commute-grading columns for the jobs table (07-26-2026).
-- Adds: search scope tier + commute estimate so the digest can rank and label
-- local roles. Idempotent — safe to re-run. Apply manually in Supabase.
alter table public.jobs
  add column if not exists search_scope text,
  add column if not exists commute_min numeric,
  add column if not exists commute_grade text,
  add column if not exists location_tier int;
