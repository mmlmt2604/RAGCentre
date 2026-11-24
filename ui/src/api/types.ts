// API data contract — mirrors IMPLEMENTATION_PLAN.md section 22.
// Change this file only together with that section and the mock (see CONTRACT_CHANGES.md).

export type UUID = string;
export type ISODate = string;                       // UTC, ISO-8601
export type Scalar = string | number | boolean;
export type Metric = "cosine" | "l2" | "dot";
export type FieldType = "str" | "int" | "float" | "bool";

export interface ApiError { error: { code: string; message: string; details?: Record<string, unknown> } }
export interface Page<T> { items: T[]; next_cursor: string | null }

export interface Health {
  status: "ok"; version: string; migration: number; workers: number;
  auth_required: boolean; secret_storage_enabled: boolean;
}

// ---------- Providers ----------
export type ProviderKind = "parser" | "chunker" | "embedder" | "reranker" | "vector_store" | "llm";

export interface StoreCapabilities {
  metrics: Metric[]; max_batch: number; multi_vector: boolean; native_hybrid: boolean;
  max_dim: number | null; consistency: "immediate" | "eventual";
  filter_exact: boolean;                     // false: a filtered search may return fewer than k matches
  max_collections: number | null;
}

export interface ProviderType {
  kind: ProviderKind; type: string; label: string; description: string;
  config_schema: Record<string, unknown>;    // JSON Schema for `config`
  fingerprint_fields: string[];              // keys of config that are permanent once created
  secret_keys: string[];                     // e.g. ["api_key"]
  capabilities: StoreCapabilities | null;    // vector stores only; the type's best case
}

export interface Provider {
  id: UUID; name: string; kind: ProviderKind; type: string;
  config: Record<string, unknown>;           // never contains secrets
  fingerprint: string;
  secrets: Record<string, { source: "env" | "stored"; set: boolean; env_var: string | null }>;
  builtin: boolean;
  used_by_pipelines: number;                 // non-archived pipelines referencing it
  created_at: ISODate; updated_at: ISODate;
}
export interface ProviderCreate { name: string; kind: ProviderKind; type: string; config: Record<string, unknown> }
export interface ProviderPatch { config: Record<string, unknown> }   // merged into the current config
export type SecretSet = { env: string } | { value: string };

export interface ProviderTestResult {
  ok: boolean; latency_ms: number; message: string | null;
  service: { name: string; version: string; mime_types: string[] } | null;  // parsers, chunkers
  dimensions: number | null;                 // embedders
  version_drift: { pinned: string; current: string } | null;
  check?: ParserCheck | StoreCheck | null;   // only a deep test (`?deep=true`)
}
export type OcrState = "looking" | "detected" | "checking" | "ready" | "failed" | "engine_down" | "needs_key" | "changed" | "offline" | "no_ocr";
export type OcrStep = "ok" | "failed" | "running" | "unknown";
export interface OcrService {
  id: string;
  provider_id: UUID | null;                  // null: found, not added yet
  provider_name: string | null;
  suggested_name: string | null;             // a free provider name, for one that was found
  base_url: string; name: string | null; version: string | null;
  state: OcrState;                           // "ready" only after a scanned page's text was read back
  steps: { shim: OcrStep; engine: OcrStep; text: OcrStep };
  busy: boolean;                             // slow to answer, as an engine at work is
  rechecking: boolean;                       // a test is running and `state` is still the last result
  message: string; detail: string | null;
  checked_at: ISODate | null; check_seconds: number | null;
}
export interface OcrStatus { services: OcrService[]; scanning: boolean; discovery_enabled: boolean; in_container: boolean }
export interface ParserFinding { level: "error" | "warn" | "info"; check: string; message: string; lost: string | null }
export interface ParserCheck { ok: boolean; blocks: number; pages: number; seconds: number; findings: ParserFinding[] }
export interface StoreFinding { level: "error" | "warn" | "info"; check: string; message: string; ms: number; lost: string | null }
export interface StoreCheck { ok: boolean; store_type: string; collection: string; records: number; metrics: string[]; seconds: number; findings: StoreFinding[] }

// ---------- Corpora ----------
export interface MetadataSchema { fields: Record<string, { type: FieldType; default: Scalar }> }
export type PipelineState = "building" | "ready" | "degraded";
export interface CorpusCreate { name: string; metadata_schema?: MetadataSchema }
export interface CorpusPatch { name: string }                        // PATCH /corpora/{id}: rename

export interface PipelineSummary {
  id: UUID; name: string; fingerprint: string; archived: boolean;
  status: PipelineState; coverage: number;   // 0..1
  created_at: ISODate;
}
export interface Corpus {
  id: UUID; name: string; metadata_schema: MetadataSchema;
  active_pipeline_id: UUID | null; document_count: number;
  pipelines: PipelineSummary[];              // non-archived only
  created_at: ISODate;
}

