// Synthetic demonstration data. Names and text are invented; nothing here is a real result.
import type { EnrichMode, MetadataSchema, PipelineConfig, Provider, ProviderType, QueryTransformConfig } from "../api/types";
import { fp, rng, uuidFrom } from "./hash";
import { ago, type CorpusRec, type DocRec } from "./store";

const obj = (properties: Record<string, unknown>, required: string[] = []) => ({ type: "object", properties, required });
const str = (description: string, extra: Record<string, unknown> = {}) => ({ type: "string", description, ...extra });
const int = (description: string, extra: Record<string, unknown> = {}) => ({ type: "integer", description, ...extra });
const bool = (description: string) => ({ type: "boolean", description });
const http = { base_url: str("Service URL", { format: "uri" }), timeout_seconds: int("Request timeout", { default: 60 }), max_concurrency: int("Parallel requests", { default: 4 }) };

export const providerTypes: ProviderType[] = [
  {
    kind: "parser", type: "builtin_text", label: "Built-in text", description: "Plain text and Markdown, parsed in-process.",
    config_schema: obj({}), fingerprint_fields: ["impl_version"], secret_keys: [], capabilities: null,
  },
  {
    kind: "parser", type: "http", label: "Parser service (HTTP)", description: "Your own parser or OCR service speaking ragcentre.parser/v1.",
    config_schema: obj({ ...http, revision: str("Optional revision tag"), on_version_drift: str("When the service version changes", { enum: ["fail", "warn"], default: "fail" }) }, ["base_url"]),
    fingerprint_fields: ["service_name", "service_version", "revision"], secret_keys: ["api_key"], capabilities: null,
  },
  {
    kind: "chunker", type: "http", label: "Chunker service (HTTP)", description: "A custom chunking service speaking ragcentre.chunker/v1.",
    config_schema: obj({ ...http, revision: str("Optional revision tag") }, ["base_url"]),
    fingerprint_fields: ["service_name", "service_version", "revision"], secret_keys: ["api_key"], capabilities: null,
  },
  {
    kind: "embedder", type: "openai_compatible", label: "OpenAI-compatible embeddings", description: "Any server exposing POST /embeddings.",
    config_schema: obj({ ...http, model: str("Model name"), dimensions: int("Output dimensions (optional)"), normalize: bool("L2-normalise vectors"), query_prefix: str("Prefix for queries"), document_prefix: str("Prefix for documents"), batch_size: int("Texts per request", { default: 64 }), revision: str("Optional revision tag") }, ["base_url", "model"]),
    fingerprint_fields: ["model", "dimensions", "normalize", "query_prefix", "document_prefix", "revision"], secret_keys: ["api_key"], capabilities: null,
  },
  {
    kind: "embedder", type: "builtin_hash", label: "Built-in hash embedder", description: "Lexical hashing embedder for tests and demos; no model needed.",
    config_schema: obj({ dimensions: int("Vector dimensions", { default: 256 }) }), fingerprint_fields: ["impl_version", "dimensions"], secret_keys: [], capabilities: null,
  },
  {
    kind: "reranker", type: "http", label: "Reranker (HTTP)", description: "Cohere/Jina-style POST /rerank.",
    config_schema: obj({ ...http, model: str("Model name"), revision: str("Optional revision tag") }, ["base_url", "model"]),
    fingerprint_fields: ["model", "revision"], secret_keys: ["api_key"], capabilities: null,
  },
  {
    kind: "llm", type: "openai_compatible", label: "OpenAI-compatible chat completions", description: "A language model server exposing POST /chat/completions.",
    config_schema: obj({
      ...http,
      timeout_seconds: { type: "number", default: 120, exclusiveMinimum: 0, maximum: 3600, description: "Request timeout in seconds" },
      max_concurrency: int("Parallel requests", { default: 4, minimum: 1, maximum: 256 }),
      model: str("Chat model name", { minLength: 1 }),
      temperature: { anyOf: [{ type: "number", minimum: 0, maximum: 2 }, { type: "null" }], default: 0, description: "Sampling temperature; empty to use the server default" },
      top_p: { anyOf: [{ type: "number", minimum: 0, maximum: 1 }, { type: "null" }], default: null, description: "Nucleus sampling probability" },
      seed: { anyOf: [{ type: "integer" }, { type: "null" }], default: null, description: "Sampling seed" },
      max_tokens: int("Maximum output tokens per request", { default: 1024, minimum: 1, maximum: 131072 }),
      token_limit_field: { type: "string", enum: ["max_tokens", "max_completion_tokens"], default: "max_tokens", description: "Request field for the token limit; OpenAI reasoning models need max_completion_tokens" },
      supports_images: { ...bool("Allow PNG, JPEG and WebP input"), default: false },
      extra_body: { type: "object", description: "Additional request parameters as a JSON object", default: {} },
      revision: str("Optional revision tag for the model", { default: "" }),
      max_tokens_per_run: int("Fail a document's enrichment above this many estimated tokens", { minimum: 1 }),
      daily_token_budget: int("Stop for the rest of the UTC day after this many tokens", { minimum: 1 }),
    }, ["base_url", "model"]),
    fingerprint_fields: ["model", "temperature", "top_p", "seed", "max_tokens", "supports_images", "extra_body", "revision"], secret_keys: ["api_key"], capabilities: null,
  },
  {
    kind: "vector_store", type: "sqlite_vec", label: "SQLite (sqlite-vec)", description: "Zero-setup local store; exact search, best under ~100k vectors.",
    config_schema: obj({ path: str("Database file", { default: "data/vectors.db" }) }), fingerprint_fields: [], secret_keys: [],
    capabilities: { metrics: ["cosine", "l2"], max_batch: 1000, multi_vector: false, native_hybrid: false, max_dim: null, consistency: "immediate", filter_exact: true, max_collections: null },
  },
  {
    kind: "vector_store", type: "qdrant", label: "Qdrant", description: "Qdrant server or embedded local mode.",
    config_schema: obj({ url: str("Server URL", { format: "uri" }), path: str("Local path instead of a server") }), fingerprint_fields: [], secret_keys: ["api_key"],
    capabilities: { metrics: ["cosine", "l2", "dot"], max_batch: 256, multi_vector: true, native_hybrid: false, max_dim: null, consistency: "immediate", filter_exact: true, max_collections: null },
  },
  {
    kind: "vector_store", type: "pgvector", label: "PostgreSQL (pgvector)", description: "Tables in your Postgres with an HNSW index.",
    config_schema: obj({ schema: str("Schema", { default: "ragcentre" }), vector_type: str("Vector type", { default: "auto" }), hnsw_m: int("HNSW m") }), fingerprint_fields: ["vector_type", "hnsw_m"], secret_keys: ["dsn"],
    capabilities: { metrics: ["cosine", "l2", "dot"], max_batch: 500, multi_vector: false, native_hybrid: false, max_dim: 16000, consistency: "immediate", filter_exact: true, max_collections: null },
  },
  {
    kind: "vector_store", type: "chroma", label: "Chroma", description: "Chroma server or persistent local client.",
    config_schema: obj({ host: str("Host"), port: int("Port", { default: 8000 }), path: str("Local path instead of a server") }), fingerprint_fields: [], secret_keys: [],
    capabilities: { metrics: ["cosine", "l2", "dot"], max_batch: 1000, multi_vector: false, native_hybrid: false, max_dim: null, consistency: "immediate", filter_exact: true, max_collections: null },
  },
  {
    kind: "vector_store", type: "pinecone", label: "Pinecone", description: "Managed serverless index; eventually consistent.",
    config_schema: obj({ index_name: str("Index name"), prefix: str("Namespace prefix") }, ["index_name"]), fingerprint_fields: [], secret_keys: ["api_key"],
    capabilities: { metrics: ["cosine", "l2", "dot"], max_batch: 1000, multi_vector: false, native_hybrid: false, max_dim: 20000, consistency: "eventual", filter_exact: false, max_collections: null },
  },
];

