// Mock provider and corpus operations: the behaviour of the backend's providers/manager.py and
// corpora router, close enough that the UI meets the same errors (IMPLEMENTATION_PLAN.md §8, §15, §16).
import type { MetadataSchema, Provider, ProviderTestResult, Scalar } from "../api/types";
import { configFields, METADATA_FIELD_RE, PROVIDER_NAME_RE, SERVER_FIELDS, toConfig, type FormValues } from "../lib/configSchema";
import { refreshProviderUsage } from "./engine";
import { provider as makeProvider } from "./fixtures";
import { uuidFrom } from "./hash";
import { now, type CorpusRec, type MockState } from "./store";

export class MockError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
  }
}

const ENV_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

function typeOf(state: MockState, kind: string, type: string) {
  return state.providerTypes.find((t) => t.kind === kind && t.type === type);
}

export function findProvider(state: MockState, id: string): Provider {
  const p = state.providers.find((x) => x.id === id);
  if (!p) throw new MockError(404, "not_found", "Provider was not found.");
  return p;
}

/** Validate a config against the type's schema, as the backend's pydantic models do. */
function validConfig(state: MockState, kind: string, type: string, config: Record<string, unknown>): Record<string, unknown> {
  const t = typeOf(state, kind, type);
  if (!t) throw new MockError(422, "validation_error", `Unknown provider type ${kind}/${type}.`);
  const fields = configFields(t.config_schema);
  const known = new Set(fields.map((f) => f.key));
  const unknown = Object.keys(config).filter((k) => !known.has(k) && !SERVER_FIELDS.has(k));
  if (unknown.length) throw new MockError(422, "validation_error", `Unknown setting ${unknown[0]}.`, { problems: unknown.map((k) => ({ path: `config.${k}`, message: "Extra inputs are not permitted" })) });
  const values: FormValues = {};
  for (const f of fields) {
    const v = config[f.key];
    values[f.key] = f.kind === "boolean" ? v === true : v === undefined || v === null ? "" : String(v);
  }
  const { config: parsed, errors } = toConfig(fields, values);
  const problems = Object.entries(errors).map(([k, message]) => ({ path: `config.${k}`, message }));
  if (problems.length) throw new MockError(422, "validation_error", `Invalid request: ${problems[0]!.path}: ${problems[0]!.message}`, { problems });
  // Apply schema defaults so the stored config is complete, like the backend's model_dump.
  for (const f of fields) if (!(f.key in parsed) && f.default !== undefined) parsed[f.key] = f.default;
  return parsed;
}

export function createProvider(state: MockState, body: { name?: string; kind?: string; type?: string; config?: Record<string, unknown> }): Provider {
  const name = String(body.name ?? "");
  if (!PROVIDER_NAME_RE.test(name))
    throw new MockError(422, "validation_error", "Provider names use lowercase letters, digits and hyphens, start with a letter, and are at most 63 characters.");
  if (state.providers.some((p) => p.name === name)) throw new MockError(409, "conflict", `A provider named '${name}' already exists.`);
  const kind = String(body.kind ?? "") as Provider["kind"];
  const type = String(body.type ?? "");
  const config = validConfig(state, kind, type, body.config ?? {});
  const t = typeOf(state, kind, type)!;
  if (t.fingerprint_fields.includes("service_version")) {
    // HTTP parsers and chunkers: RAGCentre reads /info to pin the service name and version.
    const url = String(config.base_url ?? "");
    if (/offline|\.invalid|unreachable/.test(url))
      throw new MockError(502, "provider_error", `Could not read ${name}'s /info to pin its version: ${name}: could not connect to ${url}/info`, { provider: name });
    config.service_name = kind === "parser" ? "docling-shim" : "semantic-chunker";
    config.service_version = kind === "parser" ? "2.14.0" : "1.0.3";
  }
  if (t.fingerprint_fields.includes("impl_version")) config.impl_version = 1;
  const p = makeProvider(name, kind, type, config, { id: uuidFrom(`provider:${name}:${Date.now()}`), created_at: now(), updated_at: now() });
  state.providers.push(p);
  return p;
}

