// Mock document operations: upload, edit, replace, delete, and the parsed IR and chunks a pipeline
// produced. Mirrors the backend's documents router (IMPLEMENTATION_PLAN.md §7, §9, §16).
import type { Chunk, MetadataSchema, RunState, Scalar } from "../api/types";
import { fp, uuidFrom } from "./hash";
import type { Form } from "./multipart";
import { MockError } from "./providers";
import { indexRuns, now, planRuns, runKey, type DocRec, type MockState, type PipelineRec } from "./store";

export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

export function findDocument(state: MockState, id: string): DocRec {
  const d = state.documents.find((x) => x.id === id);
  if (!d) throw new MockError(404, "not_found", `Document ${id} was not found.`);
  return d;
}

// ---------- metadata ----------

function typeOk(v: unknown, type: string): boolean {
  if (type === "str") return typeof v === "string";
  if (type === "bool") return typeof v === "boolean";
  if (type === "int") return typeof v === "number" && Number.isInteger(v);
  return typeof v === "number" && Number.isFinite(v);
}

export function validateMetadata(meta: unknown, schema: MetadataSchema): Record<string, Scalar> {
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new MockError(422, "validation_error", "metadata and attributes must be JSON objects.");
  for (const [k, v] of Object.entries(meta)) {
    const spec = schema.fields[k];
    if (!spec) throw new MockError(422, "validation_error", `Unknown metadata field '${k}'.`);
    if (!typeOk(v, spec.type)) throw new MockError(422, "validation_error", `Metadata ${k} must have type ${spec.type}.`);
  }
  return meta as Record<string, Scalar>;
}

// ---------- listing (same rollup as the backend's list filter) ----------

export function docState(state: MockState, d: DocRec, live: Set<string>, index = indexRuns(state)): RunState {
  let f = 0;
  let ru = 0;
  let s = 0;
  for (const nodeFp of live) {
    const run = index.get(runKey(d.id, nodeFp));
    if (!run) continue;
    if (run.status === "failed") f++;
    else if (run.status === "running") ru++;
    else if (run.status === "succeeded") s++;
  }
  if (f) return "failed";
  if (ru) return "running";
  return s === live.size ? "succeeded" : "pending";
}

export function listDocuments(state: MockState, corpusId: string, q: URLSearchParams): DocRec[] {
  let pipelines = state.pipelines.filter((p) => p.corpus_id === corpusId && !p.archived);
  const pid = q.get("pipeline_id");
  if (pid) {
    pipelines = pipelines.filter((p) => p.id === pid);
    if (!pipelines.length) throw new MockError(404, "not_found", `Pipeline ${pid} was not found in this corpus.`);
  }
  const live = new Set(pipelines.flatMap((p) => p.nodes.map((n) => n.node_fp)));
  const text = (q.get("q") ?? "").toLowerCase();
  const wanted = q.get("state");
  if (wanted && !["pending", "running", "succeeded", "failed"].includes(wanted))
    throw new MockError(422, "validation_error", "state must be one of pending, running, succeeded, failed.");
  const index = indexRuns(state);
  return state.documents
    .filter((d) => d.corpus_id === corpusId && d.status === "active")
    .filter((d) => !text || d.filename.toLowerCase().includes(text) || (d.external_id ?? "").toLowerCase().includes(text))
    .filter((d) => !wanted || docState(state, d, live, index) === wanted)
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
}

// ---------- upload, replace, edit, delete ----------

