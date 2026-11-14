-- RAGCentre schema version 1 (IMPLEMENTATION_PLAN.md section 11).
-- Applied by ragcentre.db.migrate; the version is stored in PRAGMA user_version.

CREATE TABLE providers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,              -- ^[a-z][a-z0-9-]{0,62}$
  kind TEXT NOT NULL,
  type TEXT NOT NULL,
  config TEXT NOT NULL,                   -- JSON, no secrets
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE provider_secrets (
  provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
  key TEXT NOT NULL,                      -- e.g. api_key, dsn
  source TEXT NOT NULL CHECK (source IN ('env','stored')),
  value BLOB NOT NULL,                    -- env var name, or Fernet ciphertext
  PRIMARY KEY (provider_id, key)
);

CREATE TABLE corpora (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  metadata_schema TEXT NOT NULL,          -- JSON
  active_pipeline_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES corpora(id),
  external_id TEXT,                       -- user-supplied, unique per corpus
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,
  metadata TEXT NOT NULL,                 -- JSON, schema-validated, filterable
  attributes TEXT NOT NULL,               -- JSON, free-form
  revision INTEGER NOT NULL DEFAULT 1,    -- bumped when content is replaced
  status TEXT NOT NULL CHECK (status IN ('active','deleting')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (corpus_id, external_id)
);

CREATE TABLE pipelines (
  id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES corpora(id),
  name TEXT NOT NULL,
  config TEXT NOT NULL,                   -- JSON as submitted
  resolved TEXT NOT NULL,                 -- JSON with provider ids and fingerprints
  fingerprint TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (corpus_id, name)
);

CREATE TABLE nodes (
  fingerprint TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  resolved TEXT NOT NULL,                 -- JSON resolved config
  created_at TEXT NOT NULL
);
CREATE TABLE node_edges (
  node_fp TEXT NOT NULL, upstream_fp TEXT NOT NULL, position INTEGER NOT NULL,
  PRIMARY KEY (node_fp, upstream_fp)
);
CREATE TABLE pipeline_nodes (
  pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
  node_id TEXT NOT NULL,                  -- the id used inside the pipeline config
  node_fp TEXT NOT NULL REFERENCES nodes(fingerprint),
  PRIMARY KEY (pipeline_id, node_id)
);

CREATE TABLE node_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  corpus_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  node_fp TEXT NOT NULL,
  doc_revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed')),
  priority INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0,
  run_after TEXT NOT NULL,
  lease_expires_at TEXT,
  worker_id TEXT,
  error_code TEXT, error_message TEXT,
  stats TEXT,                             -- JSON
  started_at TEXT, finished_at TEXT,
  UNIQUE (document_id, node_fp)
);
CREATE INDEX node_runs_claim ON node_runs (status, run_after);
CREATE INDEX node_runs_node ON node_runs (corpus_id, node_fp, status);

CREATE TABLE parse_cache (
  cache_key TEXT PRIMARY KEY,
  blob_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE chunks (
  id TEXT PRIMARY KEY,
  chunk_node_fp TEXT NOT NULL,
  document_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  context TEXT,
  page_start INTEGER, page_end INTEGER,
  heading_path TEXT NOT NULL,             -- JSON array
  block_ids TEXT NOT NULL,                -- JSON array
  metadata TEXT NOT NULL,                 -- JSON
  UNIQUE (chunk_node_fp, document_id, ordinal)
);

CREATE TABLE indexes (
  id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL,
  node_fp TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('vector','fts')),
  provider_id TEXT,                       -- vector indexes only
  physical_name TEXT NOT NULL,
  dim INTEGER, metric TEXT,               -- dim set by the first index run
  created_at TEXT NOT NULL,
  UNIQUE (corpus_id, node_fp)
);
CREATE TABLE index_records (
  index_id TEXT NOT NULL, document_id TEXT NOT NULL, record_id TEXT NOT NULL,
  PRIMARY KEY (index_id, record_id)
);
CREATE INDEX index_records_doc ON index_records (index_id, document_id);

CREATE TABLE tasks (                      -- maintenance work outside the node graph
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                     -- delete_document | drop_index | eval_run
  payload TEXT NOT NULL,                  -- JSON
  status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
  run_after TEXT NOT NULL, lease_expires_at TEXT, worker_id TEXT,
  error_message TEXT, created_at TEXT NOT NULL, finished_at TEXT
);

CREATE TABLE eval_datasets (
  id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL, name TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE (corpus_id, name)
);
CREATE TABLE eval_items (
  id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES eval_datasets(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  targets TEXT NOT NULL,                  -- JSON list, see section 14
  filter TEXT,                            -- JSON, optional
  metadata TEXT NOT NULL
);
CREATE TABLE eval_runs (
  id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
  config TEXT NOT NULL,                   -- JSON: pipeline_ids, k_values
  status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE eval_results (
  run_id TEXT NOT NULL, pipeline_id TEXT NOT NULL, item_id TEXT NOT NULL,
  retrieved TEXT, metrics TEXT, latency_ms REAL, error TEXT,
  PRIMARY KEY (run_id, pipeline_id, item_id)
);
CREATE TABLE eval_summaries (
  run_id TEXT NOT NULL, pipeline_id TEXT NOT NULL, pipeline_fp TEXT NOT NULL,
  metrics TEXT NOT NULL,
  PRIMARY KEY (run_id, pipeline_id)
);

-- Supporting indexes for common lookups (not in section 11; purely additive).
CREATE INDEX documents_corpus ON documents (corpus_id, status);
CREATE INDEX tasks_claim ON tasks (status, run_after);
CREATE INDEX eval_items_dataset ON eval_items (dataset_id);
CREATE INDEX eval_runs_dataset ON eval_runs (dataset_id);
