import { describe, expect, it } from "vitest";
import { ENGINES, shimCommand, type RunWith } from "./engines";
import { choicesFromDraft, setupDraft, embedderPick, freeName, pipelineConfig, storePick, validateStep, defaultChoices, type Choices } from "./model";

const base: Choices = { parser: { mode: "text" }, embedder: { mode: "hash" }, store: { mode: "sqlite" }, corpus: "docs", pipeline: "first", retrieval: "hybrid" };

describe("welcome setup model", () => {
  it("uses the guide only when detection is available and enabled", () => {
    const status = { services: [], scanning: false, discovery_enabled: true, in_container: true };
    const guide = { mode: "guide", engine: "docling", runWith: "docker" };
    const choices = defaultChoices([], [], status);
    expect(choices.parser).toEqual(guide);
    expect(validateStep(1, choices, new Set())).toHaveProperty("parserGuide");
    expect(defaultChoices([], [], { ...status, discovery_enabled: false }).parser).toEqual({ mode: "text" });
    expect(defaultChoices([], []).parser).toEqual({ mode: "text" });
  });

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

  it("round-trips safe custom drafts with nullable API references", () => {
    const choices: Choices = { ...base, embedder: { mode: "openai", url: "https://model.example/v1", model: "small" }, store: { mode: "existing", name: "saved-store" } };
    const draft = setupDraft(choices, "custom", [], 3);
    expect(draft).not.toHaveProperty("password");
    expect(choicesFromDraft({ ...draft, embedder: { reuse: null, create: draft.embedder!.create }, store: { reuse: "saved-store", create: null } })).toEqual(choices);
    expect(setupDraft(choices, "trial", [], 4)).toMatchObject({ parser: null, embedder: null, store: null });
  });

  it("defaults to no-setup built-ins on an empty server", () => {
    const c = defaultChoices([], []);
    expect(c.parser.mode).toBe("text");
    expect(c.embedder.mode).toBe("hash");
    expect(c.store.mode).toBe("sqlite");
  });
});

describe("shim command", () => {
  const cases = ENGINES.flatMap((engine) =>
    (["docker", "uv", "remote"] as RunWith[]).flatMap((mode) =>
      [true, false].map((container) => ({ engine, mode, container })),
    ),
  );

  it.each(cases)("builds $engine.id for $mode with container=$container", ({ engine, mode, container }) => {
    const { id, port } = engine;
    const { lines, watchUrl } = shimCommand(engine, mode, container);
    const command = lines.join("\n");
    if (mode === "docker" && container) {
      expect(command).toBe(`docker compose --profile ${id} up -d --build ${id}`);
      expect(watchUrl).toBe(`http://${id}:${port}`);
    } else if (mode === "docker") {
      expect(command).toContain(`docker build -t ${id}-shim -f examples/${id}_shim/Dockerfile examples`);
      expect(command).toContain(`-p 127.0.0.1:${port}:${port}`);
      expect(watchUrl).toBe(`http://127.0.0.1:${port}`);
    } else {
      expect(command).toContain(`--host ${container || mode === "remote" ? "0.0.0.0" : "127.0.0.1"} --port ${port}`);
      expect(watchUrl).toBe(mode === "remote" ? null : `http://${container ? "host.docker.internal" : "127.0.0.1"}:${port}`);
      if (id === "paddleocr") expect(command).toMatch(/uv sync &&\n {2}uv pip install paddlepaddle &&\n {2}uv run --no-sync/);
      if (id === "easyocr" || id === "doctr") expect(command).toMatch(/uv pip install .*torch torchvision &&\n {2}uv sync/);
    }
  });

  it("matches the example shim ports in KNOWN_SHIMS", () => {
    expect(ENGINES.map(({ id, port }) => [id, port])).toEqual([
      ["docling", 8701], ["mineru", 8702], ["paddleocr", 8703], ["surya", 8704], ["easyocr", 8705], ["doctr", 8706],
    ]);
  });
});
