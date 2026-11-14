// Mock evaluation: dataset import, runs that progress in the background, metrics, paired-bootstrap
// comparison and item diffs. Follows src/ragcentre/eval/{datasets,runner,matching,metrics,compare}.py.
// Retrieval goes through the mock query engine and matching is real, so item diffs show the chunks
// that actually hit or missed.
import type { EvalComparison, EvalDataset, EvalItem, EvalItemComparison, EvalItemResult, EvalRun, EvalRunDetail, EvalSummary, EvalTarget, QueryRequest } from "../api/types";
import { filterFields, FilterError, validateFilter } from "../lib/filters";
import { chunks as chunksOf } from "./documents";
import { rng, uuidFrom } from "./hash";
import type { Form } from "./multipart";
import { MockError } from "./providers";
import { query } from "./query";
import { activeDocs, indexRuns, now, pipelineHealth, type EvalRunRec, type MockState } from "./store";

export const DEFAULT_K = [1, 3, 5, 10];
/** The id of the seeded "ready" run, so tests and screenshots can point at it. */
export const SEEDED_RUN_ID = "seeded-run";
const MAX_ERRORS = 100;

// ---------- metrics (eval/metrics.py) ----------

export function metricNames(ks: number[]): string[] {
  return [...ks.flatMap((k) => [`hit_rate@${k}`, `recall@${k}`, `precision@${k}`, `ndcg@${k}`]), "mrr"];
}

export function itemMetrics(hits: number[][], nTargets: number, ks: number[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const k of ks) {
    const top = hits.slice(0, k);
    const found = new Set<number>();
    let dcg = 0;
    top.forEach((matched, i) => {
      const first = matched.filter((t) => !found.has(t));
      dcg += first.length / Math.log2(i + 2);
      first.forEach((t) => found.add(t));
    });
    let idcg = 0;
    for (let i = 1; i <= Math.min(nTargets, k); i++) idcg += 1 / Math.log2(i + 1);
    out[`hit_rate@${k}`] = found.size ? 1 : 0;
    out[`recall@${k}`] = nTargets ? found.size / nTargets : 0;
    out[`precision@${k}`] = top.filter((m) => m.length).length / k;
    out[`ndcg@${k}`] = idcg ? dcg / idcg : 0;
  }
  const limit = Math.max(...ks);
  const first = hits.slice(0, limit).findIndex((m) => m.length);
  out.mrr = first >= 0 ? 1 / (first + 1) : 0;
  return out;
}

function percentile(values: number[], q: number): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(1, Math.ceil((q / 100) * sorted.length)) - 1]!;
}

// ---------- matching (eval/matching.py) ----------

const norm = (t: string) => t.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();

export function matchedTargets(chunk: { document_id: string; text: string; page_start: number | null; page_end: number | null }, targets: EvalTarget[]): number[] {
  return targets.flatMap((t, i) => {
    if (chunk.document_id !== t.document_id) return [];
    if (t.pages?.length) {
      if (chunk.page_start === null || chunk.page_end === null) return [];
      if (!t.pages.some((p) => chunk.page_start! <= p && p <= chunk.page_end!)) return [];
    }
    if (t.text_contains && !norm(chunk.text).includes(norm(t.text_contains))) return [];
    return [i];
  });
}

// ---------- paired bootstrap (eval/compare.py) ----------

export function pairedBootstrap(a: number[], b: number[], resamples = 2000, seed = 1729): Omit<EvalComparison, "a" | "b" | "metric"> {
  const n = a.length;
  if (!n) return { mean_a: 0, mean_b: 0, diff: 0, ci_low: 0, ci_high: 0, significant: false };
  const diffs = a.map((x, i) => b[i]! - x);
  const rand = rng(seed);
  const means: number[] = [];
  for (let r = 0; r < resamples; r++) {
    let s = 0;
    for (let i = 0; i < n; i++) s += diffs[Math.floor(rand() * n)]!;
    means.push(s / n);
  }
  means.sort((x, y) => x - y);
  const low = means[Math.floor(0.025 * (resamples - 1))]!;
  const high = means[Math.round(0.975 * (resamples - 1))]!;
  const meanA = a.reduce((s, x) => s + x, 0) / n;
  const meanB = b.reduce((s, x) => s + x, 0) / n;
  return { mean_a: meanA, mean_b: meanB, diff: meanB - meanA, ci_low: low, ci_high: high, significant: low > 0 || high < 0 };
}

