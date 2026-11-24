import "@testing-library/jest-dom/vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { completeRuns, createPipeline, tick } from "../../mocks/engine";
import { hybrid } from "../../mocks/fixtures";
import { uuidFrom } from "../../mocks/hash";
import { buildScenario } from "../../mocks/scenarios";
import { renderApp } from "../../test/render";

const support = uuidFrom("corpus:support-docs");
const wiki = uuidFrom("corpus:eng-wiki");

function file(name: string, text: string, type = "text/markdown") {
  return new File([text], name, { type });
}

describe("documents (UI-2)", () => {
  it("uploads several dropped files with per-file progress and per-file errors", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${wiki}/documents`, "ready", (s) => {
      s.maxUploadBytes = 200; // the third file is over the limit
    });
    const page = (await screen.findByRole("heading", { name: "Documents" })).closest(".docs")!;
    const files = [file("a.md", "# A\n\nAlpha text."), file("b.md", "# B\n\nBeta text."), file("big.md", "x".repeat(500))];
    fireEvent.dragOver(page, { dataTransfer: { types: ["Files"], files } });
    expect(screen.getByText(/Drop files to upload to eng-wiki/)).toBeInTheDocument();
    fireEvent.drop(page, { dataTransfer: { types: ["Files"], files } });
    const panel = screen.getByRole("region", { name: /Upload 3 files/ });
    await user.click(within(panel).getByRole("button", { name: "Upload 3 files" }));
    const list = within(panel).getByRole("list", { name: "Files to upload" });
    await waitFor(() => expect(within(list).getAllByText(/Added\. Ingestion has started\./)).toHaveLength(2));
    expect(within(list).getByText(/File is larger than the upload limit/)).toBeInTheDocument();
    const added = state.documents.filter((d) => d.corpus_id === wiki && ["a.md", "b.md"].includes(d.filename));
    expect(added.map((d) => d.external_id).sort()).toEqual(["a.md", "b.md"]);
    // Every new document got runs for every pipeline node of the corpus... eng-wiki has none.
    expect(state.runs.filter((r) => added.some((d) => d.id === r.document_id))).toHaveLength(0);
  });

  it("re-uploading an unchanged file under the same external id does no work", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${support}/documents`);
    const input = (await screen.findByTestId("upload-input")) as HTMLInputElement;
    await user.upload(input, file("notes.md", "# Notes\n\nSame text."));
    await user.click(screen.getByRole("button", { name: "Upload 1 file" }));
    await screen.findByText(/Added\./);
    const runs = state.runs.length;
    await user.click(screen.getByRole("button", { name: "Done" }));
    await user.upload(input, file("notes.md", "# Notes\n\nSame text."));
    await user.click(screen.getByRole("button", { name: "Upload 1 file" }));
    expect(await screen.findByText(/Already in the corpus as notes\.md; unchanged, no work needed/)).toBeInTheDocument();
    expect(state.runs.length).toBe(runs);
  });

  it("shows each document's state per pipeline and updates by polling", async () => {
    const { state } = renderApp(`/corpora/${support}/documents?state=pending`, "building");
    const table = await screen.findByRole("table");
    const link = (await within(table).findAllByRole("link"))[0]!;
    const firstRow = link.closest("tr")!;
    const name = link.textContent!;
    expect(within(firstRow).getByText(/structure-1200: pending/)).toBeInTheDocument();
    // Run the ingestion simulator to completion; the table must notice without any user action.
    for (let i = 0; i < 500 && state.runs.some((r) => r.status === "pending" || r.status === "running"); i++) tick(state);
    await waitFor(() => expect(within(table).queryByText(name)).toBeNull(), { timeout: 4000 });
  });

  it("filters by file name and by state, keeping the filters in the URL", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(`/corpora/${support}/documents`, "degraded");
    await user.type(await screen.findByRole("searchbox", { name: /Filter by file name/ }), "billing/");
    await waitFor(() => expect(router.state.location.search).toContain("q=billing"));
    await user.click(screen.getByRole("radio", { name: "Failed" }));
    await waitFor(() => expect(router.state.location.search).toContain("state=failed"));
    expect(router.state.location.search).toContain("q=billing");
    await waitFor(() => {
      const rows = screen.getAllByRole("row").slice(1);
      expect(rows.length).toBeGreaterThan(0);
      for (const r of rows) {
        if ((r.textContent ?? "").includes("No documents match")) continue;
        expect(r.textContent).toContain("billing/");
        expect(within(r).getByText(/bge-m3-swap: failed/)).toBeInTheDocument();
      }
    }, { timeout: 3000 });
  });

  it("shows every node run with its error, and retries a failed one", async () => {
    const user = userEvent.setup();
    const docId = buildScenario("degraded").runs.find((r) => r.status === "failed")!.document_id;
    renderApp(`/corpora/${support}/documents/${docId}`, "degraded");
    const swap = await screen.findByRole("region", { name: "Ingestion in bge-m3-swap" });
    const failedRow = within(swap).getByText("provider_error").closest("tr")!;
    expect(await within(failedRow).findByText(/503 Service Unavailable/)).toBeInTheDocument();
    expect(within(swap).getByText(/Waiting on/)).toBeInTheDocument();
    const active = screen.getByRole("region", { name: "Ingestion in baseline-hybrid" });
    expect(within(active).getAllByText("Succeeded").length).toBeGreaterThan(3);
    await user.click(within(failedRow).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(swap).queryByText("provider_error")).toBeNull());
  });

  it("shows a pipeline's chunks and compares two chunkers side by side", async () => {
    const user = userEvent.setup();
    const docId = buildScenario("ready").documents.find((d) => d.corpus_id === support)!.id;
    renderApp(`/corpora/${support}/documents/${docId}?tab=chunks`, "ready");
    const from = await screen.findByLabelText("Chunks from");
    await user.selectOptions(from, screen.getByRole("option", { name: "baseline-hybrid" }));
    const a = await screen.findByRole("region", { name: "Chunks from baseline-hybrid" });
    await within(a).findAllByText(/^#\d+$/);
    await user.selectOptions(screen.getByLabelText("Compare with"), "structure-1200");
    const b = await screen.findByRole("region", { name: "Chunks from structure-1200" });
    expect(await within(b).findAllByText("context")).not.toHaveLength(0); // structure chunks carry the heading path
    expect(within(a).queryByText("context")).toBeNull();
    await user.selectOptions(screen.getByLabelText("Compare with"), "rerank-jina");
    expect(await screen.findByText(/Both pipelines use the same chunk step/)).toBeInTheDocument();
  });

  it("shows a chunk's enrichment only for a pipeline with an enrich node", async () => {
    const user = userEvent.setup();
    const docId = buildScenario("ready").documents.find((d) => d.corpus_id === support)!.id;
    renderApp(`/corpora/${support}/documents/${docId}?tab=chunks`, "ready", (s) => {
      const p = createPipeline(s, support, hybrid("enriched", { enrich: "situate" }));
      completeRuns(s, p.nodes.map((n) => n.node_fp));
    });
    await user.selectOptions(await screen.findByLabelText("Chunks from"), screen.getByRole("option", { name: "baseline-hybrid" }));
    const plain = await screen.findByRole("region", { name: "Chunks from baseline-hybrid" });
    await within(plain).findAllByText(/^#\d+$/);
    expect(within(plain).queryByText("enrichment")).toBeNull();
    await user.selectOptions(screen.getByLabelText("Compare with"), "enriched");
    const enriched = await screen.findByRole("region", { name: "Chunks from enriched" });
    expect((await within(enriched).findAllByText("enrichment")).length).toBeGreaterThan(0);
  });

  it("validates metadata edits against the corpus schema", async () => {
    const user = userEvent.setup();
    const docId = buildScenario("ready").documents.find((d) => d.corpus_id === support)!.id;
    renderApp(`/corpora/${support}/documents/${docId}`, "ready");
    await user.click(await screen.findByRole("button", { name: "Edit metadata" }));
    const year = screen.getByLabelText(/year/);
    await user.clear(year);
    await user.type(year, "soon");
    await user.click(screen.getByRole("button", { name: "Save metadata" }));
    expect(await screen.findByText("Enter a number.")).toBeInTheDocument();
    await user.clear(year);
    await user.type(year, "2023");
    await user.click(screen.getByRole("button", { name: "Save metadata" }));
    await screen.findByRole("button", { name: "Edit metadata" });
    const { db } = await import("../../mocks/db");
    expect(db().documents.find((d) => d.id === docId)!.metadata.year).toBe(2023);
  });
});
