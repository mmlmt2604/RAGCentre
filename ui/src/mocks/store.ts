// In-memory mock backend state. Mirrors the backend's data model closely enough that
// screens behave as they will against the real server (IMPLEMENTATION_PLAN.md §§5, 6, 12).
import type {
  Document,
  EvalComparison,
  EvalDataset,
  EvalItem,
  EvalItemResult,
  EvalRun,
  EvalSummary,
  MetadataSchema,
  OcrState,
  PipelineConfig,
  PipelineNode,
  PipelineProblem,
  PipelineState,
  Provider,
  ProviderType,
  RunState,
} from "../api/types";
import { fp } from "./hash";

export interface CorpusRec {
  id: string;
  name: string;
  metadata_schema: MetadataSchema;
  active_pipeline_id: string | null;
  created_at: string;
}

export type DocRec = Omit<Document, "ingestion"> & { chunk_texts: string[]; /** uploaded text, when known */ content?: string };

export interface NodeRec {
  node_id: string;
  type: PipelineNode["type"];
  node_fp: string;
  upstream: string[]; // upstream node fingerprints
  resolved: Record<string, unknown>;
}

export interface PipelineRec {
  id: string;
  corpus_id: string;
  name: string;
  config: PipelineConfig;
  fingerprint: string;
  archived: boolean;
  created_at: string;
  nodes: NodeRec[];
}

export type StoredRunStatus = Exclude<RunState, "blocked">;

export interface RunRec {
  id: number;
  corpus_id: string;
  document_id: string;
  node_fp: string;
  node_type: string;
  upstream: string[];
  status: StoredRunStatus;
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  stats: Record<string, unknown> | null;
  run_after: string;
  started_at: string | null;
  finished_at: string | null;
  /** Mock-only: this run fails when executed, until retried. */
  will_fail: { code: string; message: string } | null;
}

export interface EvalRunRec extends EvalRun {
  summaries: EvalSummary[];
  results: Record<string, Record<string, EvalItemResult>>; // pipeline -> item -> result
  allow_partial: boolean;
  /** Each pipeline's fingerprint when the run was created. */
  fingerprints: Record<string, string>;
  /** Computed once the run has finished. */
  comparisons?: EvalComparison[];
  /** Seeded runs: reported as finished, results computed when first read (keeps scenarios fast). */
  deferred?: { finishedAt: string };
}

export interface MockState {
  providerTypes: ProviderType[];
  providers: Provider[];
  corpora: CorpusRec[];
  documents: DocRec[];
  pipelines: PipelineRec[];
  runs: RunRec[];
  datasets: EvalDataset[];
  datasetItems: Record<string, EvalItem[]>;
  evalRuns: EvalRunRec[];
  nextRunId: number;
  authToken: string | null;
  failNetwork: boolean;
  /** Whether the server has RAGCENTRE_SECRET_KEY, so secrets can be stored encrypted. */
  secretStorage: boolean;
  /** RAGCENTRE_MAX_UPLOAD_MB, in bytes. */
  maxUploadBytes: number;
  /** Simulated provider service conditions, by provider name: down (with the error) or a newer version. */
  serviceFaults: Record<string, { down?: string; version?: string; ocr?: OcrState }>;
  /** URLs where a parser shim answers, whether or not it is a provider. */
  inContainer: boolean;
  runningShims: string[];
  startingShims: { url: string; at: number }[];
  discoveryEnabled: boolean;
  /** Runs started or finished per tick of the ingestion simulator. */
  throughput: number;
}

export const MAX_ATTEMPTS = 5;

export const now = () => new Date().toISOString();
export const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

export function emptyState(): MockState {
  return {
    providerTypes: [],
    providers: [],
    corpora: [],
    documents: [],
    pipelines: [],
    runs: [],
    datasets: [],
    datasetItems: {},
    evalRuns: [],
    nextRunId: 1,
    authToken: null,
    failNetwork: false,
    secretStorage: true,
    maxUploadBytes: 200 * 1024 * 1024,
    serviceFaults: {},
    inContainer: true,
    runningShims: [],
    startingShims: [],
    discoveryEnabled: true,
    throughput: 60,
  };
}

// ---------- Pipeline resolution and fingerprints (§5, §6) ----------

const IMPL_VERSION: Record<string, number> = {
  parse: 1,
  chunk_fixed: 1,
  chunk_recursive: 1,
  chunk_structure: 1,
  chunk_page: 1,
  chunk_http: 1,
  embed: 1,
  index_vector: 1,
  index_fts: 1,
};

