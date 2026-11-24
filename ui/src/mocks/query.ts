// Mock query engine (IMPLEMENTATION_PLAN.md §6 mismatch guard, §13 retrieval). Chunks are ranked by
// word overlap so different queries give different results and two pipelines give overlapping but
// different lists. Errors and their order follow src/ragcentre/retrieval/engine.py.
import type { Chunk, QueryRequest, QueryResponse, QueryResult } from "../api/types";
import { filterFields, FilterError, matches, validateFilter } from "../lib/filters";
import { chunks as chunksOf } from "./documents";
import { fp } from "./hash";
import { mockProbes, type MockProbe } from "./transform";
import { MockError } from "./providers";
import { activeDocs, indexRuns, pipelineHealth, runKey, type MockState, type PipelineRec } from "./store";

const STOP = new Set(["the", "and", "for", "are", "with", "how", "what", "when", "does", "can", "you", "your", "this", "that", "from", "into", "after", "about", "there", "which", "will", "have", "has", "not"]);

export function tokens(text: string): string[] {
  return (text.toLowerCase().match(/[a-z0-9]+/g) ?? []).filter((w) => w.length > 2 && !STOP.has(w)).map((w) => w.replace(/(ing|ed|es|s)$/, ""));
}

const TOKEN_CACHE = new WeakMap<Chunk, string[]>();
function chunkTokens(c: Chunk): string[] {
  let t = TOKEN_CACHE.get(c);
  if (!t) TOKEN_CACHE.set(c, (t = tokens(`${c.context ?? ""} ${c.text}`)));
  return t;
}

/** A stable pseudo-random number in [0, 1) for a key: the "semantic" part of a dense score. */
function jitter(key: string): number {
  return parseInt(fp(key).slice(0, 8), 16) / 0x100000000;
}

interface Candidate {
  chunk: Chunk;
  docId: string;
  tokens: string[];
}

function denseScores(q: string[], cands: Candidate[], embedder: string): Map<string, number> {
  const qs = new Set(q);
  const out = new Map<string, number>();
  for (const c of cands) {
    const cs = new Set(c.tokens);
    const overlap = [...qs].filter((t) => cs.has(t)).length;
    const cos = qs.size && cs.size ? overlap / Math.sqrt(qs.size * cs.size) : 0;
    out.set(c.chunk.id, Math.min(0.99, 0.18 + 0.7 * cos + 0.1 * jitter(`${embedder}:${c.chunk.id}`)));
  }
  return out;
}

function keywordScores(q: string[], cands: Candidate[]): Map<string, number> {
  const df = new Map<string, number>();
  for (const c of cands) for (const t of new Set(c.tokens)) df.set(t, (df.get(t) ?? 0) + 1);
  const n = Math.max(1, cands.length);
  const avg = cands.reduce((s, c) => s + c.tokens.length, 0) / n || 1;
  const out = new Map<string, number>();
  for (const c of cands) {
    let s = 0;
    const norm = 1.2 * (0.25 + (0.75 * c.tokens.length) / avg); // BM25 k1 = 1.2, b = 0.75
    for (const t of new Set(q)) {
      const tf = c.tokens.filter((x) => x === t).length;
      if (!tf) continue;
      const idf = Math.log(1 + (n - (df.get(t) ?? 0) + 0.5) / ((df.get(t) ?? 0) + 0.5));
      s += idf * ((tf * 2.2) / (tf + norm));
    }
    if (s > 0) out.set(c.chunk.id, Number(s.toFixed(4)));
  }
  return out;
}

function ranked(scores: Map<string, number>, k: number): [string, number][] {
  return [...scores.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, k);
}

function pipelineFor(state: MockState, corpusId: string, pipelineId: string | undefined): PipelineRec {
  const corpus = state.corpora.find((c) => c.id === corpusId);
  if (!corpus) throw new MockError(404, "not_found", `Corpus ${corpusId} was not found.`);
  const id = pipelineId ?? corpus.active_pipeline_id;
  if (!id) throw new MockError(404, "not_found", `${corpus.name} has no active pipeline. Choose a pipeline to query, or make one active.`);
  const p = state.pipelines.find((x) => x.id === id && x.corpus_id === corpusId);
  if (!p) throw new MockError(404, "not_found", `Pipeline ${id} was not found in this corpus.`);
  return p;
}

const TRANSFORM_CACHE = new WeakMap<MockState, Set<string>>();
/** Queries the mock language model has already answered, so a repeat reports `cached`. */
function transformCache(state: MockState): Set<string> {
  let seen = TRANSFORM_CACHE.get(state);
  if (!seen) TRANSFORM_CACHE.set(state, (seen = new Set()));
  return seen;
}

