import { describe, expect, it } from "vitest";
import { tick } from "./engine";
import { hybrid } from "./fixtures";
import table from "../../../tests/fixtures/option_problems.json";
import { optionProblems } from "./optionSchema";
import { createProvider, patchProvider } from "./providers";
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

describe("mock parser option schemas", () => {
  it("seeds the HTTP parser with a schema and stores one on providers created later, immutably", () => {
    const state = buildScenario("ready");
    expect(state.providers.find((p) => p.name === "docling")!.config.options_schema).toMatchObject({ additionalProperties: false });
    const made = createProvider(state, { name: "docling-two", kind: "parser", type: "http", config: { base_url: "http://parse-02:5001" } });
    expect(made.config.options_schema).toMatchObject({ type: "object" });
    const plain = createProvider(state, { name: "plain-parser", kind: "parser", type: "http", config: { base_url: "http://no-schema:5001" } });
    expect(plain.config.options_schema).toBeNull();
    expect(() => patchProvider(state, made.id, { options_schema: null })).toThrow(/cannot be changed/);
    expect(state.providers.find((p) => p.name === "builtin-text")!.config).not.toHaveProperty("options_schema");
  });

  it("reports problems with the backend's paths and wording", () => {
    const schema = buildScenario("ready").providers.find((p) => p.name === "docling")!.config.options_schema;
    expect(optionProblems(schema, { ocr: true, table_mode: "fast", page_range: [1, 4], engine: { x: 1 } })).toEqual([]);
    expect(optionProblems(schema, { nope: 1, ocr: "yes", image_dpi: 900, image_dpi_x: 1, table_mode: "slow", page_range: [1] })).toEqual([
      { suffix: ".nope", message: "This parser has no such option." },
      { suffix: ".image_dpi_x", message: "This parser has no such option." },
      { suffix: ".image_dpi", message: "Must be at most 400." },
      { suffix: ".ocr", message: "Must be true or false." },
      { suffix: ".page_range", message: "Must have at least 2 items or be null." },
      { suffix: ".table_mode", message: 'Must be one of: "fast", "accurate".' },
    ]);
    expect(optionProblems(schema, { image_dpi: 100.5 })).toEqual([{ suffix: ".image_dpi", message: "Must be a whole number." }]);
    expect(optionProblems(null, { anything: 1 })).toEqual([]);
  });

  // tests/fixtures/option_problems.json is the same table tests/test_parser_options_schema.py holds the backend to.
  // The backend words these; the mock has to say the same thing, in the same order, for the same options.
  describe("against the backend's table of cases", () => {
    const schemas = table.schemas as Record<string, unknown>;
    it.each(table.cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
      expect(optionProblems(schemas[c.schema], c.options)).toEqual(c.problems);
    });
    it("covers every keyword the mock words", () => {
      const used = new Set(JSON.stringify(Object.values(schemas)).match(/"(type|enum|const|minimum|maximum|exclusiveMinimum|exclusiveMaximum|minLength|maxLength|minItems|maxItems|items|properties|required|additionalProperties|allOf|anyOf|\$ref)":/g));
      expect([...used].map((k) => k.slice(1, -2)).sort()).toEqual(
        ["$ref", "additionalProperties", "allOf", "anyOf", "const", "enum", "exclusiveMaximum", "exclusiveMinimum", "items", "maxItems", "maxLength", "maximum", "minItems", "minLength", "minimum", "properties", "required", "type"],
      );
    });
    it.each(table.unmirrored.map((c) => [c.keyword, c] as const))("accepts what the backend words in jsonschema's own text: %s", (_keyword, c) => {
      expect(optionProblems(c.schema, c.options)).toEqual([]);
    });
  });
});
