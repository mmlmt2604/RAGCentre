// Turn mock records into the API shapes of IMPLEMENTATION_PLAN.md §22.
import type {
  Corpus,
  Document,
  DocumentIngestion,
  Health,
  NodeRun,
  Pipeline,
  PipelineStatus,
  PipelineSummary,
  RunState,
} from "../api/types";
import {
  MAX_ATTEMPTS,
  activeDocs,
  blockingRun,
  effectiveStatus,
  indexRuns,
  pipelineHealth,
  runKey,
  sharedWith,
  type CorpusRec,
  type DocRec,
  type MockState,
  type PipelineRec,
  type RunRec,
} from "./store";

export type RunIndex = Map<string, RunRec>;

export function health(state: MockState): Health {
  return {
    status: "ok",
    version: "0.1.0-mock",
    migration: 5,
    workers: 4,
    auth_required: state.account !== null || state.adminToken !== null,
    secret_storage_enabled: state.secretStorage,
  };
}

export function pipelineSummary(state: MockState, p: PipelineRec, index: RunIndex): PipelineSummary {
  const h = pipelineHealth(state, p, index);
  return {
    id: p.id,
    name: p.name,
    fingerprint: p.fingerprint,
    archived: p.archived,
    status: h.status,
    coverage: h.coverage,
    created_at: p.created_at,
  };
}

export function pipeline(state: MockState, p: PipelineRec, index: RunIndex): Pipeline {
  const c = state.corpora.find((x) => x.id === p.corpus_id);
  return {
    ...pipelineSummary(state, p, index),
    corpus_id: p.corpus_id,
    config: p.config,
    is_active: c?.active_pipeline_id === p.id,
    nodes: p.nodes.map((n) => ({
      node_id: n.node_id,
      type: n.type,
      node_fp: n.node_fp,
      shared_with: sharedWith(state, p.corpus_id, n.node_fp, p.id),
    })),
  };
}

export function corpus(state: MockState, c: CorpusRec, index: RunIndex): Corpus {
  return {
    id: c.id,
    name: c.name,
    metadata_schema: c.metadata_schema,
    active_pipeline_id: c.active_pipeline_id,
    document_count: activeDocs(state, c.id).length,
    pipelines: state.pipelines
      .filter((p) => p.corpus_id === c.id && !p.archived)
      .map((p) => pipelineSummary(state, p, index)),
    created_at: c.created_at,
  };
}

export function nodeRun(state: MockState, run: RunRec, index: RunIndex): NodeRun {
  const doc = state.documents.find((d) => d.id === run.document_id);
  const status = effectiveStatus(run, index);
  const blocker = status === "blocked" ? blockingRun(run, index) : null;
  return {
    id: run.id,
    corpus_id: run.corpus_id,
    document_id: run.document_id,
    document_filename: doc?.filename ?? "(deleted)",
    node_fp: run.node_fp,
    node_type: run.node_type,
    pipelines: state.pipelines
      .filter((p) => p.corpus_id === run.corpus_id && !p.archived)
      .flatMap((p) => p.nodes.filter((n) => n.node_fp === run.node_fp).map((n) => ({ pipeline_id: p.id, pipeline_name: p.name, node_id: n.node_id }))),
    status,
    attempt: run.attempt,
    max_attempts: MAX_ATTEMPTS,
    blocked_by_run_id: blocker?.id ?? null,
    error_code: run.error_code,
    error_message: run.error_message,
    stats: run.stats,
    run_after: run.run_after,
    started_at: run.started_at,
    finished_at: run.finished_at,
  };
}

export function pipelineStatus(state: MockState, p: PipelineRec, index: RunIndex): PipelineStatus {
  const h = pipelineHealth(state, p, index);
  const fps = new Set(p.nodes.map((n) => n.node_fp));
  const failures = state.runs
    .filter((r) => r.corpus_id === p.corpus_id && r.status === "failed" && fps.has(r.node_fp))
    .sort((a, b) => (b.finished_at ?? "").localeCompare(a.finished_at ?? ""))
    .slice(0, 20)
    .map((r) => nodeRun(state, r, index));
  return {
    pipeline_id: p.id,
    status: h.status,
    coverage: h.coverage,
    document_count: activeDocs(state, p.corpus_id).length,
    nodes: p.nodes.map((n) => ({
      node_id: n.node_id,
      type: n.type,
      node_fp: n.node_fp,
      counts: h.counts.get(n.node_fp) ?? { pending: 0, running: 0, succeeded: 0, failed: 0, blocked: 0 },
    })),
    recent_failures: failures,
  };
}

export function document(state: MockState, d: DocRec, index: RunIndex): Document {
  const ingestion: DocumentIngestion[] = state.pipelines
    .filter((p) => p.corpus_id === d.corpus_id && !p.archived)
    .map((p) => {
      const nodes = p.nodes.map((n) => {
        const run = index.get(runKey(d.id, n.node_fp));
        return {
          node_id: n.node_id,
          node_fp: n.node_fp,
          run_id: run?.id ?? 0,
          status: run ? effectiveStatus(run, index) : ("pending" as RunState),
          error_code: run?.error_code ?? null,
        };
      });
      const ss = nodes.map((n) => n.status);
      const state_: RunState = ss.includes("failed")
        ? "failed"
        : ss.includes("running")
            ? "running"
            : ss.every((s) => s === "succeeded")
              ? "succeeded"
              : "pending";
      return { pipeline_id: p.id, state: state_, nodes };
    });
  const { chunk_texts: _unused, content: _content, ...rest } = d;
  void _unused;
  void _content;
  return { ...rest, ingestion };
}

export { indexRuns };
