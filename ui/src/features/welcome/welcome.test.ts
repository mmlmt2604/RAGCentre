import { describe, expect, it } from "vitest";
import { embedderPick, freeName, pipelineConfig, storePick, validateStep, defaultChoices, type Choices } from "./model";

const base: Choices = { parser: { mode: "text" }, embedder: { mode: "hash" }, store: { mode: "sqlite" }, corpus: "docs", pipeline: "first", retrieval: "hybrid" };

describe("welcome setup model", () => {
  it("makes provider names that fit the rule and avoid taken ones", () => {
    expect(freeName("BAAI/bge-small-en-v1.5", new Set())).toBe("baai-bge-small-en-v1-5");
    expect(freeName("hash-embedder", new Set(["hash-embedder", "hash-embedder-2"]))).toBe("hash-embedder-3");
    expect(freeName("9lives", new Set())).toMatch(/^[a-z]/);
  });

  it("reuses registered providers and creates the rest", () => {
    expect(embedderPick({ mode: "existing", name: "bge" }, new Set())).toEqual({ reuse: "bge" });
    expect(storePick({ mode: "qdrant", url: " http://q:6333 " }, new Set())).toEqual({ create: { name: "qdrant", kind: "vector_store", type: "qdrant", config: { url: "http://q:6333" } } });
  });

  it("builds hybrid and dense pipelines", () => {
    const names = { text: "builtin-text", parser: "docling", embedder: "hash-embedder", store: "local-sqlite" };
    const hybrid = pipelineConfig(base, names);
    expect(hybrid.nodes.map((n) => n.id)).toEqual(["parse", "chunk", "embed", "vec", "fts"]);
    expect(hybrid.retrieval.fusion).toEqual({ method: "rrf", k: 60 });
    const dense = pipelineConfig({ ...base, retrieval: "dense" }, { ...names, parser: null });
    expect(dense.nodes.map((n) => n.id)).toEqual(["parse", "chunk", "embed", "vec"]);
    expect(dense.nodes[0]).toMatchObject({ routes: [{ mime: "text/*", provider: "builtin-text" }] });
  });

  it("validates each step before moving on", () => {
    expect(validateStep(2, { ...base, embedder: { mode: "openai", url: "nope", model: "" } }, new Set())).toHaveProperty("embedUrl");
    expect(validateStep(4, { ...base, corpus: "docs" }, new Set(["docs"]))).toHaveProperty("corpus");
    expect(validateStep(4, base, new Set())).toEqual({});
  });

  it("defaults to no-setup built-ins on an empty server", () => {
    const c = defaultChoices([], []);
    expect(c.parser.mode).toBe("text");
    expect(c.embedder.mode).toBe("hash");
    expect(c.store.mode).toBe("sqlite");
  });
});
