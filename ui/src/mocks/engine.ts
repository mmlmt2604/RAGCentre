// Mock ingestion engine: pipeline creation, the worker simulation, and retries.
import type { PipelineConfig } from "../api/types";
import { uuidFrom } from "./hash";
import {
  MAX_ATTEMPTS,
  ago,
  indexRuns,
  now,
  planRuns,
  resolvePipeline,
  runKey,
  type MockState,
  type PipelineRec,
  type RunRec,
} from "./store";

export class MockValidationError extends Error {
  constructor(readonly problems: ReturnType<typeof resolvePipeline>["problems"]) {
    super("invalid_pipeline");
  }
}

export function createPipeline(state: MockState, corpusId: string, config: PipelineConfig, createdMinutesAgo = 0): PipelineRec {
  const res = resolvePipeline(state, config);
  if (res.problems.length || !res.fingerprint) throw new MockValidationError(res.problems);
  const rec: PipelineRec = {
    id: uuidFrom(`pipeline:${corpusId}:${config.name}`),
    corpus_id: corpusId,
    name: config.name,
    config,
    fingerprint: res.fingerprint,
    archived: false,
    created_at: ago(createdMinutesAgo),
    nodes: res.nodes,
  };
  state.pipelines.push(rec);
  planRuns(state, corpusId, rec.nodes);
  refreshProviderUsage(state);
  return rec;
}

export function refreshProviderUsage(state: MockState): void {
  for (const p of state.providers) {
    p.used_by_pipelines = state.pipelines.filter(
      (pl) => !pl.archived && JSON.stringify(pl.config).includes(`"provider":"${p.name}"`),
    ).length;
  }
}

function statsFor(state: MockState, run: RunRec): Record<string, unknown> {
  const doc = state.documents.find((d) => d.id === run.document_id);
  const n = doc?.chunk_texts.length ?? 4;
  const duration_ms = Math.round(
    { parse: 900, chunk: 40, enrich: 2400, embed: 600, index_vector: 30, index_fts: 12 }[run.node_type] ?? 50,
  ) * (0.6 + ((run.id * 37) % 10) / 10);
  switch (run.node_type) {
    case "parse":
      return { duration_ms, cache_hit: false, blocks: n * 6 };
    case "chunk":
      return { duration_ms, n_chunks: n };
    case "enrich":
      return { duration_ms, n_chunks: n, n_cache_hits: 0, n_llm_calls: n, tokens: n * 310 };
    case "embed":
      return { duration_ms, n_texts: n, n_cache_hits: 0 };
    default:
      return { duration_ms, n_records: n };
  }
}

export function succeed(state: MockState, run: RunRec, minutesAgo = 0): void {
  run.status = "succeeded";
  run.attempt = Math.max(1, run.attempt);
  run.started_at = ago(minutesAgo + 0.05);
  run.finished_at = ago(minutesAgo);
  run.stats = statsFor(state, run);
  run.error_code = null;
  run.error_message = null;
}

export function fail(run: RunRec, code: string, message: string, minutesAgo = 0): void {
  run.status = "failed";
  run.attempt = MAX_ATTEMPTS;
  run.started_at = ago(minutesAgo + 0.1);
  run.finished_at = ago(minutesAgo);
  run.error_code = code;
  run.error_message = message;
  run.will_fail = null;
}

/** Mark every run of these node fingerprints succeeded, for documents in `docIds` (or all). */
export function completeRuns(state: MockState, nodeFps: string[], docIds?: Set<string>, minutesAgo = 60): void {
  for (const run of state.runs) {
    if (!nodeFps.includes(run.node_fp)) continue;
    if (docIds && !docIds.has(run.document_id)) continue;
    succeed(state, run, minutesAgo);
  }
}

/** Reset failed runs to pending. Returns the number reset. */
export function retryRuns(runs: RunRec[]): number {
  let n = 0;
  for (const run of runs) {
    if (run.status !== "failed") continue;
    run.status = "pending";
    run.attempt = 0;
    run.error_code = null;
    run.error_message = null;
    run.will_fail = null;
    run.run_after = now();
    n += 1;
  }
  return n;
}

/** One step of the worker simulation: finish running runs, then claim ready ones. */
export function tick(state: MockState): boolean {
  const index = indexRuns(state);
  let changed = false;

  for (const run of state.runs) {
    if (run.status !== "running") continue;
    changed = true;
    if (run.will_fail) fail(run, run.will_fail.code, run.will_fail.message);
    else succeed(state, run);
  }

  let budget = state.throughput;
  for (const run of state.runs) {
    if (budget <= 0) break;
    if (run.status !== "pending") continue;
    const ready = run.upstream.every((up) => index.get(runKey(run.document_id, up))?.status === "succeeded");
    if (!ready) continue;
    run.status = "running";
    run.attempt += 1;
    run.started_at = now();
    budget -= 1;
    changed = true;
  }
  return changed;
}
