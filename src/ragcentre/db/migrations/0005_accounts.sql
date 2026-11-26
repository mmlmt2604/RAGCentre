-- Single-user account, browser sessions and named API keys.
-- `users.is_platform_admin` is always 1 in this slice: it is the seam for multi-user roles.
-- `api_keys.team_id` is NULL (global) now; multi-team adds `teams` and a foreign key here.
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  is_platform_admin INTEGER NOT NULL DEFAULT 1,
  auth_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE local_credentials (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  password_hash TEXT NOT NULL,
  password_changed_at TEXT NOT NULL
);

CREATE TABLE user_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  auth_version INTEGER NOT NULL,
  csrf_secret TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX user_sessions_user ON user_sessions (user_id);
CREATE INDEX user_sessions_expires ON user_sessions (expires_at);

CREATE TABLE api_keys (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('read', 'full')),
  token_hash TEXT NOT NULL UNIQUE,
  team_id TEXT,
  created_by_user_id TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  last_used_at TEXT
);
CREATE INDEX api_keys_team ON api_keys (team_id);
