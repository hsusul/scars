create table hits (
  seq integer primary key, at real not null,
  harness text not null, harness_version text, model_id text,   -- model_id may be null and is backfilled by the miner
  session_id text not null, tool_use_id text, event text not null, stage text,
  lesson_id text, lesson_revision integer, status_at_injection text, trigger_key text,
  outcome text not null check (outcome in ('shown','blocked','no_match','log_only','error')),
  injected_chars integer, latency_ms real, error_class text, arm text
);
create index hits_session on hits(session_id, at);
create index if not exists hits_at on hits(at);                    -- doctor's 24 h window, the 30-day backfill
create index if not exists hits_lesson on hits(lesson_id, at);     -- scars why, ls --firing

create table outcomes (                        -- delayed outcomes, computed by the miner
  hit_seq integer primary key,
  outcome text not null check (outcome in ('heeded','ignored_then_failed','fired_unneeded','no_attempt','unknown'))
);

create table shown (                           -- advisory deduplication only
  session_id text not null, context_epoch integer not null, lesson_id text not null, trigger_key text not null,
  primary key (session_id, context_epoch, lesson_id, trigger_key)
);
create table session_epoch (session_id text primary key, context_epoch integer not null default 0);
create table cwd_repo (cwd text primary key, repo_id text, common_dir text, checked_at real not null);