export function provider(name: string, kind: Provider["kind"], type: string, config: Record<string, unknown>, extra: Partial<Provider> = {}): Provider {
  const t = providerTypes.find((x) => x.kind === kind && x.type === type);
  const fields = Object.fromEntries((t?.fingerprint_fields ?? []).map((f) => [f, config[f] ?? null]));
  const id = uuidFrom(`provider:${name}`);
  return {
    id, name, kind, type, config,
    fingerprint: fp({ kind, type, fields: kind === "vector_store" ? { id } : fields }),
    secrets: Object.fromEntries((t?.secret_keys ?? []).map((k) => [k, { source: "env" as const, set: false, env_var: null }])),
    builtin: false, used_by_pipelines: 0,
    created_at: ago(60 * 24 * 21), updated_at: ago(60 * 24 * 21),
    ...extra,
  };
}

export const builtinText = () => provider("builtin-text", "parser", "builtin_text", { impl_version: 1 }, { builtin: true, created_at: ago(60 * 24 * 30) });

export function allProviders(): Provider[] {
  return [
    builtinText(),
    provider("docling", "parser", "http", { base_url: "http://parse-01:5001", timeout_seconds: 600, max_concurrency: 2, service_name: "docling-shim", service_version: "2.14.0", revision: "", on_version_drift: "fail" }),
    provider("bge-small", "embedder", "openai_compatible", { base_url: "http://gpu-01:8080/v1", model: "BAAI/bge-small-en-v1.5", dimensions: 384, normalize: true, query_prefix: "Represent this sentence for searching relevant passages: ", document_prefix: "", batch_size: 64, revision: "" }),
    provider("bge-m3", "embedder", "openai_compatible", { base_url: "http://gpu-02:8080/v1", model: "BAAI/bge-m3", dimensions: 1024, normalize: true, query_prefix: "", document_prefix: "", batch_size: 32, revision: "" }),
    provider("local-chat", "llm", "openai_compatible", { base_url: "http://gpu-01:8082/v1", model: "qwen3-4b-instruct", temperature: 0, max_tokens: 1024, token_limit_field: "max_tokens", supports_images: false, extra_body: {}, revision: "", daily_token_budget: 2000000 }),
    provider("jina-rerank", "reranker", "http", { base_url: "http://gpu-01:8081", model: "jina-reranker-v2-base-multilingual", revision: "" }),
    provider("local-sqlite", "vector_store", "sqlite_vec", { path: "data/vectors.db" }),
    provider("qdrant-main", "vector_store", "qdrant", { url: "http://qdrant:6333" }, { secrets: { api_key: { source: "env", set: true, env_var: "QDRANT_API_KEY" } } }),
  ];
}

