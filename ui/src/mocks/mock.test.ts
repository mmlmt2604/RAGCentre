import { describe, expect, it } from "vitest";
import { completeRuns, createPipeline, tick } from "./engine";
import { chunks } from "./documents";
import { query } from "./query";
import { hybrid } from "./fixtures";
import { buildScenario } from "./scenarios";
import { indexRuns, pipelineHealth, planRuns, resolvePipeline } from "./store";

describe("mock pipeline resolution", () => {
  it("shares the parse node when only the chunker differs", () => {
    const state = buildScenario("ready");
    const a = resolvePipeline(state, hybrid("a"));
    const b = resolvePipeline(state, hybrid("b", { chunk: "structure", maxChars: 1200 }));
    const fp = (r: typeof a, id: string) => r.nodes.find((n) => n.node_id === id)?.node_fp;
    expect(fp(a, "parse")).toBe(fp(b, "parse"));
    for (const id of ["chunk", "embed", "vec", "fts"]) expect(fp(a, id)).not.toBe(fp(b, id));
  });

  it("gives identical ingestion fingerprints when only retrieval differs, and creates no runs", () => {
    const state = buildScenario("ready");
    const corpus = state.corpora.find((c) => c.name === "support-docs")!;
    const a = resolvePipeline(state, hybrid("a"));
    const b = resolvePipeline(state, hybrid("b", { rerank: true }));
    expect(a.nodes.map((n) => n.node_fp)).toEqual(b.nodes.map((n) => n.node_fp));
    expect(a.fingerprint).not.toBe(b.fingerprint);
    expect(planRuns(state, corpus.id, b.nodes, undefined, true)).toBe(0);
  });

  it("reports node ids that do not match the naming rule and missing providers", () => {
    const state = buildScenario("empty");
    const cfg = hybrid("x");
    cfg.nodes[0]!.id = "Parse!";
    const res = resolvePipeline(state, cfg);
    expect(res.fingerprint).toBeNull();
    expect(res.problems.some((p) => p.message.includes("lowercase"))).toBe(true);
    expect(res.problems.some((p) => p.message.includes('No provider named "bge-small"'))).toBe(true);
  });
});

describe("mock scenarios", () => {
  it("degraded has failed embed runs with blocked runs downstream", () => {
    const state = buildScenario("degraded");
    const swap = state.pipelines.find((p) => p.name === "bge-m3-swap")!;
    const h = pipelineHealth(state, swap);
    expect(h.status).toBe("degraded");
    const embed = swap.nodes.find((n) => n.type === "embed")!;
    const vec = swap.nodes.find((n) => n.type === "index_vector")!;
    expect(h.counts.get(embed.node_fp)!.failed).toBe(14);
    expect(h.counts.get(vec.node_fp)!.blocked).toBe(14);
  });

  it("building progresses to ready as the simulator ticks", () => {
    const state = buildScenario("building");
    const structure = state.pipelines.find((p) => p.name === "structure-1200")!;
    const before = pipelineHealth(state, structure).coverage;
    expect(pipelineHealth(state, structure).status).toBe("building");
    state.throughput = 10_000;
    for (let i = 0; i < 12; i++) tick(state);
    const after = pipelineHealth(state, structure, indexRuns(state));
    expect(after.coverage).toBeGreaterThan(before);
    expect(after.status).toBe("ready");
  });
});

