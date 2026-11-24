import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { completeRuns, createPipeline } from "../../mocks/engine";
import { hybrid } from "../../mocks/fixtures";
import { uuidFrom } from "../../mocks/hash";
import { query as queryMock } from "../../mocks/query";
import { renderApp, server } from "../../test/render";

const support = uuidFrom("corpus:support-docs");
const pid = (name: string) => uuidFrom(`pipeline:${support}:${name}`);
const base = `/corpora/${support}/query`;
const q = encodeURIComponent("refunds original payment method");

describe("query playground (UI-4)", () => {
  it("runs a query against the active pipeline and shows sources and timings", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(base);
    await user.type(await screen.findByRole("textbox", { name: "Query" }), "refunds original payment method");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(router.state.location.search).toContain("q=refunds"));
    const col = await screen.findByRole("region", { name: "Results from baseline-hybrid" });
    const first = (await within(col).findAllByRole("listitem"))[0]!;
    expect(within(first).getByText("#1")).toBeInTheDocument();
    // Per-source ranks and scores (dense and sparse sources of the hybrid pipeline).
    expect(within(first).getByLabelText("Rank and score in each source")).toHaveTextContent(/(dense|sparse) #\d+ \d\.\d{3}/);
    expect(within(col).getByLabelText("Timings")).toHaveTextContent(/total/);
    expect(within(col).getByLabelText("Timings")).toHaveTextContent(/embed/);
  });

  it("shows what the enrich node wrote for each result, and which sources read it", async () => {
    const { state } = renderApp(`${base}?q=${q}&a=${pid("baseline-hybrid")}&b=${pid("enriched")}`, "ready", (s) => {
      const p = createPipeline(s, support, hybrid("enriched", { enrich: "summary" }));
      completeRuns(s, p.nodes.map((n) => n.node_fp));
    });
    expect(state.pipelines.some((p) => p.name === "enriched")).toBe(true);
    const plain = await screen.findByRole("region", { name: "Results from baseline-hybrid" });
    const enriched = await screen.findByRole("region", { name: "Results from enriched" });
    await within(enriched).findAllByRole("listitem");
    await within(plain).findAllByRole("listitem");
    expect(within(enriched).getByLabelText("Enriched sources")).toHaveTextContent("dense, sparse");
    expect((await within(enriched).findAllByText("enrichment")).length).toBeGreaterThan(0);
    expect(within(plain).queryByText("enrichment")).toBeNull();
    expect(within(plain).queryByLabelText("Enriched sources")).toBeNull();
  });

  it("puts two pipelines side by side and links the chunks they share", async () => {
    renderApp(`${base}?q=${q}&a=${pid("baseline-hybrid")}&b=${pid("rerank-jina")}`);
    const a = await screen.findByRole("region", { name: "Results from baseline-hybrid" });
    const b = await screen.findByRole("region", { name: "Results from rerank-jina" });
    await within(a).findAllByRole("listitem");
    await within(b).findAllByRole("listitem");
    // The two share their chunk step, so their results are the same chunks in a different order.
    await waitFor(() => expect(within(a).getAllByText(/^b #\d+/).length).toBeGreaterThan(0));
    expect(within(b).getAllByText(/^a #\d+/).length).toBeGreaterThan(0);
  });

  it("offers only schema fields, and range operators only for numbers", async () => {
    const user = userEvent.setup();
    renderApp(base);
    await user.click(await screen.findByRole("button", { name: /^Filter/ }));
    await user.click(screen.getByRole("button", { name: "Add condition" }));
    const field = screen.getByLabelText("Condition 1 field");
    const names = within(field).getAllByRole("option").map((o) => o.textContent);
    expect(names).toEqual(["Field…", "lang (text)", "product (text)", "year (integer)", "document_id (text)"]);
    await user.selectOptions(field, "lang");
    const textOps = within(screen.getByLabelText("Condition 1 operator")).getAllByRole("option").map((o) => o.textContent);
    expect(textOps).toEqual(["is", "is not", "is one of", "is none of"]);
    await user.selectOptions(field, "year");
    const numOps = within(screen.getByLabelText("Condition 1 operator")).getAllByRole("option").map((o) => o.textContent);
    expect(numOps).toContain("≥");
    await user.selectOptions(screen.getByLabelText("Condition 1 operator"), "$gte");
    await user.type(screen.getByLabelText("Condition 1 value"), "soon");
    expect(screen.getByText("Enter a whole number.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  });

  it("applies a filter: every result satisfies it", async () => {
    const f = encodeURIComponent(JSON.stringify({ lang: "de" }));
    const { state } = renderApp(`${base}?q=${q}&f=${f}`);
    const col = await screen.findByRole("region", { name: "Results from baseline-hybrid" });
    const items = await within(col).findAllByRole("listitem");
    for (const li of items) {
      const name = within(li).getAllByRole("link")[0]!.textContent;
      expect(state.documents.find((d) => d.filename === name)?.metadata.lang).toBe("de");
    }
  });

  it("explains a building pipeline and queries it anyway, flagging partial results", async () => {
    const user = userEvent.setup();
    renderApp(`${base}?q=${q}&a=${pid("structure-1200")}`, "building");
    expect(await screen.findByRole("heading", { name: "structure-1200 is still building" })).toBeInTheDocument();
    expect(screen.getByText(/of documents are searchable through it/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Query anyway" }));
    expect(await screen.findByText(/Partial: the index is still building/)).toBeInTheDocument();
  });

  it("explains an archived pipeline", async () => {
    renderApp(`${base}?q=${q}&a=${pid("bge-m3-swap")}`, "ready", (s) => {
      s.pipelines.find((p) => p.id === pid("bge-m3-swap"))!.archived = true;
    });
    expect(await screen.findByRole("heading", { name: "This pipeline is archived" })).toBeInTheDocument();
    expect(screen.getByText("Archived pipelines cannot be used. Pick another pipeline.")).toBeInTheDocument();
  });

  it.each([
    ["index_not_built", "This pipeline's indexes have not been built yet. Check its status page."],
    ["index_mismatch", "The physical index no longer matches this pipeline. Rebuild it by cloning the pipeline."],
    ["embedding_dimension_mismatch", "The embedder now returns a different vector size than the index. Create a new provider and pipeline."],
    ["provider_error", "A provider service failed. Check the provider and retry."],
  ])("gives a specific next step for %s", async (code, next) => {
    server.use(http.post("/v1/corpora/:id/query", () => HttpResponse.json({ error: { code, message: `simulated ${code}` } }, { status: code === "provider_error" ? 502 : 409 })));
    renderApp(`${base}?q=${q}`);
    expect(await screen.findByText(`simulated ${code}`)).toBeInTheDocument();
    expect(screen.getByText(next)).toBeInTheDocument();
  });

  it("retries the same query after a provider failure when Search is pressed again", async () => {
    const user = userEvent.setup();
    server.use(http.post("/v1/corpora/:id/query", () => HttpResponse.json({ error: { code: "provider_error", message: "Embedding service unavailable" } }, { status: 502 })));
    renderApp(base);
    await user.type(await screen.findByRole("textbox", { name: "Query" }), "refunds");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Embedding service unavailable")).toBeInTheDocument();
    server.resetHandlers();
    await user.click(screen.getByRole("button", { name: "Search" }));
    const col = await screen.findByRole("region", { name: "Results from baseline-hybrid" });
    expect((await within(col).findAllByRole("listitem")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Embedding service unavailable")).not.toBeInTheDocument();
  });

  it("remembers recent queries per corpus", async () => {
    const user = userEvent.setup();
    localStorage.clear();
    renderApp(base);
    await user.type(await screen.findByRole("textbox", { name: "Query" }), "token rotation");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("button", { name: "token rotation" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(`ragcentre.recent-queries.${support}`)!)).toEqual(["token rotation"]);
  });

  it("shows the queries a transform generated, and the skip toggle sends transform: false", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`${base}?q=${encodeURIComponent("password reimbursement")}&a=${pid("multi-query")}`);
    const col = await screen.findByRole("region", { name: "Results from multi-query" });
    const probes = await within(col).findByLabelText("Query transform");
    const rows = within(probes).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual([
      "originalpassword reimbursement",
      "paraphrasetoken refund",
      "paraphrasereimbursement password",
      "paraphraseinformation about password reimbursement",
    ]);
    expect(within(col).getByLabelText("Timings")).toHaveTextContent(/transform/);
    expect(within(probes).queryByText("cached")).not.toBeInTheDocument();

    // The same query again is served from the mock model's cache.
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(within(col).getByText("cached")).toBeInTheDocument());

    const seen: unknown[] = [];
    server.use(
      http.post("/v1/corpora/:id/query", async ({ request }) => {
        seen.push(await request.clone().json());
        return HttpResponse.json(queryMock(state, support, (await request.json()) as never));
      }),
    );
    await user.click(screen.getByRole("checkbox", { name: "Skip query transform" }));
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(within(screen.getByRole("region", { name: "Results from multi-query" })).queryByLabelText("Query transform")).not.toBeInTheDocument());
    expect(seen.at(-1)).toMatchObject({ transform: false });
  });

  it("offers the transform toggles only when a chosen pipeline has a transform", async () => {
    renderApp(`${base}?q=${q}&a=${pid("baseline-hybrid")}`);
    await screen.findByRole("region", { name: "Results from baseline-hybrid" });
    expect(screen.queryByRole("checkbox", { name: "Skip query transform" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Query transform")).not.toBeInTheDocument();
  });

  it("marks which generated query found each result when asked", async () => {
    renderApp(`${base}?q=${encodeURIComponent("password reimbursement")}&a=${pid("multi-query")}&dbg=1`);
    const col = await screen.findByRole("region", { name: "Results from multi-query" });
    const first = (await within(col).findAllByRole("listitem")).find((li) => li.className.includes("qres"))!;
    expect(within(first).getByLabelText("Queries that found this chunk")).toHaveTextContent(/^found by #\d/);
  });

  it("reports a failing language model as a provider error", async () => {
    renderApp(`${base}?q=${q}&a=${pid("multi-query")}`, "ready", (s) => {
      s.serviceFaults["local-chat"] = { down: "POST http://gpu-01:8082/v1/chat/completions returned 503" };
    });
    expect(await screen.findByText(/local-chat failed: POST http:\/\/gpu-01:8082/)).toBeInTheDocument();
  });
});