// ---------- datasets (eval/datasets.py) ----------

function docIndex(state: MockState, corpusId: string): Map<string, [string, string]> {
  const out = new Map<string, [string, string]>();
  const docs = activeDocs(state, corpusId);
  for (const d of docs) out.set(d.id, [d.id, d.external_id ?? d.filename]);
  for (const d of docs) if (d.external_id) out.set(d.external_id, [d.id, d.external_id]);
  return out;
}

function parseItem(raw: unknown, docs: Map<string, [string, string]>, fields: ReturnType<typeof filterFields>): Omit<EvalItem, "id"> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("each line must be a JSON object");
  const r = raw as Record<string, unknown>;
  const unknown = Object.keys(r).filter((k) => !["question", "targets", "filter", "metadata"].includes(k));
  if (unknown.length) throw new Error(`unknown keys: ${unknown.sort().join(", ")}`);
  if (typeof r.question !== "string" || !r.question.trim()) throw new Error("'question' must be a non-empty string");
  if (!Array.isArray(r.targets) || !r.targets.length) throw new Error("'targets' must be a non-empty list");
  const targets: EvalTarget[] = r.targets.map((t: unknown, i) => {
    const o = t as Record<string, unknown>;
    if (!o || typeof o !== "object" || typeof o.document !== "string") throw new Error(`targets[${i}] needs a 'document' (external_id or id)`);
    const extra = Object.keys(o).filter((k) => !["document", "pages", "text_contains"].includes(k));
    if (extra.length) throw new Error(`targets[${i}] has unknown keys: ${extra.sort().join(", ")}`);
    const doc = docs.get(o.document);
    if (!doc) throw new Error(`targets[${i}]: unknown document '${o.document}'`);
    const pages = o.pages;
    if (pages !== undefined && pages !== null && (!Array.isArray(pages) || !pages.length || !pages.every((p) => Number.isInteger(p) && p >= 1)))
      throw new Error(`targets[${i}].pages must be a non-empty list of page numbers (1-based)`);
    const snippet = o.text_contains;
    if (snippet !== undefined && snippet !== null && (typeof snippet !== "string" || !snippet.trim())) throw new Error(`targets[${i}].text_contains must be a non-empty string`);
    return { document_id: doc[0], document_label: doc[1], pages: (pages as number[] | undefined) ?? null, text_contains: (snippet as string | undefined) ?? null };
  });
  if (r.filter !== undefined && r.filter !== null) {
    try {
      validateFilter(r.filter, fields);
    } catch (e) {
      throw new Error(`invalid filter: ${e instanceof FilterError ? e.message : String(e)}`, { cause: e });
    }
  }
  if (r.metadata !== undefined && (typeof r.metadata !== "object" || Array.isArray(r.metadata) || r.metadata === null)) throw new Error("'metadata' must be an object");
  return { question: r.question, targets, filter: (r.filter as Record<string, unknown>) ?? null, metadata: (r.metadata as Record<string, unknown>) ?? {} };
}

export function importDataset(state: MockState, corpusId: string, name: string, content: string, createdAt = now()): EvalDataset {
  const corpus = state.corpora.find((c) => c.id === corpusId);
  if (!corpus) throw new MockError(404, "not_found", `Corpus ${corpusId} was not found.`);
  if (!name.trim()) throw new MockError(422, "validation_error", "Give the dataset a name.");
  const docs = docIndex(state, corpusId);
  const fields = filterFields(corpus.metadata_schema);
  const items: EvalItem[] = [];
  const errors: { line: number; message: string }[] = [];
  content.split(/\r?\n/).forEach((text, i) => {
    if (!text.trim() || errors.length >= MAX_ERRORS) return;
    let raw: unknown;
    try {
      raw = JSON.parse(text);
    } catch (e) {
      return void errors.push({ line: i + 1, message: `invalid JSON: ${(e as Error).message}` });
    }
    try {
      items.push({ id: uuidFrom(`item:${corpusId}:${name}:${i}:${createdAt}`), ...parseItem(raw, docs, fields) });
    } catch (e) {
      errors.push({ line: i + 1, message: (e as Error).message });
    }
  });
  if (!items.length && !errors.length) errors.push({ line: 0, message: "the file contains no items" });
  if (errors.length) throw new MockError(422, "validation_error", `The dataset has ${errors.length} problem(s); line ${errors[0]!.line}: ${errors[0]!.message}`, { errors });
  if (state.datasets.some((d) => d.corpus_id === corpusId && d.name === name.trim())) throw new MockError(409, "conflict", `A dataset named '${name.trim()}' already exists in this corpus.`);
  const ds: EvalDataset = { id: uuidFrom(`dataset:${corpusId}:${name}:${createdAt}`), corpus_id: corpusId, name: name.trim(), item_count: items.length, created_at: createdAt };
  state.datasets.push(ds);
  state.datasetItems[ds.id] = items;
  return ds;
}