const CHUNK_DEFAULTS: Record<string, Record<string, unknown>> = {
  fixed: { max_chars: 1000, overlap_chars: 100 },
  recursive: { max_chars: 1000, overlap_chars: 100 },
  structure: { max_chars: 1200, overlap_chars: 0, include_heading_path: true },
  page: { max_chars: 4000 },
  http: {},
};

const EXPECTED_INPUT: Record<PipelineNode["type"], PipelineNode["type"] | null> = {
  parse: null,
  chunk: "parse",
  embed: "chunk",
  index_vector: "embed",
  index_fts: "chunk",
};

const PROVIDER_KIND: Partial<Record<PipelineNode["type"], Provider["kind"]>> = {
  embed: "embedder",
  index_vector: "vector_store",
};

export interface Resolution {
  nodes: NodeRec[];
  problems: PipelineProblem[];
  fingerprint: string | null;
}

/** Validate (rules 1–5, 8–11 of §5) and fingerprint a pipeline config against the store. */
export function resolvePipeline(state: MockState, config: PipelineConfig): Resolution {
  const problems: PipelineProblem[] = [];
  const providerByName = new Map(state.providers.map((p) => [p.name, p]));
  const byId = new Map<string, PipelineNode>();

  if (!config.name || !config.name.trim()) problems.push({ node_id: null, path: "name", message: "Give the pipeline a name." });

  config.nodes.forEach((node, i) => {
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(node.id))
      problems.push({ node_id: node.id || null, path: `nodes[${i}].id`, message: "Node ids use lowercase letters, digits and underscores, starting with a letter." });
    if (byId.has(node.id)) problems.push({ node_id: node.id, path: `nodes[${i}].id`, message: `Node id "${node.id}" is used twice.` });
    byId.set(node.id, node);
  });

  const parseNodes = config.nodes.filter((n) => n.type === "parse");
  if (parseNodes.length !== 1)
    problems.push({ node_id: null, path: "nodes", message: parseNodes.length === 0 ? "Add a parse node." : "A pipeline has exactly one parse node." });

  const a = (noun: string) => `${/^[aeiou]/i.test(noun) ? "an" : "a"} ${noun.replace("_", " ")}`;
  const checkProvider = (node: PipelineNode, name: string | undefined, kind: Provider["kind"], path: string) => {
    if (!name) {
      problems.push({ node_id: node.id, path, message: `Choose ${a(kind)} provider.` });
      return null;
    }
    const p = providerByName.get(name);
    if (!p) problems.push({ node_id: node.id, path, message: `No provider named "${name}".` });
    else if (p.kind !== kind) problems.push({ node_id: node.id, path, message: `"${name}" is ${a(p.kind)}, not ${a(kind)}.` });
    return p && p.kind === kind ? p : null;
  };

  config.nodes.forEach((node, i) => {
    const expected = EXPECTED_INPUT[node.type];
    const inputs = "inputs" in node ? node.inputs : [];
    if (expected === null) {
      if (inputs && inputs.length) problems.push({ node_id: node.id, path: `nodes[${i}].inputs`, message: "A parse node takes no inputs." });
    } else {
      const src = inputs?.[0];
      const up = src ? byId.get(src) : undefined;
      if (!src) problems.push({ node_id: node.id, path: `nodes[${i}].inputs`, message: `Connect this node to a ${expected} node.` });
      else if (!up) problems.push({ node_id: node.id, path: `nodes[${i}].inputs`, message: `Input "${src}" does not exist.` });
      else if (up.type !== expected) problems.push({ node_id: node.id, path: `nodes[${i}].inputs`, message: `A ${node.type} node reads from a ${expected} node, not ${up.type}.` });
    }
    if (node.type === "parse") {
      if (!node.routes.length) problems.push({ node_id: node.id, path: `nodes[${i}].routes`, message: "Add at least one route." });
      node.routes.forEach((r, j) => checkProvider(node, r.provider, "parser", `nodes[${i}].routes[${j}].provider`));
    }
    if (node.type === "chunk" && node.strategy === "http") checkProvider(node, node.provider, "chunker", `nodes[${i}].provider`);
    const kind = PROVIDER_KIND[node.type];
    if (kind && "provider" in node) {
      const p = checkProvider(node, node.provider, kind, `nodes[${i}].provider`);
      if (p && node.type === "index_vector") {
        const metric = node.options?.metric ?? "cosine";
        const caps = state.providerTypes.find((t) => t.type === p.type && t.kind === p.kind)?.capabilities;
        if (caps && !caps.metrics.includes(metric))
          problems.push({ node_id: node.id, path: `nodes[${i}].options.metric`, message: `${p.name} does not support the ${metric} metric.` });
      }
    }
  });

  // Retrieval (rules 8–11)
  const r = config.retrieval;
  const sourceIds = new Set<string>();
  const chunkRoots = new Set<string>();
  r.sources.forEach((s, i) => {
    if (sourceIds.has(s.id)) problems.push({ node_id: null, path: `retrieval.sources[${i}].id`, message: `Source id "${s.id}" is used twice.` });
    sourceIds.add(s.id);
    const idx = byId.get(s.index);
    if (!idx || (idx.type !== "index_vector" && idx.type !== "index_fts"))
      problems.push({ node_id: null, path: `retrieval.sources[${i}].index`, message: `"${s.index}" is not an index node in this pipeline.` });
    else chunkRoots.add(chunkAncestor(byId, idx) ?? "?");
    if (s.top_k < 1 || s.top_k > 1000) problems.push({ node_id: null, path: `retrieval.sources[${i}].top_k`, message: "top_k must be between 1 and 1000." });
  });
  if (!r.sources.length) problems.push({ node_id: null, path: "retrieval.sources", message: "Add at least one retrieval source." });
  if (chunkRoots.size > 1) problems.push({ node_id: null, path: "retrieval.sources", message: "All sources must index the same chunk node, so their results can be fused." });
  if (r.sources.length > 1 && !r.fusion) problems.push({ node_id: null, path: "retrieval.fusion", message: "Choose a fusion method to merge two or more sources." });
  if (r.sources.length === 1 && r.fusion) problems.push({ node_id: null, path: "retrieval.fusion", message: "Fusion needs two or more sources; remove it." });
  if (r.rerank) {
    const p = state.providers.find((x) => x.name === r.rerank?.provider);
    if (!p || p.kind !== "reranker") problems.push({ node_id: null, path: "retrieval.rerank.provider", message: "Choose a reranker provider." });
  }
  const minSource = Math.min(...r.sources.map((s) => s.top_k), r.rerank?.top_n ?? Infinity);
  if (r.top_k < 1 || (Number.isFinite(minSource) && r.top_k > minSource))
    problems.push({ node_id: null, path: "retrieval.top_k", message: "The final top_k must be at least 1 and no larger than the sources feeding it." });

  if (problems.length) return { nodes: [], problems, fingerprint: null };

  // Fingerprints, in dependency order.
  const fps = new Map<string, string>();
  const nodes: NodeRec[] = [];
  const order = ["parse", "chunk", "embed", "index_vector", "index_fts"] as const;
  for (const type of order) {
    for (const node of config.nodes.filter((n) => n.type === type)) {
      const upstream = "inputs" in node && node.inputs ? node.inputs.map((id) => fps.get(id) ?? "") : [];
      const resolved = resolvedConfig(state, node);
      const implKey = node.type === "chunk" ? `chunk_${node.strategy}` : node.type;
      const nodeFp = fp({ type: node.type, impl_version: IMPL_VERSION[implKey] ?? 1, config: resolved, inputs: upstream });
      fps.set(node.id, nodeFp);
      nodes.push({ node_id: node.id, type: node.type, node_fp: nodeFp, upstream, resolved });
    }
  }
  const retrieval = {
    ...r,
    sources: r.sources.map((s) => ({ ...s, index: fps.get(s.index) })),
    rerank: r.rerank ? { ...r.rerank, provider: providerFp(state, r.rerank.provider) } : undefined,
  };
  const fingerprint = fp({ nodes: nodes.map((n) => n.node_fp).sort(), retrieval });
  return { nodes, problems: [], fingerprint };
}