export function patchProvider(state: MockState, id: string, patch: Record<string, unknown>): Provider {
  const p = findProvider(state, id);
  const t = typeOf(state, p.kind, p.type)!;
  const permanent = new Set([...t.fingerprint_fields, ...SERVER_FIELDS]);
  for (const [key, value] of Object.entries(patch)) {
    if (permanent.has(key) && JSON.stringify(value) !== JSON.stringify(p.config[key] ?? null))
      throw new MockError(409, "immutable_field", `${key} is part of ${p.name}'s fingerprint and cannot be changed. Create a new provider (and a new pipeline) instead.`, { field: key });
  }
  const merged = { ...p.config, ...patch };
  const operational = Object.fromEntries(Object.entries(merged).filter(([k]) => !SERVER_FIELDS.has(k)));
  const valid = validConfig(state, p.kind, p.type, operational);
  for (const k of SERVER_FIELDS) if (k in p.config) valid[k] = p.config[k];
  p.config = valid;
  p.updated_at = now();
  return p;
}

export function deleteProvider(state: MockState, id: string): void {
  const p = findProvider(state, id);
  if (p.builtin) throw new MockError(409, "conflict", `${p.name} is built in and cannot be deleted.`);
  refreshProviderUsage(state);
  if (p.used_by_pipelines)
    throw new MockError(409, "provider_in_use", `${p.name} is used by ${p.used_by_pipelines} pipeline${p.used_by_pipelines === 1 ? "" : "s"}. Archive them first.`, { pipelines: p.used_by_pipelines });
  state.providers = state.providers.filter((x) => x.id !== id);
}

export function setSecret(state: MockState, id: string, key: string, body: { env?: string; value?: string }): Provider {
  const p = findProvider(state, id);
  const t = typeOf(state, p.kind, p.type)!;
  if (!t.secret_keys.includes(key))
    throw new MockError(422, "validation_error", `${p.type} providers have no secret '${key}' (secret keys: ${t.secret_keys.join(", ") || "none"}).`);
  const hasEnv = typeof body.env === "string";
  const hasValue = typeof body.value === "string";
  if (hasEnv === hasValue) throw new MockError(422, "validation_error", 'Send exactly one of {"env": "VAR_NAME"} or {"value": "..."}.');
  if (hasEnv) {
    if (!ENV_RE.test(body.env!)) throw new MockError(422, "validation_error", "env must be an environment variable name.");
    p.secrets[key] = { source: "env", set: true, env_var: body.env! };
  } else {
    if (!state.secretStorage)
      throw new MockError(409, "secret_storage_disabled", "Stored secrets need RAGCENTRE_SECRET_KEY on the server. Reference an environment variable instead.");
    if (!body.value) throw new MockError(422, "validation_error", "The secret value is empty.");
    p.secrets[key] = { source: "stored", set: true, env_var: null };
  }
  p.updated_at = now();
  return p;
}

export function deleteSecret(state: MockState, id: string, key: string): Provider {
  const p = findProvider(state, id);
  if (key in p.secrets) p.secrets[key] = { source: "env", set: false, env_var: null };
  p.updated_at = now();
  return p;
}

