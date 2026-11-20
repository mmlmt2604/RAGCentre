-- Schema version 2: async parse jobs in flight (parser protocol 1.1).
-- A worker that dies or restarts mid-parse resumes polling the stored job instead of submitting
-- the document again. The key is the parse cache key (provider, options and content).

CREATE TABLE parse_jobs (
  cache_key TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