function chunkAncestor(byId: Map<string, PipelineNode>, node: PipelineNode): string | null {
  let cur: PipelineNode | undefined = node;
  while (cur && cur.type !== "chunk") {
    const src: string | undefined = "inputs" in cur ? cur.inputs?.[0] : undefined;
    cur = src ? byId.get(src) : undefined;
  }
  return cur?.id ?? null;
}

function providerFp(state: MockState, name: string | undefined): string | null {
  return state.providers.find((p) => p.name === name)?.fingerprint ?? null;
}

function resolvedConfig(state: MockState, node: PipelineNode): Record<string, unknown> {
  switch (node.type) {
    case "parse":
      return { routes: node.routes.map((r) => ({ mime: r.mime, provider_fp: providerFp(state, r.provider), options: r.options ?? {} })) };
    case "chunk":
      return {
        strategy: node.strategy,
        options: { ...CHUNK_DEFAULTS[node.strategy], ...(node.options ?? {}) },
        ...(node.strategy === "http" ? { provider_fp: providerFp(state, node.provider) } : {}),
      };
    case "embed":
      return { provider_fp: providerFp(state, node.provider) };
    case "index_vector":
      return { provider_fp: providerFp(state, node.provider), metric: node.options?.metric ?? "cosine" };
    case "index_fts":
      return { tokenizer: node.options?.tokenizer ?? "unicode61" };
  }
}

