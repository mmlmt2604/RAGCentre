import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { uuidFrom } from "../../mocks/hash";
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
});