// ---------- Corpora and documents ----------

export const supportSchema = { fields: { lang: { type: "str" as const, default: "en" }, product: { type: "str" as const, default: "core" }, year: { type: "int" as const, default: 2025 } } };

export function corpus(name: string, schema: MetadataSchema = supportSchema, createdDaysAgo = 30): CorpusRec {
  return { id: uuidFrom(`corpus:${name}`), name, metadata_schema: schema, active_pipeline_id: null, created_at: ago(60 * 24 * createdDaysAgo) };
}

const AREAS = ["billing", "api", "auth", "sso", "webhooks", "exports", "admin", "onboarding", "integrations", "security", "mobile", "reports"];
const TOPICS = ["refund-policy", "rate-limits", "token-rotation", "saml-setup", "retry-behaviour", "csv-export", "user-roles", "workspace-setup", "slack-app", "data-retention", "push-notifications", "usage-dashboard", "invoice-history", "error-codes", "pagination", "audit-log", "ip-allowlist", "scim-provisioning", "timezone-handling", "bulk-import"];
const KINDS: [string, string, number][] = [["md", "text/markdown", 0.55], ["pdf", "application/pdf", 0.3], ["txt", "text/plain", 0.08], ["html", "text/html", 0.07]];
export const SENTENCES = [
  "Refunds are issued to the original payment method within five to ten business days.",
  "Requests above the limit receive HTTP 429 with a Retry-After header in seconds.",
  "Rotate API tokens every 90 days; old tokens keep working for 24 hours after rotation.",
  "Workspace admins can assign the Billing Manager role without granting full admin access.",
  "Webhook deliveries are retried with exponential backoff for up to 72 hours.",
  "Exports larger than 100,000 rows are split into multiple CSV files inside one archive.",
  "SAML assertions must be signed; encrypted assertions are optional but recommended.",
  "Audit log entries are retained for 400 days on the Enterprise plan.",
  "The mobile app caches the last 50 conversations for offline reading.",
  "Usage dashboards refresh every 15 minutes and show data in the workspace timezone.",
];