// ---------- Documents ----------
export type RunState = "pending" | "running" | "succeeded" | "failed" | "blocked";
// "blocked" is derived: a pending run whose upstream run has failed.

export interface DocumentIngestion {
  pipeline_id: UUID;
  state: RunState;                           // failed if any node failed; else running if any
                                             // running; else succeeded if all succeeded; else pending
  nodes: { node_id: string; node_fp: string; run_id: number; status: RunState;
           error_code: string | null }[];    // in graph order
}
export interface Document {
  id: UUID; corpus_id: UUID; external_id: string | null;
  filename: string; mime_type: string; size_bytes: number; content_sha256: string;
  metadata: Record<string, Scalar>; attributes: Record<string, unknown>;
  revision: number; status: "active" | "deleting";
  ingestion: DocumentIngestion[];
  created_at: ISODate; updated_at: ISODate;
}
export interface Chunk {
  id: UUID; document_id: UUID; ordinal: number;
  text: string; context: string | null;
  enrichment: string | null;                 // what an enrich node wrote for the chunk, if one ran
  page_start: number | null; page_end: number | null;
  heading_path: string[]; block_ids: string[]; char_count: number;
}
// GET /documents/{id}/parsed returns the Document IR of section 7 plus `heading_path` on each block.

// ---------- Pipelines ----------
export type EnrichMode = "situate" | "summary" | "keywords" | "questions";
export type PipelineNode =
  | { id: string; type: "parse";
      routes: { mime: string; provider: string; options?: Record<string, unknown> }[] }
  | { id: string; type: "chunk"; inputs: [string];
      strategy: "fixed" | "recursive" | "structure" | "page" | "http";
      options?: Record<string, unknown>; provider?: string }
  | { id: string; type: "enrich"; inputs: [string]; provider: string;
      options?: { mode?: EnrichMode; prompt?: string; max_doc_chars?: number;
                  max_tokens?: number; temperature?: number } }
  | { id: string; type: "embed"; inputs: [string]; provider: string }
  | { id: string; type: "index_vector"; inputs: [string]; provider: string;
      options?: { metric?: Metric } }
  | { id: string; type: "index_fts"; inputs: [string];
      options?: { tokenizer?: "unicode61" | "trigram" } };
// `provider` values are provider names.

export type TransformMode = "rewrite" | "multi_query" | "hyde" | "decompose";
// Turns the query into the probes that are searched, with a language model ("llm" provider).
export interface QueryTransformConfig {
  mode: TransformMode; provider: string;
  n?: number;                       // up to 8, default 3; rewrite always makes one
  include_original?: boolean;       // default true
  original_weight?: number;         // 0–10, default 1
  k?: number;                       // RRF k when merging probes, default 60
  instructions?: string;            // appended to the built-in prompt, up to 2000 characters
}

export interface RetrievalConfig {
  transform?: QueryTransformConfig;
  sources: { id: string; index: string; top_k: number }[];
  fusion?: { method: "rrf" | "weighted"; k?: number; weights?: Record<string, number> };
  rerank?: { provider: string; top_n: number };
  expand?: { neighbors: number };
  top_k: number;
}
export interface PipelineConfig { name: string; nodes: PipelineNode[]; retrieval: RetrievalConfig }

export interface PipelineProblem { node_id: string | null; path: string; message: string }
// A real create that fails validation returns 422 with code "invalid_pipeline"
// and details: { problems: PipelineProblem[] }.

export interface PipelinePlan {                     // POST …/pipelines?dry_run=true, always 200
  valid: boolean;
  problems: PipelineProblem[];
  pipeline_fingerprint: string | null;       // null when invalid
  nodes: { node_id: string; type: string; node_fp: string;
           reused: boolean;                  // an identical node already exists on this corpus
           shared_with: UUID[];              // pipelines that have it
           runs_to_create: number }[];
  total_runs_to_create: number;
}

export interface Pipeline extends PipelineSummary {
  corpus_id: UUID; config: PipelineConfig; is_active: boolean;
  nodes: { node_id: string; type: string; node_fp: string; shared_with: UUID[] }[];
}
export interface PipelineStatus {
  pipeline_id: UUID; status: PipelineState; coverage: number; document_count: number;
  nodes: { node_id: string; type: string; node_fp: string;
           counts: Record<RunState, number> }[];
  recent_failures: NodeRun[];                // newest first, at most 20
}
export interface ConfigChange { path: string; a: unknown; b: unknown }
export interface PipelineDiff {
  a: UUID; b: UUID;
  nodes: { node_id: string; type: string;
           in_a: boolean; in_b: boolean;
           same: boolean;                    // same node fingerprint
           changes: ConfigChange[] }[];      // paired by node_id
  retrieval_same: boolean; retrieval_changes: ConfigChange[];
}

