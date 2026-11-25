-- Schema version 5: teams, team-owned corpora and providers, team service tokens.
-- No table rebuilds here: only new tables and ADD COLUMN (the migration runner wraps each
-- migration in BEGIN IMMEDIATE with PRAGMA foreign_keys=ON, so a parent-table rebuild would
-- cascade or fail).

CREATE TABLE teams (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,            -- ^[a-z][a-z0-9-]{0,62}$; the future OIDC group key
  created_at TEXT NOT NULL
);

-- One default team. Existing corpora and providers are backfilled into it.
INSERT INTO teams (id, name, slug, created_at)
VALUES ('00000000-0000-0000-0000-000000000001', 'Default', 'default',
        '2026-11-25T00:00:00.000000Z');

-- Every corpus has exactly one owning team. NOT NULL with a default so existing rows land in
-- `default` without a rebuild. The application always sets team_id explicitly on new rows.
ALTER TABLE corpora ADD COLUMN team_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'
  REFERENCES teams(id);

-- NULL = system/built-in provider only (builtin-text); non-NULL = owned by that team.
ALTER TABLE providers ADD COLUMN team_id TEXT REFERENCES teams(id);

-- Backfill pre-existing user providers into the default team. Corpora got their DEFAULT above;
-- providers have none, so without this every pre-upgrade provider would stay NULL and read as
-- system/built-in (visible to every team, admin-only to change). The built-in keeps NULL.
UPDATE providers SET team_id = '00000000-0000-0000-0000-000000000001'
WHERE team_id IS NULL AND name != 'builtin-text';

CREATE TABLE team_tokens (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('full','read')),
  name TEXT NOT NULL,                   -- label shown in the UI, e.g. "CI read token"
  token_hash TEXT NOT NULL UNIQUE,      -- sha256 hex of the opaque token
  created_at TEXT NOT NULL,
  last_used_at TEXT
);
CREATE INDEX team_tokens_team ON team_tokens (team_id);
