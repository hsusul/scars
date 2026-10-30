create table repos (
  id text primary key,
  display_name text not null,
  owner_class text not null check (owner_class in ('own','upstream'))
);
create table clones (
  common_dir text primary key,                 -- absolute path from git rev-parse --git-common-dir
  repo_id text not null references repos(id),
  registered_at real not null
);
create table lessons (
  id text primary key,                         -- 'L-' + 8 random hex, stable across edits
  revision integer not null default 1,         -- compare-and-set key
  status text not null check (status in ('candidate','provisional','promoted','retired','superseded')),
  level text not null default 'note' check (level in ('note','warn','block')),
  pinned integer not null default 0 check (pinned in (0,1)),
  scope_kind text not null check (scope_kind in ('machine','repo','path','session')),
  repo_id text references repos(id),
  path_glob text,
  session_id text,                             -- set only while scope_kind = 'session'
  avoid text not null check (length(avoid) <= 240),
  instead text not null check (length(instead) <= 240),
  reason text not null check (length(reason) <= 240),
  trust_class text not null check (trust_class in ('own_code','third_party_repo','web','human')),
  fingerprint text not null,                   -- sha256 of scope and error signature, not command text
  sessions_seen integer not null default 1,
  fix_programs text not null default '[]',     -- JSON list of distinct programs used in the passing call
  precision_ok integer not null default 0,     -- 1 when the last scars test passed (cleared on edit)
  created_at real not null, updated_at real not null, last_seen_at real not null,
  expires_at real,                             -- provisional and candidate only
  superseded_by text references lessons(id),
  check (level <> 'block' or status = 'promoted'),
  check (pinned = 0 or status = 'promoted'),
  check (scope_kind <> 'machine' or status in ('promoted','retired','superseded'))
);
create index lessons_active on lessons(status, scope_kind, repo_id);
create unique index lessons_fingerprint on lessons(fingerprint) where status in ('candidate','provisional','promoted');

create table triggers (
  lesson_id text not null references lessons(id),
  kind text not null check (kind in ('command','path','error','prompt')),
  spec text not null,                          -- grammar in "Matching"
  stage text not null check (stage in ('plan','gate','repair')),
  primary key (lesson_id, kind, spec, stage)
);
create index triggers_kind on triggers(kind, stage);

create table observations (                    -- one row per mined failure-then-fix pair
  id integer primary key,
  lesson_id text not null references lessons(id),
  harness text not null, session_id text not null, failed_tool_use_id text not null unique,
  failed_input text not null,                  -- after secret scan, capped at 1 KB
  error_signature text not null,
  passing_input text not null,                 -- capped at 1 KB
  at real not null
);

create table events (                          -- audit log, read by scars why and scars doctor
  seq integer primary key,
  lesson_id text references lessons(id),
  type text not null,                          -- an EventType value
  actor text not null,                         -- 'human' | 'miner' | 'agent:<harness>:<session>'
  from_revision integer, to_revision integer,
  payload text not null,                       -- canonical JSON of the change
  prev_hash text not null,                     -- sha256 of the previous row's canonical JSON
  at real not null
);

create table miner_offsets (path text primary key, offset integer not null, inode integer not null, mtime real not null);
create table seen_tool_use (tool_use_id text primary key);   -- copied-history dedupe across batches
create table pending_failures (                -- failures still waiting for a fix across batch boundaries
  tool_use_id text primary key, harness text not null, session_id text not null,
  tool text not null, program text, input text not null, error_signature text not null,
  calls_since integer not null default 0, last_call_at real not null
);
create table if not exists mined_calls (       -- the last 30 days of mined tool calls, for the precision check and outcomes
  seq integer primary key,                     -- transcript order within a session
  tool_use_id text not null unique, harness text not null, session_id text not null, at real not null,
  tool text not null, tool_kind text not null, command text,   -- command after secret scan, else null
  paths text not null default '[]', status text not null check (status in ('ok','failed','unknown')),
  error_signature text, cwd text not null, model_id text
);
create index if not exists mined_calls_session on mined_calls(session_id, seq);
create table precision_runs (lesson_id text not null, revision integer not null, matched_failing integer not null,
                             matched_passing integer not null, window_days integer not null, at real not null,
                             primary key (lesson_id, revision));