// ---------- Node runs ----------
export interface NodeRun {
  id: number; corpus_id: UUID; document_id: UUID; document_filename: string;
  node_fp: string; node_type: string;
  pipelines: { pipeline_id: UUID; pipeline_name: string; node_id: string }[];
  status: RunState; attempt: number; max_attempts: number;
  blocked_by_run_id: number | null;
  error_code: string | null; error_message: string | null;
  stats: Record<string, unknown> | null;
  run_after: ISODate; started_at: ISODate | null; finished_at: ISODate | null;
}

// ---------- Query ----------
export type Filter = Record<string, unknown>;       // the filter language of section 10.2

export interface QueryRequest {
  query: string; pipeline_id?: UUID; top_k?: number;
  filter?: Filter; allow_partial?: boolean; debug?: boolean;
  transform?: boolean | null;     // false skips the pipeline's query transform for this request
}
export interface QueryResult {
  chunk_id: UUID; document_id: UUID; score: number; rank: number;
  text: string; context: string | null;
  enrichment: string | null;                 // null without an enrich node
  page_start: number | null; page_end: number | null; heading_path: string[];
  document: { filename: string; external_id: string | null;
              metadata: Record<string, Scalar>; attributes: Record<string, unknown> };
  context_before: string[]; context_after: string[];
  sources: Record<string, { rank: number; score: number }>;   // keyed by source id
  probes: number[] | null;                   // probe indexes that found it; transformed query with debug only
}
export type ProbeKind = "original" | "rewrite" | "paraphrase" | "hypothetical" | "sub_question";
export interface QueryProbe { index: number; text: string; kind: ProbeKind }
export interface QueryTransformInfo { mode: TransformMode; probes: QueryProbe[]; cached: boolean }
export interface QueryResponse {
  pipeline_id: UUID; pipeline_fingerprint: string; partial: boolean;
  enriched_sources: string[];                // source ids whose index holds the enriched text
  results: QueryResult[];
  timings_ms: Record<string, number>;        // always includes "total"; "transform" when one ran
  transform: QueryTransformInfo | null;      // null without a transform or when the request skipped it
}

// ---------- Evaluation ----------
export interface EvalTarget { document_id: UUID; document_label: string;
                       pages: number[] | null; text_contains: string | null }
export interface EvalItem { id: UUID; question: string; targets: EvalTarget[];
                     filter: Filter | null; metadata: Record<string, unknown> }
export interface EvalDataset { id: UUID; corpus_id: UUID; name: string; item_count: number; created_at: ISODate }
// A failed import returns 422 with details: { errors: { line: number; message: string }[] }.

export interface EvalRunCreate { dataset_id: UUID; pipeline_ids: UUID[]; k_values?: number[]; allow_partial?: boolean }
export interface EvalRun {
  id: UUID; corpus_id: UUID; dataset_id: UUID; dataset_name: string;
  pipeline_ids: UUID[]; k_values: number[];
  status: "pending" | "running" | "finished" | "failed";
  progress: { done: number; total: number };
  item_count: number;                        // questions in the dataset
  created_at: ISODate; finished_at: ISODate | null;
}
export interface EvalSummary {
  pipeline_id: UUID; pipeline_name: string; pipeline_fingerprint: string; pipeline_archived: boolean;
  metrics: Record<string, number>;           // keys: "hit_rate@5", "recall@5", "precision@5", "ndcg@5", "mrr", …
  latency_ms: { p50: number; p95: number };
  error_count: number; chunk_count: number; mean_chunk_chars: number;
}
export interface EvalComparison {
  a: UUID; b: UUID; metric: string;
  mean_a: number; mean_b: number; diff: number;   // diff = mean_b − mean_a
  ci_low: number; ci_high: number; significant: boolean;
}
export interface EvalRunDetail extends EvalRun {
  summaries: EvalSummary[];                  // empty until finished
  comparisons: EvalComparison[];             // every ordered pair with a < b in pipeline_ids order
}
export interface EvalItemResult {
  pipeline_id: UUID; metrics: Record<string, number>;
  latency_ms: number | null; error: string | null;
  retrieved: { rank: number; chunk_id: UUID; document_id: UUID; document_label: string;
               text: string; page_start: number | null; page_end: number | null;
               matched_targets: number[] }[];   // indexes into item.targets
}
export interface EvalItemComparison {               // GET /eval/runs/{id}/items?a=&b=&metric=&only=
  item: EvalItem; a: EvalItemResult; b: EvalItemResult;
  outcome: "improved" | "regressed" | "unchanged";   // b relative to a, on the chosen metric
}

// ---------- Admin ----------
export interface GcRequest { dry_run: boolean }
export interface GcReport {
  dry_run: boolean;
  indexes: { id: UUID; kind: "vector" | "fts"; physical_name: string; corpus_id: UUID }[];
  node_count: number; blob_count: number; blob_bytes: number;
}