export function importForm(state: MockState, corpusId: string, form: Form): EvalDataset {
  if (!form.file) throw new MockError(422, "validation_error", "Invalid request: file: Field required");
  if (form.file.size > 50 * 1024 * 1024) throw new MockError(422, "validation_error", "The dataset file is larger than 50 MB.");
  return importDataset(state, corpusId, form.fields.name ?? "", form.file.text);
}

export function findDataset(state: MockState, id: string): EvalDataset {
  const d = state.datasets.find((x) => x.id === id);
  if (!d) throw new MockError(404, "not_found", `Dataset ${id} was not found.`);
  return d;
}

export function deleteDataset(state: MockState, id: string): void {
  findDataset(state, id);
  if (state.evalRuns.some((r) => r.dataset_id === id && !r.deferred && (r.status === "pending" || r.status === "running")))
    throw new MockError(409, "conflict", "An evaluation run on this dataset is still in progress.");
  state.datasets = state.datasets.filter((d) => d.id !== id);
  state.evalRuns = state.evalRuns.filter((r) => r.dataset_id !== id);
  delete state.datasetItems[id];
}

// ---------- runs (eval/runner.py) ----------

export function createRun(state: MockState, body: { dataset_id?: string; pipeline_ids?: string[]; k_values?: number[] | null; allow_partial?: boolean }, createdAt = now()): EvalRunRec {
  const ds = findDataset(state, String(body.dataset_id ?? ""));
  const ids = body.pipeline_ids ?? [];
  if (!ids.length) throw new MockError(422, "validation_error", "Choose at least one pipeline.");
  if (new Set(ids).size !== ids.length) throw new MockError(422, "validation_error", "Each pipeline may appear only once in a run.");
  const ks = [...new Set(body.k_values?.length ? body.k_values : DEFAULT_K)].sort((a, b) => a - b);
  if (!ks.every((k) => Number.isInteger(k) && k >= 1 && k <= 1000)) throw new MockError(422, "validation_error", "k values must be between 1 and 1000.");
  const index = indexRuns(state);
  for (const pid of ids) {
    const p = state.pipelines.find((x) => x.id === pid);
    if (!p) throw new MockError(404, "not_found", `Pipeline ${pid} was not found.`);
    if (p.corpus_id !== ds.corpus_id) throw new MockError(422, "validation_error", `Pipeline ${p.name} belongs to another corpus than the dataset.`);
    if (p.archived) throw new MockError(409, "pipeline_archived", `${p.name} is archived.`);
    if (!body.allow_partial) {
      const h = pipelineHealth(state, p, index);
      if (h.status !== "ready")
        throw new MockError(409, "index_incomplete", `${p.name} is ${h.status} (${Math.round(h.coverage * 100)}% coverage). Comparing a partly built index is misleading; wait for it, or set allow_partial.`, {
          pipeline_id: pid,
          status: h.status,
          coverage: h.coverage,
        });
    }
  }
  const itemCount = state.datasetItems[ds.id]?.length ?? 0;
  const total = itemCount * ids.length;
  const run: EvalRunRec = {
    id: uuidFrom(`run:${ds.id}:${ids.join(",")}:${createdAt}:${state.evalRuns.length}`),
    corpus_id: ds.corpus_id,
    dataset_id: ds.id,
    dataset_name: ds.name,
    pipeline_ids: ids,
    k_values: ks,
    status: "pending",
    progress: { done: 0, total },
    item_count: itemCount,
    created_at: createdAt,
    finished_at: null,
    summaries: [],
    results: Object.fromEntries(ids.map((id) => [id, {}])),
    allow_partial: Boolean(body.allow_partial),
    fingerprints: Object.fromEntries(ids.map((id) => [id, state.pipelines.find((p) => p.id === id)!.fingerprint])),
  };
  state.evalRuns.push(run);
  return run;
}

