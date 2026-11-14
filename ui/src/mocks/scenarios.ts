// Named starting states for the mock API (IMPLEMENTATION_PLAN.md §21.3).
import { completeRuns, createPipeline, fail, refreshProviderUsage } from "./engine";
import { SEEDED_RUN_ID, seedFinishedRun, importDataset } from "./eval";
import { allProviders, builtinText, corpus, documents, goldenJsonl, hybrid, providerTypes } from "./fixtures";
import { emptyState, type MockState } from "./store";

export const SCENARIOS = ["empty", "building", "degraded", "ready", "auth"] as const;
export type ScenarioName = (typeof SCENARIOS)[number];

export const SCENARIO_LABELS: Record<ScenarioName, string> = {
  empty: "Empty — first run",
  building: "Building — ingestion in progress",
  degraded: "Degraded — failed and blocked runs",
  ready: "Ready — everything built",
  auth: "Auth — token required",
};

export const DEV_TOKEN = "dev-token";

export function buildScenario(name: ScenarioName): MockState {
  const state = emptyState();
  state.providerTypes = providerTypes;

  if (name === "empty") {
    // A fresh install: no RAGCENTRE_SECRET_KEY yet, so only environment-variable secrets work.
    state.providers = [builtinText()];
    state.secretStorage = false;
    return state;
  }

  state.providers = allProviders();
  const support = corpus("support-docs", undefined, 34);
  const contracts = corpus("contracts-2025", { fields: { party: { type: "str", default: "" }, year: { type: "int", default: 2025 } } }, 9);
  const wiki = corpus("eng-wiki", { fields: {} }, 2);
  state.corpora.push(support, contracts, wiki);
  state.documents.push(...documents(support, 640, 7), ...documents(contracts, 212, 11), ...documents(wiki, 38, 13));

  // support-docs
  const baseline = createPipeline(state, support.id, hybrid("baseline-hybrid"), 60 * 24 * 12);
  support.active_pipeline_id = baseline.id;
  completeRuns(state, baseline.nodes.map((n) => n.node_fp), undefined, 60 * 24 * 11);
  const rerank = createPipeline(state, support.id, hybrid("rerank-jina", { rerank: true }), 60 * 24 * 5);
  completeRuns(state, rerank.nodes.map((n) => n.node_fp));

  const structure = createPipeline(state, support.id, hybrid("structure-1200", { chunk: "structure", maxChars: 1200 }), name === "ready" ? 60 * 26 : 180);
  const supportDocs = state.documents.filter((d) => d.corpus_id === support.id);
  const newFps = structure.nodes.filter((n) => n.type !== "parse").map((n) => n.node_fp);
  if (name === "ready") completeRuns(state, newFps, undefined, 60 * 25);
  else completeRuns(state, newFps, new Set(supportDocs.slice(0, Math.round(supportDocs.length * 0.31)).map((d) => d.id)), 5);

  if (name !== "building") {
    const swap = createPipeline(state, support.id, hybrid("bge-m3-swap", { embedder: "bge-m3" }), 60 * 30);
    const swapFps = swap.nodes.map((n) => n.node_fp);
    completeRuns(state, swapFps, undefined, 60 * 29);
    if (name === "degraded") {
      const embedFp = swap.nodes.find((n) => n.type === "embed")!.node_fp;
      const failing = state.runs.filter((r) => r.node_fp === embedFp).slice(40, 54);
      const failingDocs = new Set(failing.map((r) => r.document_id));
      for (const run of state.runs) {
        if (!failingDocs.has(run.document_id)) continue;
        if (run.node_fp === embedFp) fail(run, "provider_error", "bge-m3 at http://gpu-02:8080/v1 returned 503 Service Unavailable after 5 attempts.", 40);
        else if (run.node_type === "index_vector" && swapFps.includes(run.node_fp)) {
          run.status = "pending";
          run.stats = null;
          run.started_at = null;
          run.finished_at = null;
        }
      }
    }
  }

  // contracts-2025: one ready pipeline on Qdrant
  const contractsCfg = hybrid("contracts-qdrant", { chunk: "structure", maxChars: 1600 });
  contractsCfg.nodes = contractsCfg.nodes.map((n) => (n.type === "index_vector" ? { ...n, provider: "qdrant-main" } : n));
  const cp = createPipeline(state, contracts.id, contractsCfg, 60 * 24 * 8);
  contracts.active_pipeline_id = cp.id;
  completeRuns(state, cp.nodes.map((n) => n.node_fp), undefined, 60 * 24 * 7);

  // An evaluation dataset for support-docs; in "ready", a finished run comparing two chunkers.
  const golden = importDataset(state, support.id, "support-golden", goldenJsonl(supportDocs), new Date(Date.now() - 60 * 24 * 3 * 60_000).toISOString());
  if (name === "ready") {
    const finished = seedFinishedRun(state, { dataset_id: golden.id, pipeline_ids: [baseline.id, structure.id] }, 60 * 20);
    finished.id = SEEDED_RUN_ID;
  }

  if (name === "degraded") {
    state.serviceFaults["bge-m3"] = { down: "POST http://gpu-02:8080/v1/embeddings returned 503 Service Unavailable" };
    state.serviceFaults["docling"] = { version: "2.15.1" };
  }

  refreshProviderUsage(state);
  if (name === "auth") state.authToken = DEV_TOKEN;
  return state;
}
