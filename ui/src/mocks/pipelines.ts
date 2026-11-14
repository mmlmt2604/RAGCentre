// Mock pipeline creation, dry-run plans and diffs, following the backend's planner and diff
// (IMPLEMENTATION_PLAN.md §5, §16; src/ragcentre/pipeline/planner.py, api/routers/pipelines.py).
import type { ConfigChange, PipelineConfig, PipelineDiff, PipelinePlan, PipelineProblem } from "../api/types";
import { createPipeline } from "./engine";
import { MockError } from "./providers";
import { activeDocs, indexRuns, resolvePipeline, runKey, type MockState } from "./store";

const STRATEGIES = ["fixed", "recursive", "structure", "page", "http"];

/** Rules the mock's resolver does not cover: names and chunk options (§5 rule 12, §9). */
function extraProblems(state: MockState, corpusId: string, config: PipelineConfig): PipelineProblem[] {
  const problems: PipelineProblem[] = [];
  if (config.name && state.pipelines.some((p) => p.corpus_id === corpusId && p.name === config.name))
    problems.push({ node_id: null, path: "name", message: `A pipeline named "${config.name}" already exists in this corpus.` });
  config.nodes.forEach((n, i) => {
    if (n.type !== "chunk") return;
    if (!STRATEGIES.includes(n.strategy)) {
      problems.push({ node_id: n.id, path: `nodes[${i}].strategy`, message: `Unknown chunk strategy "${n.strategy}".` });
      return;
    }
    const o = (n.options ?? {}) as Record<string, unknown>;
    const max = o.max_chars;
    if (max !== undefined && (typeof max !== "number" || !Number.isInteger(max) || max < 100 || max > 20000))
      problems.push({ node_id: n.id, path: `nodes[${i}].options.max_chars`, message: "max_chars must be a whole number from 100 to 20000." });
    const overlap = o.overlap_chars;
    const effectiveMax = typeof max === "number" ? max : n.strategy === "structure" ? 1200 : 1000;
    if (overlap !== undefined && (typeof overlap !== "number" || overlap < 0 || overlap >= effectiveMax / 2))
      problems.push({ node_id: n.id, path: `nodes[${i}].options.overlap_chars`, message: "overlap_chars must be at least 0 and less than half of max_chars." });
  });
  return problems;
}

function shape(raw: unknown): PipelineConfig {
  const c = (raw ?? {}) as Partial<PipelineConfig>;
  return { name: typeof c.name === "string" ? c.name : "", nodes: Array.isArray(c.nodes) ? c.nodes : [], retrieval: (c.retrieval ?? { sources: [], top_k: 0 }) as PipelineConfig["retrieval"] };
}

export function plan(state: MockState, corpusId: string, raw: unknown): PipelinePlan {
  const config = shape(raw);
  const res = resolvePipeline(state, config);
  const problems = [...extraProblems(state, corpusId, config), ...res.problems];
  if (problems.length || !res.fingerprint) return { valid: false, problems, pipeline_fingerprint: null, nodes: [], total_runs_to_create: 0 };
  const docs = activeDocs(state, corpusId);
  const index = indexRuns(state);
  let total = 0;
  const nodes = res.nodes.map((n) => {
    const users = state.pipelines.filter((p) => p.corpus_id === corpusId && p.nodes.some((x) => x.node_fp === n.node_fp));
    const toCreate = docs.filter((d) => !index.has(runKey(d.id, n.node_fp))).length;
    total += toCreate;
    return { node_id: n.node_id, type: n.type, node_fp: n.node_fp, reused: users.length > 0, shared_with: users.filter((p) => !p.archived).map((p) => p.id), runs_to_create: toCreate };
  });
  return { valid: true, problems: [], pipeline_fingerprint: res.fingerprint, nodes, total_runs_to_create: total };
}

/** The submitted configuration as the backend returns it: unset settings omitted, defaults filled. */
function normalise(c: PipelineConfig): PipelineConfig {
  const clean = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(clean);
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).filter(([, x]) => x !== null && x !== undefined).map(([k, x]) => [k, clean(x)]));
    return v;
  };
  const out = clean(c) as PipelineConfig;
  // Submitted configs may omit these; the response always has them (the contract makes them required).
  if (out.retrieval.fusion) out.retrieval.fusion = { ...out.retrieval.fusion, method: out.retrieval.fusion.method ?? "rrf" };
  if (out.retrieval.expand) out.retrieval.expand = { ...out.retrieval.expand, neighbors: out.retrieval.expand.neighbors ?? 0 };
  return out;
}

export function create(state: MockState, corpusId: string, raw: unknown) {
  if (!state.corpora.some((c) => c.id === corpusId)) throw new MockError(404, "not_found", `Corpus ${corpusId} was not found.`);
  const p = plan(state, corpusId, raw);
  if (!p.valid) throw new MockError(422, "invalid_pipeline", `The pipeline is not valid: ${p.problems[0]?.message ?? ""}`, { problems: p.problems });
  return createPipeline(state, corpusId, normalise(shape(raw)));
}

export function diff(state: MockState, aId: string, bId: string): PipelineDiff {
  const a = state.pipelines.find((p) => p.id === aId);
  const b = state.pipelines.find((p) => p.id === bId);
  if (!a || !b) throw new MockError(404, "not_found", "Pipeline was not found.");
  if (a.corpus_id !== b.corpus_id) throw new MockError(404, "not_found", "Pipelines belong to different corpora.");
  const ma = new Map(a.config.nodes.map((n) => [n.id, n as Record<string, unknown>]));
  const mb = new Map(b.config.nodes.map((n) => [n.id, n as Record<string, unknown>]));
  const fa = new Map(a.nodes.map((n) => [n.node_id, n.node_fp]));
  const fb = new Map(b.nodes.map((n) => [n.node_id, n.node_fp]));
  const eq = (x: unknown, y: unknown) => JSON.stringify(x ?? null) === JSON.stringify(y ?? null);
  const ids = [...new Set([...ma.keys(), ...mb.keys()])];
  const nodes = ids.map((id) => {
    const na = ma.get(id);
    const nb = mb.get(id);
    const same = Boolean(na && nb && fa.get(id) === fb.get(id));
    const changes: ConfigChange[] = [];
    if (na && nb && !same)
      for (const k of [...new Set([...Object.keys(na), ...Object.keys(nb)])].sort()) if (!eq(na[k], nb[k])) changes.push({ path: k, a: na[k] ?? null, b: nb[k] ?? null });
    return { node_id: id, type: String((na ?? nb)!.type), in_a: Boolean(na), in_b: Boolean(nb), same, changes };
  });
  const ra = a.config.retrieval as unknown as Record<string, unknown>;
  const rb = b.config.retrieval as unknown as Record<string, unknown>;
  const retrieval_changes = [...new Set([...Object.keys(ra), ...Object.keys(rb)])].filter((k) => !eq(ra[k], rb[k])).map((k) => ({ path: k, a: ra[k] ?? null, b: rb[k] ?? null }));
  return { a: a.id, b: b.id, nodes, retrieval_same: retrieval_changes.length === 0, retrieval_changes };
}