describe("mock chunk enrichment", () => {
  const fpOf = (r: ReturnType<typeof resolvePipeline>, id: string) => r.nodes.find((n) => n.node_id === id)?.node_fp;

  it("shares parse and chunk with a plain pipeline and changes what reads the enriched text", () => {
    const state = buildScenario("ready");
    const plain = resolvePipeline(state, hybrid("plain"));
    const enriched = resolvePipeline(state, hybrid("enriched", { enrich: "situate" }));
    expect(enriched.problems).toEqual([]);
    for (const id of ["parse", "chunk"]) expect(fpOf(plain, id)).toBe(fpOf(enriched, id));
    for (const id of ["embed", "vec", "fts"]) expect(fpOf(plain, id)).not.toBe(fpOf(enriched, id));
    expect(fpOf(enriched, "enrich")).toBeTruthy();
    const summary = resolvePipeline(state, hybrid("summary", { enrich: "summary" }));
    expect(fpOf(summary, "enrich")).not.toBe(fpOf(enriched, "enrich"));
  });

  it("rejects a prompt without {chunk}, a non-llm provider and two enrich nodes feeding the sources", () => {
    const state = buildScenario("ready");
    const bad = hybrid("bad", { enrich: "situate" });
    const enrich = bad.nodes.find((n) => n.type === "enrich")!;
    if (enrich.type === "enrich") {
      enrich.options = { prompt: "Summarise this." };
      enrich.provider = "bge-small";
    }
    const messages = resolvePipeline(state, bad).problems.map((p) => p.message);
    expect(messages).toContain("A custom prompt must contain {chunk}, where the chunk's text goes.");
    expect(messages.some((m) => m.includes("not an llm"))).toBe(true);

    const two = hybrid("two", { enrich: "situate" });
    two.nodes.push({ id: "enrich_2", type: "enrich", inputs: ["chunk"], provider: "qwen-llm", options: { mode: "keywords" } });
    two.nodes = two.nodes.map((n) => (n.id === "fts" && n.type === "index_fts" ? { ...n, inputs: ["enrich_2"] } : n));
    expect(resolvePipeline(state, two).problems.some((p) => p.message.includes("At most one enrich node"))).toBe(true);
  });

  it("lets enrich nodes stack, but not read from a parse node", () => {
    const state = buildScenario("ready");
    const stacked = hybrid("stacked", { enrich: "situate" });
    stacked.nodes.push({ id: "enrich_2", type: "enrich", inputs: ["enrich"], provider: "qwen-llm", options: { mode: "keywords" } });
    stacked.nodes = stacked.nodes.map((n) => (n.type === "embed" || n.type === "index_fts" ? { ...n, inputs: ["enrich_2"] } : n));
    expect(resolvePipeline(state, stacked).problems).toEqual([]);
    const wrong = hybrid("wrong", { enrich: "situate" });
    wrong.nodes = wrong.nodes.map((n) => (n.type === "enrich" ? { ...n, inputs: ["parse"] } : n));
    expect(resolvePipeline(state, wrong).problems.some((p) => p.message.includes("reads from a chunk or enrich node"))).toBe(true);
  });

  it("writes an enrichment per chunk, keeps chunk ids, and shows it in query results", () => {
    const state = buildScenario("ready");
    const corpus = state.corpora.find((c) => c.name === "support-docs")!;
    const p = createPipeline(state, corpus.id, hybrid("enriched-q", { enrich: "keywords" }));
    completeRuns(state, p.nodes.map((n) => n.node_fp));
    const plain = state.pipelines.find((x) => x.name === "baseline-hybrid")!;
    const doc = state.documents.find((d) => d.corpus_id === corpus.id)!;
    const withText = chunks(state, doc, p.id, "chunk");
    const without = chunks(state, doc, plain.id, "chunk");
    expect(withText.map((c) => c.id)).toEqual(without.map((c) => c.id));
    expect(withText.every((c) => typeof c.enrichment === "string" && c.enrichment.length > 0)).toBe(true);
    expect(without.every((c) => c.enrichment === null)).toBe(true);

    const hit = query(state, corpus.id, { query: "how long do refunds take", pipeline_id: p.id });
    expect(hit.enriched_sources.sort()).toEqual(["dense", "sparse"]);
    expect(hit.results.every((r) => r.enrichment)).toBe(true);
    const miss = query(state, corpus.id, { query: "how long do refunds take", pipeline_id: plain.id });
    expect(miss.enriched_sources).toEqual([]);
    expect(miss.results.every((r) => r.enrichment === null)).toBe(true);
  });
});