function evaluate(state: MockState, run: EvalRunRec, item: EvalItem, pid: string): EvalItemResult {
  const body: QueryRequest = { query: item.question, pipeline_id: pid, top_k: Math.max(...run.k_values), allow_partial: run.allow_partial, ...(item.filter ? { filter: item.filter } : {}) };
  const labels = new Map(state.documents.map((d) => [d.id, d.external_id ?? d.filename]));
  try {
    const res = query(state, run.corpus_id, body);
    const retrieved = res.results.map((r) => ({
      rank: r.rank,
      chunk_id: r.chunk_id,
      document_id: r.document_id,
      document_label: labels.get(r.document_id) ?? r.document.filename,
      text: r.text,
      page_start: r.page_start,
      page_end: r.page_end,
      matched_targets: matchedTargets({ document_id: r.document_id, text: r.text, page_start: r.page_start, page_end: r.page_end }, item.targets),
    }));
    return {
      pipeline_id: pid,
      metrics: itemMetrics(retrieved.map((x) => x.matched_targets), item.targets.length, run.k_values),
      latency_ms: res.timings_ms.total ?? 0,
      error: null,
      retrieved,
    };
  } catch (e) {
    const err = e as MockError;
    return { pipeline_id: pid, metrics: Object.fromEntries(metricNames(run.k_values).map((m) => [m, 0])), latency_ms: null, error: `${err.code ?? "error"}: ${err.message}`, retrieved: [] };
  }
}

function summarise(state: MockState, run: EvalRunRec): EvalSummary[] {
  const items = state.datasetItems[run.dataset_id] ?? [];
  const index = indexRuns(state);
  return run.pipeline_ids.map((pid) => {
    const p = state.pipelines.find((x) => x.id === pid);
    const rows = items.map((it) => run.results[pid]![it.id]!).filter(Boolean);
    const metrics = Object.fromEntries(metricNames(run.k_values).map((m) => [m, rows.reduce((s, r) => s + (r.metrics[m] ?? 0), 0) / Math.max(1, rows.length)]));
    const lat = rows.filter((r) => r.error === null).map((r) => r.latency_ms ?? 0);
    const chunkNode = p?.nodes.find((n) => n.type === "chunk");
    let count = 0;
    let chars = 0;
    if (p && chunkNode)
      for (const d of activeDocs(state, run.corpus_id))
        for (const c of chunksOf(state, d, p.id, chunkNode.node_id, index)) {
          count += 1;
          chars += c.char_count;
        }
    return {
      pipeline_id: pid,
      pipeline_name: p?.name ?? "(deleted)",
      pipeline_fingerprint: run.fingerprints[pid] ?? "",
      pipeline_archived: p?.archived ?? true,
      metrics,
      latency_ms: { p50: percentile(lat, 50), p95: percentile(lat, 95) },
      error_count: rows.filter((r) => r.error !== null).length,
      chunk_count: count,
      mean_chunk_chars: count ? chars / count : 0,
    };
  });
}

/** Evaluate up to `budget` rows of unfinished runs; mark runs finished when all rows exist. */
export function advanceRuns(state: MockState, budget = 12, only?: EvalRunRec): boolean {
  let changed = false;
  for (const run of only ? [only] : state.evalRuns) {
    if (run.deferred || (run.status !== "pending" && run.status !== "running")) continue;
    run.status = "running";
    changed = true;
    const items = state.datasetItems[run.dataset_id] ?? [];
    outer: for (const it of items)
      for (const pid of run.pipeline_ids) {
        if (run.results[pid]![it.id]) continue;
        if (budget-- <= 0) break outer;
        run.results[pid]![it.id] = evaluate(state, run, it, pid);
        run.progress.done += 1;
      }
    if (run.progress.done >= run.progress.total) {
      run.summaries = summarise(state, run);
      run.status = "finished";
      run.finished_at = now();
    }
    if (budget <= 0) break;
  }
  return changed;
}