export function chunkDefaults(strategy: string): Record<string, unknown> {
  return { ...(CHUNK_DEFAULTS[strategy] ?? {}) };
}

// ---------- Derivations (§12.6) ----------

export function runKey(documentId: string, nodeFp: string): string {
  return `${documentId}|${nodeFp}`;
}

export function indexRuns(state: MockState): Map<string, RunRec> {
  const map = new Map<string, RunRec>();
  for (const run of state.runs) map.set(runKey(run.document_id, run.node_fp), run);
  return map;
}

/** Effective state: a pending run whose upstream (transitively) failed is "blocked". */
export function effectiveStatus(run: RunRec, index: Map<string, RunRec>, seen = 0): RunState {
  if (run.status !== "pending" || seen > 8) return run.status;
  for (const up of run.upstream) {
    const u = index.get(runKey(run.document_id, up));
    if (!u) continue;
    const s = effectiveStatus(u, index, seen + 1);
    if (s === "failed" || s === "blocked") return "blocked";
  }
  return "pending";
}

/** The failed run that a blocked run is ultimately waiting on. */
export function blockingRun(run: RunRec, index: Map<string, RunRec>): RunRec | null {
  for (const up of run.upstream) {
    const u = index.get(runKey(run.document_id, up));
    if (!u) continue;
    if (u.status === "failed") return u;
    const deeper = blockingRun(u, index);
    if (deeper) return deeper;
  }
  return null;
}

export function activeDocs(state: MockState, corpusId: string): DocRec[] {
  return state.documents.filter((d) => d.corpus_id === corpusId && d.status === "active");
}

export interface PipelineHealth {
  status: PipelineState;
  coverage: number;
  counts: Map<string, Record<RunState, number>>; // by node_fp
}

export function pipelineHealth(state: MockState, pipeline: PipelineRec, index = indexRuns(state)): PipelineHealth {
  const docs = activeDocs(state, pipeline.corpus_id);
  const counts = new Map<string, Record<RunState, number>>();
  let anyProgress = false;
  let anyFailed = false;
  let allOk = true;
  const terminal = pipeline.nodes.filter((n) => n.type === "index_vector" || n.type === "index_fts");
  let covered = 0;

  for (const node of pipeline.nodes) counts.set(node.node_fp, { pending: 0, running: 0, succeeded: 0, failed: 0, blocked: 0 });

  for (const doc of docs) {
    let docCovered = true;
    for (const node of pipeline.nodes) {
      const run = index.get(runKey(doc.id, node.node_fp));
      const s: RunState = run ? effectiveStatus(run, index) : "pending";
      const c = counts.get(node.node_fp);
      if (c) c[s] += 1;
      if (s !== "succeeded") allOk = false;
      if (s === "pending" || s === "running") anyProgress = true;
      if (s === "failed") anyFailed = true;
      if (terminal.some((t) => t.node_fp === node.node_fp) && s !== "succeeded") docCovered = false;
    }
    if (docCovered) covered += 1;
  }

  const coverage = docs.length ? covered / docs.length : 1;
  const status: PipelineState = allOk ? "ready" : anyProgress ? "building" : anyFailed ? "degraded" : "building";
  return { status, coverage, counts };
}

/** Pipelines (non-archived, same corpus) that contain a node with this fingerprint. */
export function sharedWith(state: MockState, corpusId: string, nodeFp: string, exceptId?: string): string[] {
  return state.pipelines
    .filter((p) => p.corpus_id === corpusId && !p.archived && p.id !== exceptId && p.nodes.some((n) => n.node_fp === nodeFp))
    .map((p) => p.id);
}

/** Create pending runs for every (active document × node) pair that has none yet. Returns how many. */
export function planRuns(state: MockState, corpusId: string, nodes: NodeRec[], docs = activeDocs(state, corpusId), dryRun = false): number {
  const index = indexRuns(state);
  let created = 0;
  for (const doc of docs) {
    for (const node of nodes) {
      if (index.has(runKey(doc.id, node.node_fp))) continue;
      created += 1;
      if (dryRun) continue;
      const run: RunRec = {
        id: state.nextRunId++,
        corpus_id: corpusId,
        document_id: doc.id,
        node_fp: node.node_fp,
        node_type: node.type,
        upstream: node.upstream,
        status: "pending",
        attempt: 0,
        error_code: null,
        error_message: null,
        stats: null,
        run_after: now(),
        started_at: null,
        finished_at: null,
        will_fail: null,
      };
      state.runs.push(run);
      index.set(runKey(doc.id, node.node_fp), run);
    }
  }
  return created;
}
