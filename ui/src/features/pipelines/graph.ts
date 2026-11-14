// Pipeline configuration helpers shared by the builder, detail and diff screens.
import type { PipelineConfig, PipelineDiff, PipelineNode } from "../../api/types";

export type NodeType = PipelineNode["type"];
export const COLUMNS: { key: string; label: string; types: NodeType[] }[] = [
  { key: "parse", label: "Parse", types: ["parse"] },
  { key: "chunk", label: "Chunk", types: ["chunk"] },
  { key: "embed", label: "Embed", types: ["embed"] },
  { key: "index", label: "Index", types: ["index_vector", "index_fts"] },
];

/** What each node type reads from. */
export const INPUT_TYPE: Record<NodeType, NodeType | null> = { parse: null, chunk: "parse", embed: "chunk", index_vector: "embed", index_fts: "chunk" };

export function inputsOf(n: PipelineNode): string[] {
  return "inputs" in n && Array.isArray(n.inputs) ? n.inputs : [];
}

/** One line describing a node's key settings. */
export function nodeSummary(n: PipelineNode): string {
  switch (n.type) {
    case "parse":
      return n.routes.map((r) => `${r.mime} → ${r.provider || "?"}`).join(", ") || "no routes";
    case "chunk": {
      const o = (n.options ?? {}) as Record<string, unknown>;
      const size = typeof o.max_chars === "number" ? ` ${o.max_chars}` : "";
      return `${n.strategy}${size}${n.strategy === "http" && n.provider ? ` via ${n.provider}` : ""}`;
    }
    case "embed":
      return n.provider || "no embedder";
    case "index_vector":
      return `${n.provider || "no store"} · ${n.options?.metric ?? "cosine"}`;
    case "index_fts":
      return `keyword · ${n.options?.tokenizer ?? "unicode61"}`;
  }
}

/** A fresh node id like "chunk_2" not used in the config. */
export function freshId(config: PipelineConfig, base: string): string {
  const used = new Set(config.nodes.map((n) => n.id));
  if (!used.has(base)) return base;
  for (let i = 2; ; i++) if (!used.has(`${base}_${i}`)) return `${base}_${i}`;
}

export function blankNode(type: NodeType, config: PipelineConfig): PipelineNode {
  const want = INPUT_TYPE[type];
  const input = want ? config.nodes.filter((n) => n.type === want).at(-1)?.id : undefined;
  const base = { parse: "parse", chunk: "chunk", embed: "embed", index_vector: "vec", index_fts: "fts" }[type];
  const id = freshId(config, base);
  switch (type) {
    case "parse":
      return { id, type, routes: [{ mime: "text/*", provider: "builtin-text" }] };
    case "chunk":
      return { id, type, inputs: [input ?? ""], strategy: "recursive", options: { max_chars: 1000, overlap_chars: 100 } };
    case "embed":
      return { id, type, inputs: [input ?? ""], provider: "" };
    case "index_vector":
      return { id, type, inputs: [input ?? ""], provider: "", options: { metric: "cosine" } };
    case "index_fts":
      return { id, type, inputs: [input ?? ""], options: { tokenizer: "unicode61" } };
  }
}

/** Remove a node, its downstream dependants keep a dangling input (shown as a problem), and sources pointing at it. */
export function removeNode(config: PipelineConfig, id: string): PipelineConfig {
  const nodes = config.nodes.filter((n) => n.id !== id);
  const sources = config.retrieval.sources.filter((s) => s.index !== id);
  const retrieval = { ...config.retrieval, sources };
  if (sources.length < 2) delete retrieval.fusion;
  return { ...config, nodes, retrieval };
}

/** Rename a node and every reference to it. */
export function renameNode(config: PipelineConfig, from: string, to: string): PipelineConfig {
  return {
    ...config,
    nodes: config.nodes.map((n) => {
      const renamed = n.id === from ? { ...n, id: to } : n;
      return "inputs" in renamed && renamed.inputs ? ({ ...renamed, inputs: renamed.inputs.map((x) => (x === from ? to : x)) } as PipelineNode) : renamed;
    }),
    retrieval: { ...config.retrieval, sources: config.retrieval.sources.map((s) => (s.index === from ? { ...s, index: to } : s)) },
  };
}

