// First-run setup: the choices the welcome flow collects and the requests that realise them.
import type { OcrService, PipelineConfig, Provider, ProviderCreate } from "../../api/types";
import { PROVIDER_NAME_RE } from "../../lib/configSchema";

const KEY = "ragcentre.setup";

/** "done" or "skipped" once the visitor has finished or dismissed the welcome flow. */
export function setupSeen(): boolean {
  try {
    return Boolean(localStorage.getItem(KEY));
  } catch {
    return false;
  }
}

export function markSetupSeen(how: "done" | "skipped") {
  try {
    localStorage.setItem(KEY, how);
  } catch {
    /* storage unavailable: the flow may show again next visit */
  }
}

// ---------- Choices ----------

/** A provider the flow either reuses by name or creates from a request. */
export type Pick = { reuse: string } | { create: ProviderCreate };

export type ParserChoice = { mode: "text" } | { mode: "shim"; service: OcrService } | { mode: "url"; url: string } | { mode: "existing"; name: string };
export type EmbedderChoice = { mode: "hash" } | { mode: "openai"; url: string; model: string } | { mode: "existing"; name: string };
export type StoreChoice = { mode: "sqlite" } | { mode: "qdrant"; url: string } | { mode: "chroma"; host: string; port: string } | { mode: "existing"; name: string };
export type Retrieval = "hybrid" | "dense";

export interface Choices {
  parser: ParserChoice;
  embedder: EmbedderChoice;
  store: StoreChoice;
  corpus: string;
  pipeline: string;
  retrieval: Retrieval;
}

export function defaultChoices(providers: Provider[], services: OcrService[]): Choices {
  const shim = services.find((s) => !s.provider_id && s.state !== "engine_down" && s.state !== "offline");
  const existingParser = providers.find((p) => p.kind === "parser" && !p.builtin);
  const embedder = providers.find((p) => p.kind === "embedder");
  const store = providers.find((p) => p.kind === "vector_store");
  return {
    parser: existingParser ? { mode: "existing", name: existingParser.name } : shim ? { mode: "shim", service: shim } : { mode: "text" },
    embedder: embedder ? { mode: "existing", name: embedder.name } : { mode: "hash" },
    store: store ? { mode: "existing", name: store.name } : { mode: "sqlite" },
    corpus: "",
    pipeline: "first-pipeline",
    retrieval: "hybrid",
  };
}

// ---------- Validation, one step at a time ----------

const isUrl = (s: string) => {
  try {
    const u = new URL(s.trim());
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
};

export type Errors = Record<string, string>;

export function validateStep(step: number, c: Choices, takenCorpora: Set<string>): Errors {
  const e: Errors = {};
  if (step === 1 && c.parser.mode === "url" && !isUrl(c.parser.url)) e.parserUrl = "Enter the service's http:// or https:// address.";
  if (step === 2 && c.embedder.mode === "openai") {
    if (!isUrl(c.embedder.url)) e.embedUrl = "Enter the server's base URL, for example http://gpu-01:8080/v1.";
    if (!c.embedder.model.trim()) e.embedModel = "Name the model the server should run.";
  }
  if (step === 3) {
    if (c.store.mode === "qdrant" && !isUrl(c.store.url)) e.storeUrl = "Enter the Qdrant server address, for example http://qdrant:6333.";
    if (c.store.mode === "chroma") {
      if (!c.store.host.trim()) e.storeHost = "Enter the Chroma host.";
      if (!/^\d{1,5}$/.test(c.store.port.trim())) e.storePort = "Enter a port number.";
    }
  }
  if (step === 4) {
    const name = c.corpus.trim();
    if (!name) e.corpus = "Give the corpus a name.";
    else if (takenCorpora.has(name)) e.corpus = `A corpus named ${name} already exists.`;
    if (!c.pipeline.trim()) e.pipeline = "Give the pipeline a name.";
  }
  return e;
}

// ---------- Names ----------

/** A provider name that fits the naming rule and is not taken yet. */
export function freeName(base: string, taken: Set<string>): string {
  let slug = base
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 56);
  if (!/^[a-z]/.test(slug)) slug = `p-${slug}`.replace(/-+$/, "");
  if (!PROVIDER_NAME_RE.test(slug)) slug = "provider";
  let name = slug;
  for (let i = 2; taken.has(name); i++) name = `${slug}-${i}`;
  return name;
}