/** The document's own fact: where escalations about its topic go. */
function escalation(area: string, topic: string, i: number): string {
  return `Escalations about ${topic.replace(/-/g, " ")} for the ${area} area go to queue ${escalationQueue(i)}.`;
}

export function escalationQueue(i: number): string {
  return `SUP-${(1000 + i * 37) % 9000 + 1000}`;
}

export function documents(c: CorpusRec, count: number, seed: number): DocRec[] {
  const rand = rng(seed);
  const out: DocRec[] = [];
  const used = new Set<string>();
  for (let i = 0; i < count; i++) {
    const area = AREAS[Math.floor(rand() * AREAS.length)]!;
    const topic = TOPICS[Math.floor(rand() * TOPICS.length)]!;
    let r = rand();
    let kind = KINDS[0]!;
    for (const k of KINDS) { if (r < k[2]) { kind = k; break; } r -= k[2]; }
    let filename = `${area}/${topic}.${kind[0]}`;
    for (let v = 2; used.has(filename); v++) filename = `${area}/${topic}-v${v}.${kind[0]}`;
    used.add(filename);
    const id = uuidFrom(`doc:${c.name}:${filename}`);
    const updated = ago(Math.floor(rand() * 60 * 24 * 20));
    const chunk_texts = Array.from({ length: 3 + Math.floor(rand() * 5) }, (_, j) =>
      [SENTENCES[(i + j) % SENTENCES.length], SENTENCES[(i * 3 + j + 4) % SENTENCES.length], SENTENCES[(i + j * 7 + 2) % SENTENCES.length]].join(" "),
    );
    // One fact only this document has (no rand() call, so the rest of the fixture is unchanged).
    const at = i % chunk_texts.length;
    chunk_texts[at] = `${chunk_texts[at]} ${escalation(area, topic, i)}`;
    out.push({
      id, corpus_id: c.id, external_id: rand() < 0.6 ? `${area}-${topic}` + (filename.includes("-v") ? filename.slice(filename.lastIndexOf("-v"), filename.lastIndexOf(".")) : "") : null,
      filename, mime_type: kind[1], size_bytes: Math.floor(2_000 + rand() * (kind[0] === "pdf" ? 2_400_000 : 90_000)),
      content_sha256: fp(`content:${id}`), metadata: { lang: rand() < 0.85 ? "en" : "de", product: area === "mobile" ? "mobile" : "core", year: rand() < 0.7 ? 2025 : 2024 },
      attributes: { owner: rand() < 0.5 ? "support-team" : "docs-team" }, revision: 1, status: "active",
      created_at: updated, updated_at: updated, chunk_texts,
    });
  }
  return out;
}

// ---------- Pipeline configs ----------

const parseNode = () => ({ id: "parse", type: "parse" as const, routes: [{ mime: "text/*", provider: "builtin-text" }, { mime: "*/*", provider: "docling", options: { ocr: true } }] });