export function testProvider(state: MockState, id: string, deep = false): ProviderTestResult {
  const p = findProvider(state, id);
  const fault = state.serviceFaults[p.name] ?? {};
  const latency = 18 + ((p.name.length * 37) % 140);
  const result: ProviderTestResult = { ok: true, latency_ms: latency, message: null, service: null, dimensions: null, version_drift: null };
  if (fault.down) return { ...result, ok: false, latency_ms: 5000, message: `${p.name}: ${fault.down}` };
  if (p.kind === "parser" || p.kind === "chunker") {
    if (p.type === "builtin_text") return { ...result, latency_ms: 1, service: { name: "builtin_text", version: "1", mime_types: ["text/plain", "text/markdown"] } };
    const pinned = String(p.config.service_version ?? "");
    const current = fault.version ?? pinned;
    result.service = { name: String(p.config.service_name ?? "service"), version: current, mime_types: ["application/pdf", "image/*", "text/html"] };
    if (current !== pinned) {
      result.ok = false;
      result.version_drift = { pinned, current };
      result.message = `${p.name} now reports version ${current}; it was pinned at ${pinned} when created.`;
    }
  } else if (p.kind === "embedder") {
    result.dimensions = typeof p.config.dimensions === "number" ? p.config.dimensions : 256;
  } else if (p.kind === "vector_store" && deep) {
    // A successful deep test, mirroring the backend's stores/check.py: write, query, filter, delete, drop.
    result.check = {
      ok: true,
      store_type: p.type,
      collection: "rc_check_ab12cd34",
      records: 50,
      metrics: ["cosine", "l2"],
      seconds: 0.4,
      findings: [
        { level: "info", check: "healthcheck", message: "reachable: yes", ms: 2, lost: null },
        { level: "info", check: "create", message: "collection rc_check_ab12cd34 (metric cosine): yes", ms: 5, lost: null },
        { level: "info", check: "upsert", message: "wrote 50 records: yes", ms: 12, lost: null },
        { level: "info", check: "query:cosine", message: "top-k and scores match brute force (k=10): yes", ms: 8, lost: null },
        { level: "info", check: "filter:selective", message: "top-k and scores match brute force (k=10): yes", ms: 3, lost: null },
        { level: "info", check: "drop", message: "removed temporary collection rc_check_ab12cd34: yes", ms: 4, lost: null },
      ],
    };
  }
  return result;
}

// ---------- Corpora ----------

function checkSchema(schema: MetadataSchema): void {
  for (const [name, spec] of Object.entries(schema.fields ?? {})) {
    if (!METADATA_FIELD_RE.test(name) || name === "document_id")
      throw new MockError(422, "validation_error", `Invalid or reserved metadata field '${name}'.`);
    if (!scalarOk(spec.default, spec.type)) throw new MockError(422, "validation_error", `Default for ${name} must have type ${spec.type}.`);
  }
}

function scalarOk(v: Scalar, type: string): boolean {
  if (type === "str") return typeof v === "string";
  if (type === "bool") return typeof v === "boolean";
  if (type === "int") return typeof v === "number" && Number.isInteger(v);
  return typeof v === "number" && Number.isFinite(v);
}

export function findCorpus(state: MockState, id: string): CorpusRec {
  const c = state.corpora.find((x) => x.id === id);
  if (!c) throw new MockError(404, "not_found", `Corpus ${id} was not found.`);
  return c;
}

export function createCorpus(state: MockState, body: { name?: string; metadata_schema?: MetadataSchema }): CorpusRec {
  const name = String(body.name ?? "").trim();
  if (!name) throw new MockError(422, "validation_error", "Give the corpus a name.");
  if (state.corpora.some((c) => c.name === name)) throw new MockError(409, "conflict", `A corpus named '${name}' already exists.`);
  const schema = body.metadata_schema ?? { fields: {} };
  checkSchema(schema);
  const c: CorpusRec = { id: uuidFrom(`corpus:${name}:${Date.now()}`), name, metadata_schema: { fields: { ...schema.fields } }, active_pipeline_id: null, created_at: now() };
  state.corpora.push(c);
  return c;
}

export function renameCorpus(state: MockState, id: string, name: string): CorpusRec {
  const c = findCorpus(state, id);
  const clean = name.trim();
  if (!clean) throw new MockError(422, "validation_error", "Give the corpus a name.");
  if (state.corpora.some((x) => x.name === clean && x.id !== id)) throw new MockError(409, "conflict", `A corpus named '${clean}' already exists.`);
  c.name = clean;
  return c;
}

export function updateSchema(state: MockState, id: string, schema: MetadataSchema): CorpusRec {
  const c = findCorpus(state, id);
  checkSchema(schema);
  for (const [name, spec] of Object.entries(c.metadata_schema.fields)) {
    const incoming = schema.fields[name];
    if (!incoming || incoming.type !== spec.type || incoming.default !== spec.default)
      throw new MockError(409, "immutable_field", `Existing field ${name} and its default cannot be changed or removed.`);
  }
  // Adding a field rewrites the corpus's vector index records (cheap: vectors come from the cache).
  c.metadata_schema = { fields: { ...schema.fields } };
  return c;
}
