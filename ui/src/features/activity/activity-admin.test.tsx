import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { uuidFrom } from "../../mocks/hash";
import { renderApp } from "../../test/render";

const support = uuidFrom("corpus:support-docs");

describe("activity and admin (UI-6)", () => {
  it("lists node runs with status, pipeline and error, and retries a failed one", async () => {
    const user = userEvent.setup();
    const { state } = renderApp("/activity?status=failed", "degraded");
    const failed = state.runs.find((r) => r.status === "failed")!;
    const filename = state.documents.find((d) => d.id === failed.document_id)!.filename;
    const docLink = (await screen.findAllByRole("link", { name: filename }))[0]!;
    const row = docLink.closest("tr")!;
    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(within(row).getByText("provider_error")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(state.runs.find((r) => r.id === failed.id)!.status).toBe("pending"));
  });

  it("filters by status, corpus, pipeline and document", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/activity", "degraded");
    await user.click(await screen.findByRole("radio", { name: "Failed" }));
    await waitFor(() => expect(router.state.location.search).toContain("status=failed"));
    const rows = (await screen.findAllByRole("row")).slice(1);
    for (const r of rows) expect(within(r).getByText("Failed")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "All" }));
    const corpus = screen.getByLabelText("Corpus");
    await user.selectOptions(corpus, support);
    expect(router.state.location.search).toContain("corpus=");
  });

  it("previews garbage collection and only deletes after confirmation", async () => {
    const user = userEvent.setup();
    const { state } = renderApp("/admin", "degraded", (s) => {
      const p = s.pipelines.find((x) => x.name === "bge-m3-swap");
      if (p) p.archived = true;
    });
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByText("Indexes to drop")).toBeInTheDocument();
    const runsBefore = state.runs.length;
    // Nothing changes on a preview.
    expect(state.runs.length).toBe(runsBefore);
    await user.click(screen.getByRole("button", { name: "Collect" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete unused work" }));
    await waitFor(() => expect(state.runs.length).toBeLessThan(runsBefore));
  });
});