function paragraphsOf(text: string, filename: string, mime: string): string[] {
  if (!mime.startsWith("text/")) {
    return [`Text extracted from ${filename}.`, `${filename} is a ${mime} document; this mock does not read its contents.`];
  }
  const paras = text
    .split(/\n\s*\n/)
    .map((p) => p.replace(/^#+\s*/gm, "").replace(/\s+/g, " ").trim())
    .filter(Boolean);
  return paras.length ? paras : [];
}

function glob(pattern: string, value: string): boolean {
  const re = new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
  return re.test(value);
}

/** Plan runs for a document; parse runs with no matching route will fail like the backend's. */
function planFor(state: MockState, d: DocRec): void {
  const pipelines = state.pipelines.filter((p) => p.corpus_id === d.corpus_id && !p.archived);
  const nodes = [...new Map(pipelines.flatMap((p) => p.nodes).map((n) => [n.node_fp, n])).values()];
  planRuns(state, d.corpus_id, nodes, [d]);
  const index = indexRuns(state);
  for (const p of pipelines) {
    const parse = p.config.nodes.find((n) => n.type === "parse");
    const rec = p.nodes.find((n) => n.type === "parse");
    if (!parse || parse.type !== "parse" || !rec) continue;
    if (parse.routes.some((r) => glob(r.mime, d.mime_type))) continue;
    const run = index.get(runKey(d.id, rec.node_fp));
    if (run) run.will_fail = { code: "unsupported_media_type", message: `No parse route matches ${d.mime_type}.` };
  }
}

export function upload(state: MockState, corpusId: string, form: Form): { doc: DocRec; created: boolean } {
  const corpus = state.corpora.find((c) => c.id === corpusId);
  if (!corpus) throw new MockError(404, "not_found", `Corpus ${corpusId} was not found.`);
  const file = form.file;
  if (!file) throw new MockError(422, "validation_error", "Invalid request: file: Field required");
  if (file.size > state.maxUploadBytes) throw new MockError(413, "payload_too_large", "File is larger than the upload limit.");
  let meta: unknown;
  let attrs: unknown;
  try {
    meta = JSON.parse(form.fields.metadata || "{}");
    attrs = JSON.parse(form.fields.attributes || "{}");
  } catch {
    throw new MockError(422, "validation_error", "metadata and attributes must be valid JSON objects.");
  }
  const metadata = validateMetadata(meta, corpus.metadata_schema);
  if (!attrs || typeof attrs !== "object" || Array.isArray(attrs)) throw new MockError(422, "validation_error", "metadata and attributes must be JSON objects.");
  const filename = file.name || "document";
  const mime = file.type || "application/octet-stream";
  const text = file.text;
  const sha = fp(`content:${text}:${file.size}`);
  const externalId = (form.fields.external_id ?? "").trim() || null;

  const existing = externalId ? state.documents.find((d) => d.corpus_id === corpusId && d.external_id === externalId && d.status === "active") : undefined;
  if (existing) {
    if (existing.content_sha256 !== sha) {
      replaceWith(state, existing, { filename, mime, text, size: file.size, sha });
      existing.metadata = metadata;
      existing.attributes = attrs as Record<string, unknown>;
    }
    return { doc: existing, created: false };
  }
  const id = uuidFrom(`doc:${corpusId}:${filename}:${Date.now()}:${Math.random()}`);
  const d: DocRec = {
    id, corpus_id: corpusId, external_id: externalId, filename, mime_type: mime, size_bytes: file.size, content_sha256: sha,
    metadata, attributes: attrs as Record<string, unknown>, revision: 1, status: "active", created_at: now(), updated_at: now(),
    chunk_texts: paragraphsOf(text, filename, mime), content: text,
  };
  state.documents.push(d);
  planFor(state, d);
  return { doc: d, created: true };
}

function replaceWith(state: MockState, d: DocRec, f: { filename: string; mime: string; text: string; size: number; sha: string }): void {
  d.filename = f.filename;
  d.mime_type = f.mime;
  d.size_bytes = f.size;
  d.content_sha256 = f.sha;
  d.content = f.text;
  d.chunk_texts = paragraphsOf(f.text, f.filename, f.mime);
  d.revision += 1;
  d.updated_at = now();
  state.runs = state.runs.filter((r) => r.document_id !== d.id);
  planFor(state, d);
}

export function replaceContent(state: MockState, id: string, form: Form): DocRec {
  const d = findDocument(state, id);
  if (d.status !== "active") throw new MockError(422, "validation_error", "Document is being deleted.");
  const file = form.file;
  if (!file) throw new MockError(422, "validation_error", "Invalid request: file: Field required");
  if (file.size > state.maxUploadBytes) throw new MockError(413, "payload_too_large", "File exceeds the upload limit.");
  const sha = fp(`content:${file.text}:${file.size}`);
  if (sha !== d.content_sha256)
    replaceWith(state, d, { filename: file.name || d.filename, mime: file.type || d.mime_type, text: file.text, size: file.size, sha });
  return d;
}

export function patchDocument(state: MockState, id: string, body: { metadata?: unknown; attributes?: unknown }): DocRec {
  const d = findDocument(state, id);
  const corpus = state.corpora.find((c) => c.id === d.corpus_id)!;
  if (body.metadata !== undefined && body.metadata !== null) d.metadata = validateMetadata(body.metadata, corpus.metadata_schema);
  if (body.attributes !== undefined && body.attributes !== null) {
    if (typeof body.attributes !== "object" || Array.isArray(body.attributes)) throw new MockError(422, "validation_error", "attributes must be a JSON object.");
    d.attributes = body.attributes as Record<string, unknown>;
  }
  d.updated_at = now();
  return d;
}

export function deleteDocument(state: MockState, id: string): DocRec {
  const d = findDocument(state, id);
  d.status = "deleting";
  d.updated_at = now();
  return d;
}

/** The removal task: called by the ingestion simulator. */
export function reapDeleted(state: MockState): boolean {
  const gone = new Set(state.documents.filter((d) => d.status === "deleting").map((d) => d.id));
  if (!gone.size) return false;
  state.documents = state.documents.filter((d) => !gone.has(d.id));
  state.runs = state.runs.filter((r) => !gone.has(r.document_id));
  return true;
}

// ---------- parsed IR and chunks ----------

interface Block {
  id: string;
  type: string;
  level?: number;
  text: string;
  page?: number;
  heading_path: string[];
  metadata: Record<string, unknown>;
}

function title(d: DocRec): string {
  const base = d.filename.split("/").pop()!.replace(/\.[^.]+$/, "");
  return base.replace(/[-_]+/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

const FILLER = new Set(["a", "an", "the", "is", "are", "be", "must", "to", "of", "for", "with", "on", "in", "and", "than", "can", "after", "every"]);

function sectionTitle(text: string): string {
  const words = text.split(" ").slice(0, 5).map((w) => w.replace(/[.,;:]$/, ""));
  while (words.length > 1 && FILLER.has(words[words.length - 1]!.toLowerCase())) words.pop();
  return words.join(" ") || "Section";
}

const paged = (d: DocRec) => d.mime_type === "application/pdf";

export function blocksOf(d: DocRec): Block[] {
  const t = title(d);
  const blocks: Block[] = [{ id: "b1", type: "heading", level: 1, text: t, heading_path: [t], metadata: {}, ...(paged(d) ? { page: 1 } : {}) }];
  d.chunk_texts.forEach((text, i) => {
    const page = paged(d) ? Math.floor(i / 2) + 1 : undefined;
    const h = sectionTitle(text);
    blocks.push({ id: `b${blocks.length + 1}`, type: "heading", level: 2, text: h, heading_path: [t, h], metadata: {}, ...(page ? { page } : {}) });
    blocks.push({ id: `b${blocks.length + 1}`, type: "paragraph", text, heading_path: [t, h], metadata: {}, ...(page ? { page } : {}) });
  });
  return blocks;
}

function parseRunOk(state: MockState, d: DocRec, p: PipelineRec): boolean {
  const node = p.nodes.find((n) => n.type === "parse");
  return Boolean(node && indexRuns(state).get(runKey(d.id, node.node_fp))?.status === "succeeded");
}

export function parsed(state: MockState, d: DocRec, pipelineId: string): Record<string, unknown> {
  const p = state.pipelines.find((x) => x.id === pipelineId);
  if (!p) throw new MockError(404, "not_found", `Pipeline ${pipelineId} was not found.`);
  if (!parseRunOk(state, d, p)) throw new MockError(404, "not_found", "The parse run has not succeeded for this document.");
  const blocks = blocksOf(d);
  const pages = paged(d) ? [...new Set(blocks.map((b) => b.page!))].map((n) => ({ page: n, width: 612, height: 792 })) : [];
  return { schema_version: "1.0", metadata: { title: title(d), ...(pages.length ? { page_count: pages.length } : {}) }, pages, blocks };
}

interface Piece {
  text: string;
  block_ids: string[];
}

/** Sliding character windows over the joined text, as the fixed and recursive strategies do. */
function windows(blocks: Block[], maxChars: number, overlap: number, sentenceBreaks: boolean): Piece[] {
  const spans: { start: number; end: number; id: string }[] = [];
  let joined = "";
  for (const b of blocks) {
    if (joined) joined += "\n\n";
    spans.push({ start: joined.length, end: joined.length + b.text.length, id: b.id });
    joined += b.text;
  }
  const out: Piece[] = [];
  let start = 0;
  while (start < joined.length) {
    let end = Math.min(joined.length, start + maxChars);
    if (sentenceBreaks && end < joined.length) {
      const cut = Math.max(joined.lastIndexOf(". ", end), joined.lastIndexOf("\n\n", end));
      if (cut > start + maxChars / 3) end = cut + 1;
    }
    const text = joined.slice(start, end).trim();
    if (text) out.push({ text, block_ids: spans.filter((s) => s.start < end && s.end > start).map((s) => s.id) });
    if (end >= joined.length) break;
    start = Math.max(end - overlap, start + 1);
  }
  return out;
}

export function chunks(state: MockState, d: DocRec, pipelineId: string, nodeId: string | null, index = indexRuns(state)): Chunk[] {
  const p = state.pipelines.find((x) => x.id === pipelineId);
  if (!p) throw new MockError(404, "not_found", `Pipeline ${pipelineId} was not found.`);
  const chunkNodes = p.nodes.filter((n) => n.type === "chunk");
  const node = nodeId ? chunkNodes.find((n) => n.node_id === nodeId) : chunkNodes.length === 1 ? chunkNodes[0] : undefined;
  if (!node) throw new MockError(422, "validation_error", "Choose a chunk node, or a pipeline with exactly one chunk node.");
  if (index.get(runKey(d.id, node.node_fp))?.status !== "succeeded") return [];
  // Chunks depend only on the content and the chunk node: cache them (queries and evaluations reuse them).
  const key = `${node.node_fp}|${d.id}|${d.revision}|${d.content_sha256}`;
  const hit = CHUNK_CACHE.get(key);
  if (hit) return hit;
  const made = makeChunks(d, node);
  CHUNK_CACHE.set(key, made);
  return made;
}

const CHUNK_CACHE = new Map<string, Chunk[]>();

/** Drop cached chunks for a (corpus, chunk-node) pair, e.g. after garbage collection. */
export function clearChunksFor(corpusId: string, nodeFp: string): void {
  const prefix = `${nodeFp}|`;
  for (const key of [...CHUNK_CACHE.keys()]) {
    // Keys are `${nodeFp}|${docId}|${revision}|${sha}`; the doc's corpus is not in the key, so
    // recompute the document's corpus from the store is not available here. Callers pass the key
    // prefix and we drop everything for the node: correctness first.
    if (key.startsWith(prefix)) CHUNK_CACHE.delete(key);
  }
  void corpusId;
}

function makeChunks(d: DocRec, node: { node_fp: string; resolved: Record<string, unknown> }): Chunk[] {
  const resolved = node.resolved as { strategy: string; options: Record<string, unknown> };
  const opts = resolved.options;
  const maxChars = Number(opts.max_chars ?? 1000);
  const blocks = blocksOf(d);
  const byId = new Map(blocks.map((b) => [b.id, b]));
  let pieces: (Piece & { context?: string })[];
  if (resolved.strategy === "structure") {
    // One chunk per heading section; the heading is attached to its content.
    pieces = [];
    for (let i = 1; i < blocks.length; i += 2) {
      const h = blocks[i]!;
      const body = blocks[i + 1];
      const text = [h.text, body?.text].filter(Boolean).join("\n\n").slice(0, maxChars);
      pieces.push({ text, block_ids: [h.id, ...(body ? [body.id] : [])], ...(opts.include_heading_path !== false ? { context: h.heading_path.join(" > ") } : {}) });
    }
  } else if (resolved.strategy === "page" && paged(d)) {
    const pages = [...new Set(blocks.map((b) => b.page!))];
    pieces = pages.map((n) => {
      const bs = blocks.filter((b) => b.page === n);
      return { text: bs.map((b) => b.text).join("\n\n").slice(0, maxChars), block_ids: bs.map((b) => b.id) };
    });
  } else {
    pieces = windows(blocks, maxChars, Number(opts.overlap_chars ?? 0), resolved.strategy !== "fixed");
  }
  return pieces.map((pc, ordinal) => {
    const bs = pc.block_ids.map((id) => byId.get(id)!).filter(Boolean);
    const pages = bs.map((b) => b.page).filter((x): x is number => typeof x === "number");
    return {
      id: uuidFrom(`chunk:${node.node_fp}:${d.id}:${ordinal}`),
      document_id: d.id,
      ordinal,
      text: pc.text,
      context: pc.context ?? null,
      page_start: pages.length ? Math.min(...pages) : null,
      page_end: pages.length ? Math.max(...pages) : null,
      heading_path: bs[0]?.heading_path ?? [],
      block_ids: pc.block_ids,
      char_count: pc.text.length,
    };
  });
}

export function content(d: DocRec): string {
  return d.content ?? [`# ${title(d)}`, ...d.chunk_texts].join("\n\n");
}

