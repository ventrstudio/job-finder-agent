-- Resume versioning for the Job Scout bot.
-- The active row's content is mirrored into agent_profile.resume_text on every
-- change, so scoring (score_jobs.py) and cover letters keep reading resume_text
-- unchanged. A serverless bot can't git push, so this table IS the version store;
-- RESUME.md in the repo root is the human-readable export of the active version.
-- Applied to project zizpvwkqcuxiajqxvypk (Burgos org) via MCP on 2026-06-17.

create table if not exists public.resume_versions (
  id uuid primary key default gen_random_uuid(),
  version_no int not null,
  content text not null,
  note text,
  is_active boolean not null default false,
  created_at timestamptz not null default now()
);

create unique index if not exists resume_versions_version_no_key
  on public.resume_versions(version_no);

-- at most one active version at a time
create unique index if not exists resume_versions_one_active
  on public.resume_versions(is_active) where is_active;

-- seed v1 from the current resume (idempotent: only when the table is empty)
insert into public.resume_versions (version_no, content, note, is_active)
select 1, resume_text, 'Initial import from agent_profile.resume_text', true
from public.agent_profile
where not exists (select 1 from public.resume_versions)
limit 1;
