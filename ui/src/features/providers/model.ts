import type { Provider, ProviderKind } from "../../api/types";

export const KINDS: ProviderKind[] = ["parser", "chunker", "embedder", "reranker", "vector_store", "llm", "connector"];

export const KIND_LABEL: Record<ProviderKind, { one: string; many: string; blurb: string }> = {
  parser: { one: "Parser", many: "Parsers", blurb: "Turn uploaded files into structured text. Plain text and Markdown work out of the box." },
  chunker: { one: "Chunker", many: "Chunkers", blurb: "Optional custom chunking services. Five chunk strategies are built in." },
  embedder: { one: "Embedder", many: "Embedders", blurb: "Turn chunks and queries into vectors for dense search." },
  reranker: { one: "Reranker", many: "Rerankers", blurb: "Optionally reorder retrieved chunks by relevance at query time." },
  llm: { one: "Language model", many: "Language models", blurb: "Chat-completion models that write chunk enrichment text and plan query transforms." },
  vector_store: { one: "Vector store", many: "Vector stores", blurb: "Hold vectors and answer nearest-neighbour searches." },
  connector: { one: "Data connector", many: "Data connectors", blurb: "Pull documents into a corpus from a connected service on a schedule." },
};

/** Secrets summary for a row: "api_key set" / "api_key not set". */
export function secretSummary(p: Provider): { key: string; set: boolean; detail: string }[] {
  return Object.entries(p.secrets).map(([key, s]) => ({
    key,
    set: s.set,
    detail: !s.set ? "not set" : s.source === "env" ? `from $${s.env_var ?? "?"}` : "stored, encrypted",
  }));
}
