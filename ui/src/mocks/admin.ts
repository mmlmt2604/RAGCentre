// Mock garbage collection (POST /v1/admin/gc), following src/ragcentre/pipeline/gc.py: a preview
// by default; a real run drops work whose node no non-archived pipeline uses. The mock has no
// blobs, so blob counts are always zero.
import type { GcReport } from "../api/types";
import { clearChunksFor } from "./documents";
import { uuidFrom } from "./hash";
import type { MockState } from "./store";

function livePairs(state: MockState): Set<string> {
  const live = new Set<string>();
  for (const p of state.pipelines) {
    if (p.archived) continue;
    for (const n of p.nodes) live.add(`${p.corpus_id}|${n.node_fp}`);
  }
  return live;
}

export function collect(state: MockState, dryRun: boolean): GcReport {
  const live = livePairs(state);
  const indexes = new Map<string, GcReport["indexes"][number]>();
  for (const p of state.pipelines) {
    for (const n of p.nodes) {
      if (n.type !== "index_vector" && n.type !== "index_fts") continue;
      const key = `${p.corpus_id}|${n.node_fp}`;
      if (live.has(key)) continue;
      indexes.set(key, {
        id: uuidFrom(`idx:${key}`),
        kind: n.type === "index_vector" ? "vector" : "fts",
        physical_name: `${n.type === "index_vector" ? "vec" : "fts"}_${n.node_fp.slice(0, 8)}`,
        corpus_id: p.corpus_id,
      });
    }
  }
  // Node-work pairs: runs whose node no non-archived pipeline uses. Skip pairs with a running run.
  const dead = new Map<string, { corpus_id: string; node_fp: string }>();
  const running = new Set<string>();
  for (const r of state.runs) {
    const key = `${r.corpus_id}|${r.node_fp}`;
    if (r.status === "running") running.add(key);
    else if (!live.has(key) && !running.has(key)) dead.set(key, { corpus_id: r.corpus_id, node_fp: r.node_fp });
  }
  if (!dryRun) {
    const keys = new Set(dead.keys());
    if (keys.size) state.runs = state.runs.filter((r) => !keys.has(`${r.corpus_id}|${r.node_fp}`));
    for (const { corpus_id, node_fp } of dead.values()) clearChunksFor(corpus_id, node_fp);
  }
  return {
    dry_run: dryRun,
    indexes: [...indexes.values()],
    node_count: dead.size,
    blob_count: 0,
    blob_bytes: 0,
  };
}