// ---------- templates (§21.5: dense only; hybrid; hybrid with reranker) ----------

export type TemplateKey = "blank" | "dense" | "hybrid" | "rerank";

export function template(key: TemplateKey, providers: { embedder?: string; store?: string; reranker?: string }): PipelineConfig {
  const parse: PipelineNode = { id: "parse", type: "parse", routes: [{ mime: "text/*", provider: "builtin-text" }] };
  const chunk: PipelineNode = { id: "chunk", type: "chunk", inputs: ["parse"], strategy: "recursive", options: { max_chars: 1000, overlap_chars: 100 } };
  const embed: PipelineNode = { id: "embed", type: "embed", inputs: ["chunk"], provider: providers.embedder ?? "" };
  const vec: PipelineNode = { id: "vec", type: "index_vector", inputs: ["embed"], provider: providers.store ?? "", options: { metric: "cosine" } };
  const fts: PipelineNode = { id: "fts", type: "index_fts", inputs: ["chunk"], options: { tokenizer: "unicode61" } };
  if (key === "blank") return { name: "", nodes: [parse], retrieval: { sources: [], top_k: 8 } };
  if (key === "dense") return { name: "", nodes: [parse, chunk, embed, vec], retrieval: { sources: [{ id: "dense", index: "vec", top_k: 30 }], top_k: 8 } };
  const hybrid: PipelineConfig = {
    name: "",
    nodes: [parse, chunk, embed, vec, fts],
    retrieval: {
      sources: [
        { id: "dense", index: "vec", top_k: 30 },
        { id: "sparse", index: "fts", top_k: 30 },
      ],
      fusion: { method: "rrf", k: 60 },
      top_k: 8,
    },
  };
  if (key === "rerank") hybrid.retrieval.rerank = { provider: providers.reranker ?? "", top_n: 30 };
  return hybrid;
}

/** A configuration from a stored pipeline, ready to edit as a clone. */
export function cloneConfig(config: PipelineConfig, existingNames: Set<string>): PipelineConfig {
  const copy = structuredClone(config);
  const base = copy.name.replace(/-copy(-\d+)?$/, "");
  let name = `${base}-copy`;
  for (let i = 2; existingNames.has(name); i++) name = `${base}-copy-${i}`;
  return { ...copy, name };
}

/** A problem's path ("nodes[2].options.max_chars") -> the node index it names, if any. */
export function problemNodeIndex(path: string): number | null {
  const m = /^nodes\[(\d+)\]/.exec(path);
  return m ? Number(m[1]) : null;
}

const STAGE: Record<string, string> = { parse: "Parse", chunk: "Chunk", embed: "Embed", index_vector: "Index", index_fts: "Index" };

function sentence(xs: string[]): string {
  const words = xs.map((s, i) => (i ? s.toLowerCase() : s));
  return words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`;
}

/** "Parse is shared. Chunk, embed and index differ." — the practical consequence of a diff. */
export function summariseDiff(d: PipelineDiff): string {
  const stage = (t: string) => STAGE[t] ?? t;
  const stages = [...new Set(d.nodes.map((n) => stage(n.type)))];
  const shared = stages.filter((s) => d.nodes.filter((n) => stage(n.type) === s).every((n) => n.same));
  const differ = stages.filter((s) => !shared.includes(s));
  const parts: string[] = [];
  if (shared.length) parts.push(`${sentence(shared)} ${shared.length === 1 ? "is" : "are"} shared.`);
  if (differ.length) parts.push(`${sentence(differ)} ${differ.length === 1 ? "differs" : "differ"}.`);
  else parts.push("Every step is identical, so they share all their work.");
  parts.push(d.retrieval_same ? "Retrieval settings are the same." : `Retrieval differs in ${d.retrieval_changes.map((c) => c.path).join(", ")}.`);
  return parts.join(" ");
}

