-- Schema version 6: sync sources and source items (data connectors).
-- sync_sources inherits its team through corpus_id (no team column here). New tables only.

CREATE TABLE sync_sources (
  id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL REFERENCES providers(id),   -- a kind='connector' provider
  name TEXT NOT NULL,
  scope TEXT NOT NULL,                                  -- JSON: connector-specific target
  schedule_seconds INTEGER NOT NULL,                    -- poll interval; 0 = manual only
  cursor TEXT,                                          -- opaque connector cursor (JSON string)
  status TEXT NOT NULL CHECK (status IN ('idle','syncing','error')),
  next_run_at TEXT,                                     -- NULL = manual only, or not yet due
  last_run_at TEXT,
  last_error TEXT,
  metadata_template TEXT NOT NULL DEFAULT '{}',         -- JSON merged into document metadata
  attributes_template TEXT NOT NULL DEFAULT '{}',       -- JSON merged into document attributes
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX sync_sources_corpus ON sync_sources (corpus_id);
CREATE INDEX sync_sources_provider ON sync_sources (provider_id);
CREATE UNIQUE INDEX sync_sources_corpus_name ON sync_sources (corpus_id, name);

CREATE TABLE source_items (
  source_id TEXT NOT NULL REFERENCES sync_sources(id) ON DELETE CASCADE,
  remote_id TEXT NOT NULL,
  remote_version TEXT NOT NULL,
  document_id TEXT,                                     -- NULL while not yet ingested / after delete
  remote_path TEXT,
  remote_mime TEXT,
  remote_size INTEGER,
  remote_modified TEXT,
  skipped_reason TEXT,                                  -- 'too_large', etc.
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (source_id, remote_id)
);
CREATE INDEX source_items_doc ON source_items (document_id);
