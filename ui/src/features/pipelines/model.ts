import type { Pipeline, PipelineState } from "../../api/types";

const SEVERITY: Record<PipelineState, number> = { degraded: 0, building: 1, ready: 2 };

export const NODE_LABEL: Record<string, string> = {
  parse: "Parse",
  chunk: "Chunk",
  enrich: "Enrich",
  embed: "Embed",
  index_vector: "Vector index",
  index_fts: "Keyword index",
};

/** Order a corpus's pipelines so problems come first ("is anything broken?"). */
export function sortPipelines(items: Pipeline[]): Pipeline[] {
  return [...items].sort(
    (a, b) =>
      SEVERITY[a.status] - SEVERITY[b.status] ||
      Number(b.is_active) - Number(a.is_active) ||
      b.created_at.localeCompare(a.created_at),
  );
}
