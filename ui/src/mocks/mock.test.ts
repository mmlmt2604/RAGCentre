import { describe, expect, it } from "vitest";
import { tick } from "./engine";
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
