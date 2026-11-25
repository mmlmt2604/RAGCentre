-- Local accounts are additive: corpus, provider, pipeline and token ownership is unchanged.
CREATE TABLE auth_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  local_enabled INTEGER NOT NULL DEFAULT 0 CHECK (local_enabled IN (0, 1)),
  activated_at TEXT
);
INSERT INTO auth_state (id) VALUES (1);

CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'disabled')),
  is_platform_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_platform_admin IN (0, 1)),
  auth_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE local_credentials (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  password_hash TEXT NOT NULL,
  password_changed_at TEXT NOT NULL
);
CREATE TABLE team_memberships (
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('viewer', 'editor', 'owner')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (team_id, user_id)
);
CREATE INDEX memberships_user ON team_memberships(user_id, team_id);
CREATE TABLE user_sessions (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  auth_version INTEGER NOT NULL,
  csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX sessions_user ON user_sessions(user_id);
CREATE INDEX sessions_expiry ON user_sessions(expires_at);
CREATE TABLE account_setup_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE INDEX setup_tokens_user ON account_setup_tokens(user_id);
CREATE INDEX setup_tokens_expiry ON account_setup_tokens(expires_at);
CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  actor_id TEXT,
  team_id TEXT,
  action TEXT NOT NULL,
  target_id TEXT,
  outcome TEXT NOT NULL DEFAULT 'success'
);
CREATE INDEX audit_time ON audit_events(created_at);
CREATE INDEX audit_team ON audit_events(team_id, created_at);