// ---------- Requests ----------

export function parserPick(c: ParserChoice, taken: Set<string>): Pick | null {
  if (c.mode === "text") return null;
  if (c.mode === "existing") return { reuse: c.name };
  if (c.mode === "shim") return { create: { name: freeName(c.service.suggested_name ?? c.service.name ?? "ocr", taken), kind: "parser", type: "http", config: { base_url: c.service.base_url } } };
  const host = (() => {
    try {
      return new URL(c.url).hostname.split(".")[0] ?? "parser";
    } catch {
      return "parser";
    }
  })();
  return { create: { name: freeName(`${host}-parser`, taken), kind: "parser", type: "http", config: { base_url: c.url.trim() } } };
}

export function embedderPick(c: EmbedderChoice, taken: Set<string>): Pick {
  if (c.mode === "existing") return { reuse: c.name };
  if (c.mode === "hash") return { create: { name: freeName("hash-embedder", taken), kind: "embedder", type: "builtin_hash", config: { dimensions: 256 } } };
  const model = c.model.trim();
  return {
    create: { name: freeName(model.split("/").at(-1) ?? "embedder", taken), kind: "embedder", type: "openai_compatible", config: { base_url: c.url.trim(), model } },
  };
}

export function storePick(c: StoreChoice, taken: Set<string>): Pick {
  if (c.mode === "existing") return { reuse: c.name };
  if (c.mode === "sqlite") return { create: { name: freeName("local-sqlite", taken), kind: "vector_store", type: "sqlite_vec", config: {} } };
  if (c.mode === "qdrant") return { create: { name: freeName("qdrant", taken), kind: "vector_store", type: "qdrant", config: { url: c.url.trim() } } };
  return { create: { name: freeName("chroma", taken), kind: "vector_store", type: "chroma", config: { host: c.host.trim(), port: Number(c.port) } } };
}

/** The pipeline the choices describe, once every provider has a name. */
export function pipelineConfig(c: Choices, names: { text: string; parser: string | null; embedder: string; store: string }): PipelineConfig {
  const routes = [{ mime: "text/*", provider: names.text }, ...(names.parser ? [{ mime: "*/*", provider: names.parser }] : [])];
  const nodes: PipelineConfig["nodes"] = [
    { id: "parse", type: "parse", routes },
    { id: "chunk", type: "chunk", inputs: ["parse"], strategy: "recursive", options: { max_chars: 1000, overlap_chars: 100 } },
    { id: "embed", type: "embed", inputs: ["chunk"], provider: names.embedder },
    { id: "vec", type: "index_vector", inputs: ["embed"], provider: names.store, options: { metric: "cosine" } },
  ];
  if (c.retrieval === "dense") return { name: c.pipeline.trim(), nodes, retrieval: { sources: [{ id: "dense", index: "vec", top_k: 30 }], top_k: 8 } };
  nodes.push({ id: "fts", type: "index_fts", inputs: ["chunk"], options: { tokenizer: "unicode61" } });
  return {
    name: c.pipeline.trim(),
    nodes,
    retrieval: {
      sources: [
        { id: "dense", index: "vec", top_k: 30 },
        { id: "sparse", index: "fts", top_k: 30 },
      ],
      fusion: { method: "rrf", k: 60 },
      top_k: 8,
    },
  };
}

/** File types the chosen parsers can read, for the upload picker. */
export function acceptFor(c: ParserChoice): string | undefined {
  return c.mode === "text" ? ".txt,.md,.markdown,.html,.htm,.csv,.json,text/*" : undefined;
}