export function hybrid(
  name: string,
  opts: { chunk?: "recursive" | "structure"; maxChars?: number; embedder?: string; rerank?: boolean; enrich?: EnrichMode; transform?: QueryTransformConfig } = {},
): PipelineConfig {
  const strategy = opts.chunk ?? "recursive";
  // With enrichment the text the retrieval nodes read comes from the enrich node, not the chunk node.
  const text = opts.enrich ? "enrich" : "chunk";
  return {
    name,
    nodes: [
      parseNode(),
      { id: "chunk", type: "chunk", inputs: ["parse"], strategy, options: { max_chars: opts.maxChars ?? 1000, overlap_chars: strategy === "structure" ? 0 : 100 } },
      ...(opts.enrich ? [{ id: "enrich", type: "enrich" as const, inputs: ["chunk"] as [string], provider: "local-chat", options: { mode: opts.enrich } }] : []),
      { id: "embed", type: "embed", inputs: [text], provider: opts.embedder ?? "bge-small" },
      { id: "vec", type: "index_vector", inputs: ["embed"], provider: "local-sqlite", options: { metric: "cosine" } },
      { id: "fts", type: "index_fts", inputs: [text], options: { tokenizer: "unicode61" } },
    ],
    retrieval: {
      ...(opts.transform ? { transform: opts.transform } : {}),
      sources: [{ id: "dense", index: "vec", top_k: 30 }, { id: "sparse", index: "fts", top_k: 30 }],
      fusion: { method: "rrf", k: 60, weights: { dense: 1, sparse: 1 } },
      ...(opts.rerank ? { rerank: { provider: "jina-rerank", top_n: 30 } } : {}),
      expand: { neighbors: 1 },
      top_k: 8,
    },
  };
}

// ---------- Evaluation dataset ----------

/** A question and an answer snippet for each of SENTENCES (same order). */
const QA: [string, string][] = [
  ["how long do refunds to the original payment method take", "within five to ten business days"],
  ["what happens to requests above the rate limit", "receive HTTP 429 with a Retry-After header"],
  ["how often should API tokens be rotated", "Rotate API tokens every 90 days"],
  ["which role gives billing access without full admin", "Billing Manager role"],
  ["how long are webhook deliveries retried", "retried with exponential backoff for up to 72 hours"],
  ["what happens to exports over 100,000 rows", "split into multiple CSV files"],
  ["must SAML assertions be signed", "SAML assertions must be signed"],
  ["how long are audit log entries kept", "retained for 400 days"],
  ["how many conversations does the mobile app keep offline", "caches the last 50 conversations"],
  ["how often do usage dashboards refresh", "refresh every 15 minutes"],
];

/**
 * JSON Lines for a support-docs dataset over every `step`-th document. Most questions ask for the
 * document's own escalation queue (findable, but only if the chunker keeps that sentence whole);
 * every fourth asks a generic fact many documents share (hard for any pipeline).
 */
export function goldenJsonl(docs: DocRec[], count = 40): string {
  const step = Math.max(1, Math.floor(docs.length / count));
  const lines: string[] = [];
  for (let i = 0, n = 0; i < docs.length && lines.length < count; i += step, n++) {
    const d = docs[i]!;
    const [area, file] = d.filename.split("/") as [string, string];
    const topic = file.replace(/\.[^.]+$/, "").replace(/-v\d+$/, "").replace(/-/g, " ");
    const doc = d.external_id ?? d.id;
    const queue = /queue (SUP-\d+)/.exec(d.chunk_texts.join(" "))?.[1];
    if (n % 4 !== 3 && queue) {
      lines.push(JSON.stringify({ question: `Which queue handles ${topic} escalations in ${area}?`, targets: [{ document: doc, text_contains: `go to queue ${queue}` }], metadata: { kind: "escalation", topic } }));
      continue;
    }
    const text = d.chunk_texts.join(" ");
    const s = SENTENCES.findIndex((x) => text.includes(x));
    if (s < 0) continue;
    const [question, snippet] = QA[s]!;
    lines.push(JSON.stringify({ question: `${topic}: ${question}?`, targets: [{ document: doc, text_contains: snippet }], metadata: { kind: "fact", topic } }));
  }
  return lines.join("\n");
}

