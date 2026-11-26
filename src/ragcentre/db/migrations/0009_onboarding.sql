-- Personal setup preferences are separate from team readiness. No credentials belong here.
CREATE TABLE onboarding_drafts (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  draft TEXT NOT NULL CHECK (json_valid(draft)),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, team_id)
);
CREATE TABLE onboarding_operations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  input_version INTEGER NOT NULL CHECK (input_version >= 1),
  input_hash TEXT NOT NULL,
  draft TEXT NOT NULL CHECK (json_valid(draft)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (user_id, team_id, idempotency_key)
);
CREATE INDEX onboarding_operations_actor ON onboarding_operations(user_id, team_id, created_at);
CREATE TABLE onboarding_steps (
  operation_id TEXT NOT NULL REFERENCES onboarding_operations(id) ON DELETE CASCADE,
  step TEXT NOT NULL CHECK (step IN ('parser', 'embedder', 'store', 'corpus', 'pipeline', 'active', 'check_services')),
  state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
  resource_id TEXT,
  claim_id TEXT,
  lease_expires_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  error_code TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (operation_id, step)
);
CREATE TABLE onboarding_provider_checks (
  provider_id TEXT PRIMARY KEY REFERENCES providers(id) ON DELETE CASCADE,
  config_hash TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  ok INTEGER NOT NULL CHECK (ok IN (0, 1))
);
CREATE TABLE onboarding_first_results (
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  corpus_id TEXT NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
  pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  pipeline_fingerprint TEXT NOT NULL,
  succeeded_at TEXT NOT NULL,
  PRIMARY KEY (team_id, corpus_id, pipeline_id, pipeline_fingerprint)
);