export function findRun(state: MockState, id: string): EvalRunRec {
  const r = state.evalRuns.find((x) => x.id === id);
  if (!r) throw new MockError(404, "not_found", `Evaluation run ${id} was not found.`);
  return r;
}

export function runOut(run: EvalRunRec): EvalRun {
  const { id, corpus_id, dataset_id, dataset_name, pipeline_ids, k_values, created_at, progress } = run;
  const itemCount = progress.total / Math.max(1, pipeline_ids.length);
  if (run.deferred) return { id, corpus_id, dataset_id, dataset_name, pipeline_ids, k_values, status: "finished", progress: { done: progress.total, total: progress.total }, item_count: itemCount, created_at, finished_at: run.deferred.finishedAt };
  return { id, corpus_id, dataset_id, dataset_name, pipeline_ids, k_values, status: run.status, progress: { ...progress }, item_count: itemCount, created_at, finished_at: run.finished_at };
}

/** A seeded run that reads as finished; its results are computed on first use. */
export function seedFinishedRun(state: MockState, body: Parameters<typeof createRun>[1], createdMinutesAgo: number, tookMinutes = 2): EvalRunRec {
  const created = new Date(Date.now() - createdMinutesAgo * 60_000).toISOString();
  const run = createRun(state, body, created);
  run.deferred = { finishedAt: new Date(Date.now() - (createdMinutesAgo - tookMinutes) * 60_000).toISOString() };
  return run;
}

function materialise(state: MockState, run: EvalRunRec): void {
  if (!run.deferred) return;
  const finishedAt = run.deferred.finishedAt;
  delete run.deferred;
  while (run.status !== "finished") advanceRuns(state, 1000, run);
  run.finished_at = finishedAt;
}

export function runDetail(state: MockState, run: EvalRunRec): EvalRunDetail {
  materialise(state, run);
  const out: EvalRunDetail = { ...runOut(run), summaries: [], comparisons: [] };
  if (run.status !== "finished") return out;
  out.summaries = run.summaries.map((s) => {
    const p = state.pipelines.find((x) => x.id === s.pipeline_id);
    return { ...s, pipeline_name: p?.name ?? s.pipeline_name, pipeline_archived: p?.archived ?? true };
  });
  if (!run.comparisons) {
    const items = state.datasetItems[run.dataset_id] ?? [];
    const comps: EvalComparison[] = [];
    run.pipeline_ids.forEach((a, i) =>
      run.pipeline_ids.slice(i + 1).forEach((b) => {
        for (const metric of metricNames(run.k_values)) {
          const va = items.map((it) => run.results[a]![it.id]?.metrics[metric] ?? 0);
          const vb = items.map((it) => run.results[b]![it.id]?.metrics[metric] ?? 0);
          comps.push({ a, b, metric, ...pairedBootstrap(va, vb) });
        }
      }),
    );
    run.comparisons = comps;
  }
  out.comparisons = run.comparisons;
  return out;
}

export function itemComparisons(state: MockState, run: EvalRunRec, a: string, b: string, metric: string, only: string | null): EvalItemComparison[] {
  materialise(state, run);
  for (const pid of [a, b]) if (!run.pipeline_ids.includes(pid)) throw new MockError(422, "validation_error", `Pipeline ${pid} is not part of this run.`);
  if (!metricNames(run.k_values).includes(metric)) throw new MockError(422, "validation_error", `Unknown metric '${metric}' for this run.`);
  if (only !== null && !["differs", "improved", "regressed", "unchanged"].includes(only)) throw new MockError(422, "validation_error", "only must be differs, improved, regressed or unchanged.");
  const out: EvalItemComparison[] = [];
  for (const item of state.datasetItems[run.dataset_id] ?? []) {
    const ra = run.results[a]![item.id];
    const rb = run.results[b]![item.id];
    if (!ra || !rb) continue;
    const va = ra.metrics[metric] ?? 0;
    const vb = rb.metrics[metric] ?? 0;
    const outcome = vb > va ? "improved" : vb < va ? "regressed" : "unchanged";
    if (only === "differs" && outcome === "unchanged") continue;
    if (only && only !== "differs" && outcome !== only) continue;
    out.push({ item, a: ra, b: rb, outcome });
  }
  return out;
}
