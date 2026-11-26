-- Purpose-bound credentials. Raw values are returned once and never persisted.
CREATE TABLE team_invitations (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  issuer_user_id TEXT NOT NULL REFERENCES users(id),
  issuer_auth_version INTEGER NOT NULL CHECK (issuer_auth_version >= 0),
  target_auth_version INTEGER NOT NULL CHECK (target_auth_version >= 0),
  role TEXT NOT NULL CHECK (role IN ('viewer', 'editor', 'owner')),
  kind TEXT NOT NULL CHECK (kind IN ('new_account', 'existing_account')),
  created_user_id TEXT REFERENCES users(id),
  token_hash TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  accepted_at TEXT,
  revoked_at TEXT,
  CHECK ((kind='new_account' AND created_user_id IS NOT NULL AND created_user_id=user_id) OR (kind='existing_account' AND created_user_id IS NULL)),
  CHECK (accepted_at IS NULL OR revoked_at IS NULL)
);
CREATE UNIQUE INDEX invitation_live_target ON team_invitations(team_id, user_id)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX invitations_target ON team_invitations(user_id);
CREATE INDEX invitations_issuer ON team_invitations(issuer_user_id);
CREATE INDEX invitations_team ON team_invitations(team_id, created_at, id);
CREATE INDEX invitations_expiry ON team_invitations(expires_at);

CREATE TABLE password_recovery_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  issuer_user_id TEXT NOT NULL REFERENCES users(id),
  issuer_auth_version INTEGER NOT NULL CHECK (issuer_auth_version >= 0),
  target_auth_version INTEGER NOT NULL CHECK (target_auth_version >= 0),
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT,
  revoked_at TEXT,
  CHECK (consumed_at IS NULL OR revoked_at IS NULL)
);
CREATE UNIQUE INDEX recovery_live_target ON password_recovery_tokens(user_id)
  WHERE consumed_at IS NULL AND revoked_at IS NULL;
CREATE INDEX recovery_issuer ON password_recovery_tokens(issuer_user_id);
CREATE INDEX recovery_expiry ON password_recovery_tokens(expires_at);

CREATE TABLE installation_claims (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT,
  revoked_at TEXT,
  CHECK (consumed_at IS NULL OR revoked_at IS NULL)
);
CREATE UNIQUE INDEX installation_live ON installation_claims((1))
  WHERE consumed_at IS NULL AND revoked_at IS NULL;
CREATE INDEX installation_expiry ON installation_claims(expires_at);