export function query(state: MockState, corpusId: string, body: QueryRequest): QueryResponse {
  const started = performance.now();
  const p = pipelineFor(state, corpusId, body.pipeline_id);
  const corpus = state.corpora.find((c) => c.id === corpusId)!;
  if (p.archived) throw new MockError(409, "pipeline_archived", `${p.name} is archived and cannot be queried.`);
  const fields = filterFields(corpus.metadata_schema);
  if (body.filter !== undefined && body.filter !== null) {
    try {
      validateFilter(body.filter, fields);
    } catch (e) {
      if (e instanceof FilterError) throw new MockError(422, "invalid_filter", e.message, { path: e.path });
      throw e;
    }
  }
  const index = indexRuns(state);
  const health = pipelineHealth(state, p, index);
  if (!body.allow_partial && health.status !== "ready")
    throw new MockError(409, "index_incomplete", `${p.name} is ${health.status} (${Math.round(health.coverage * 100)}% coverage). Query it anyway by setting allow_partial: true.`, {
      status: health.status,
      coverage: health.coverage,
    });

  const cfg = p.config.retrieval;
  const chunkNode = p.nodes.find((n) => n.type === "chunk");
  const indexNodes = new Map(p.nodes.filter((n) => n.type === "index_vector" || n.type === "index_fts").map((n) => [n.node_id, n]));
  const defaults = Object.fromEntries(Object.entries(corpus.metadata_schema.fields).map(([k, v]) => [k, v.default]));

  // Documents every source's index has: the partial-index rule.
  const docs = activeDocs(state, corpusId).filter((d) =>
    cfg.sources.every((s) => {
      const n = indexNodes.get(s.index);
      return n && index.get(runKey(d.id, n.node_fp))?.status === "succeeded";
    }),
  );
  const allowed = body.filter ? docs.filter((d) => matches(body.filter, { ...defaults, ...d.metadata, document_id: d.id })) : docs;
  const byDoc = new Map<string, Chunk[]>();
  const cands: Candidate[] = [];
  for (const d of allowed) {
    const cs = chunksOf(state, d, p.id, chunkNode?.node_id ?? null, index);
    byDoc.set(d.id, cs);
    for (const c of cs) cands.push({ chunk: c, docId: d.id, tokens: chunkTokens(c) });
  }
  const timings: Record<string, number> = {};

  // 1. The probes to search (the query itself when there is no transform, or the request skipped it).
  const tcfg = body.transform === false ? undefined : cfg.transform;
  let probes: MockProbe[] = [{ index: 0, text: body.query, kind: "original", vector: true, keyword: true }];
  let cached = false;
  if (tcfg) {
    const fault = state.serviceFaults[tcfg.provider];
    if (fault?.down) throw new MockError(502, "provider_error", `${tcfg.provider} failed: ${fault.down}`, { provider: tcfg.provider, cause: "provider_error" });
    const seen = transformCache(state);
    const key = `${p.fingerprint}:${body.query}`;
    cached = seen.has(key);
    seen.add(key);
    probes = mockProbes(tcfg, body.query, cfg.sources.some((s) => indexNodes.get(s.index)?.type === "index_fts"));
    timings.transform = cached ? 1.2 : 320 + 500 * jitter(`llm:${body.query}`);
  }

  // 2. Search every probe on every source it applies to, then merge a source's probes with RRF.
  const found = new Map<string, Set<number>>();
  const perProbe = new Map<number, Record<string, [string, number][]>>();
  for (const probe of probes) {
    const q = tokens(probe.text);
    const lists: Record<string, [string, number][]> = {};
    for (const s of cfg.sources) {
      const n = indexNodes.get(s.index)!;
      if (n.type === "index_vector" && probe.vector) {
        const embedder = String((p.config.nodes.find((x) => x.type === "embed") as { provider?: string } | undefined)?.provider ?? "");
        timings.embed = (timings.embed ?? 0) + 18 + 40 * jitter(`${embedder}:${probe.text}`);
        lists[s.id] = ranked(denseScores(q, cands, embedder), s.top_k);
      } else if (n.type === "index_fts" && probe.keyword) lists[s.id] = ranked(keywordScores(q, cands), s.top_k);
      else continue;
      timings[s.id] = Math.max(timings[s.id] ?? 0, 2 + 6 * jitter(`${s.id}:${probe.text}`));
      for (const [id] of lists[s.id]!) found.set(id, (found.get(id) ?? new Set()).add(probe.index));
    }
    perProbe.set(probe.index, lists);
  }
  const mergeProbes = (sourceId: string, topK: number): [string, number][] => {
    const lists = probes.map((pr) => ({ pr, list: perProbe.get(pr.index)?.[sourceId] })).filter((x): x is { pr: MockProbe; list: [string, number][] } => Boolean(x.list));
    if (lists.length === 1) return lists[0]!.list;
    const fused = new Map<string, number>();
    for (const { pr, list } of lists) {
      const weight = pr.index === 0 ? (tcfg?.original_weight ?? 1) : 1;
      list.forEach(([id], i) => fused.set(id, (fused.get(id) ?? 0) + weight / ((tcfg?.k ?? 60) + i + 1)));
    }
    return ranked(fused, topK);
  };

  // 3. Fuse sources and rerank, per group: one group, or one per sub-question when decomposing.
  const byId = new Map(cands.map((c) => [c.chunk.id, c]));
  if (cfg.rerank) {
    const fault = state.serviceFaults[cfg.rerank.provider];
    if (fault?.down) throw new MockError(502, "provider_error", `${cfg.rerank.provider} failed: ${fault.down}`, { provider: cfg.rerank.provider, cause: "provider_error" });
  }
  const topK = body.top_k ?? cfg.top_k;
  const rankGroup = (sources: Record<string, [string, number][]>, text: string): [string, number][] => {
    let order: [string, number][];
    if (cfg.sources.length > 1) {
      const fused = new Map<string, number>();
      const w = cfg.fusion?.weights ?? {};
      for (const s of cfg.sources) {
        const list = sources[s.id] ?? [];
        const max = Math.max(...list.map((x) => x[1]), 1e-9);
        list.forEach(([id, score], i) => {
          const add = cfg.fusion?.method === "weighted" ? (w[s.id] ?? 1) * (score / max) : (w[s.id] ?? 1) / ((cfg.fusion?.k ?? 60) + i + 1);
          fused.set(id, (fused.get(id) ?? 0) + add);
        });
      }
      order = ranked(fused, Number.MAX_SAFE_INTEGER);
      timings.fusion = 0.4;
    } else order = sources[cfg.sources[0]!.id] ?? [];
    if (cfg.rerank) {
      const qs = new Set(tokens(text));
      order = order
        .slice(0, cfg.rerank.top_n)
        .map(([id]): [string, number] => {
          const c = byId.get(id)!;
          const covered = [...qs].filter((t) => c.tokens.includes(t)).length / Math.max(1, qs.size);
          return [id, Number((0.05 + 0.85 * covered + 0.1 * jitter(`rerank:${id}:${text}`)).toFixed(4))];
        })
        .sort((a, b) => b[1] - a[1]);
      timings.rerank = 60 + 80 * jitter(`rr:${body.query}`);
    }
    return order;
  };

  let sources: Record<string, [string, number][]>;
  let order: [string, number][];
  if (tcfg?.mode === "decompose" && probes.length > 1) {
    const groups = probes.map((pr) => {
      const lists = perProbe.get(pr.index) ?? {};
      return { lists, order: rankGroup(lists, pr.text).slice(0, topK) };
    });
    // Take turns between the groups, so one sub-question cannot fill the whole list.
    order = [];
    sources = Object.fromEntries(cfg.sources.map((s) => [s.id, [] as [string, number][]]));
    const taken = new Set<string>();
    const at = groups.map(() => 0);
    while (order.length < topK && groups.some((g, i) => at[i]! < g.order.length)) {
      groups.forEach((g, i) => {
        while (at[i]! < g.order.length && taken.has(g.order[at[i]!]![0])) at[i]!++;
        const item = g.order[at[i]!];
        if (!item || order.length >= topK) return;
        at[i]!++;
        taken.add(item[0]);
        order.push(item);
        for (const s of cfg.sources) {
          const r = g.lists[s.id]?.findIndex((x) => x[0] === item[0]) ?? -1;
          if (r >= 0) sources[s.id]!.push([item[0], g.lists[s.id]![r]![1]]);
        }
      });
    }
  } else {
    sources = Object.fromEntries(cfg.sources.map((s) => [s.id, mergeProbes(s.id, s.top_k)]));
    order = rankGroup(sources, body.query);
  }

  const neighbors = cfg.expand?.neighbors ?? 0;
  const results: QueryResult[] = order.slice(0, topK).map(([id, score], i) => {
    const c = byId.get(id)!;
    const d = state.documents.find((x) => x.id === c.docId)!;
    const siblings = byDoc.get(c.docId) ?? [];
    const pos = siblings.findIndex((x) => x.id === id);
    const src: QueryResult["sources"] = {};
    for (const [sid, list] of Object.entries(sources)) {
      const r = list.findIndex((x) => x[0] === id);
      if (r >= 0) src[sid] = { rank: r + 1, score: list[r]![1] };
    }
    return {
      chunk_id: id,
      document_id: d.id,
      score: Number(score.toFixed(5)),
      rank: i + 1,
      text: c.chunk.text,
      context: c.chunk.context,
      page_start: c.chunk.page_start,
      page_end: c.chunk.page_end,
      heading_path: c.chunk.heading_path,
      document: { filename: d.filename, external_id: d.external_id, metadata: d.metadata, attributes: d.attributes },
      context_before: neighbors ? siblings.slice(Math.max(0, pos - neighbors), pos).map((x) => x.text) : [],
      context_after: neighbors ? siblings.slice(pos + 1, pos + 1 + neighbors).map((x) => x.text) : [],
      sources: src,
      probes: body.debug && tcfg ? [...(found.get(id) ?? [])].sort((x, y) => x - y) : null,
    };
  });
  timings.total = Object.values(timings).reduce((a, b) => a + b, 0) + (performance.now() - started) * 0.05;
  for (const k of Object.keys(timings)) timings[k] = Number(timings[k]!.toFixed(2));
  const transform = tcfg ? { mode: tcfg.mode, probes: probes.map(({ index, text, kind }) => ({ index, text, kind })), cached } : null;
  return { pipeline_id: p.id, pipeline_fingerprint: p.fingerprint, partial: health.status !== "ready", results, timings_ms: timings, transform };
}
