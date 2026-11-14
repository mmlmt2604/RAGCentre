import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { db } from "../mocks/db";
import { DEV_TOKEN } from "../mocks/scenarios";
import { renderApp } from "../test/render";

describe("application shell", () => {
  it("shows a not-found page for an unknown route", async () => {
    renderApp("/no/such/page");
    expect(await screen.findByRole("heading", { name: "Nothing at this address" })).toBeInTheDocument();
  });

  it("lists corpora in the ready scenario and explains corpora in the empty one", async () => {
    const { unmount } = renderApp("/", "ready");
    expect(await screen.findByRole("link", { name: "support-docs" })).toBeInTheDocument();
    unmount();
    renderApp("/", "empty");
    expect(await screen.findByRole("heading", { name: "No corpora yet" })).toBeInTheDocument();
  });

  it("asks for a token on 401 and loads data once a valid one is entered", async () => {
    const user = userEvent.setup();
    renderApp("/", "auth");
    expect(await screen.findByText("Access token required", { selector: "dialog h2" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Token"), DEV_TOKEN);
    await user.click(screen.getByRole("button", { name: "Use token" }));
    expect(await screen.findByRole("link", { name: "support-docs" })).toBeInTheDocument();
  });

  it("shows a retryable error rather than a blank page when the server is unreachable", async () => {
    renderApp("/", "ready", (s) => {
      s.failNetwork = true;
    });
    try {
      const user = userEvent.setup();
      expect(await screen.findByRole("heading", { name: "Can't reach RAGCentre" })).toBeInTheDocument();
      db().failNetwork = false;
      await user.click(screen.getAllByRole("button", { name: /retry/i })[0]!);
      expect(await screen.findByRole("link", { name: "support-docs" })).toBeInTheDocument();
    } finally {
      db().failNetwork = false;
    }
  });
});

describe("pipelines list", () => {
  it("puts the degraded pipeline first and retries its failed runs", async () => {
    const user = userEvent.setup();
    const { state } = renderApp("/", "degraded");
    const corpus = state.corpora.find((c) => c.name === "support-docs")!;
    await user.click(await screen.findByRole("link", { name: "support-docs" }));

    await screen.findByText("bge-m3-swap", { selector: ".pl-name__text" });
    const body = document.querySelectorAll<HTMLElement>("tr.pl-row");
    expect(within(body[0]!).getByText("bge-m3-swap")).toBeInTheDocument();
    expect(within(body[0]!).getByText("Degraded")).toBeInTheDocument();

    const panel = screen.getByRole("complementary", { name: "bge-m3-swap details" });
    expect(await within(panel).findByText("provider_error")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Retry 14 failed runs" }));
    expect(await screen.findByText("Retrying 14 runs")).toBeInTheDocument();
    expect(state.runs.filter((r) => r.corpus_id === corpus.id && r.status === "failed")).toHaveLength(0);
  });

  it("refuses to archive the active pipeline and explains why", async () => {
    const user = userEvent.setup();
    const { state } = renderApp("/", "ready");
    const corpus = state.corpora.find((c) => c.name === "support-docs")!;
    await user.click(await screen.findByRole("link", { name: "support-docs" }));
    await user.click(await screen.findByText("baseline-hybrid", { selector: ".pl-name__text" }));
    const panel = await screen.findByRole("complementary", { name: "baseline-hybrid details" });
    await user.click(within(panel).getByRole("button", { name: "Archive pipeline" }));
    const dialog = (await screen.findByText("Archive baseline-hybrid?", { selector: "dialog h2" })).closest("dialog")!;
    await user.click(within(dialog).getByRole("button", { name: "Archive pipeline" }));
    expect(await screen.findByText(/is the active pipeline/)).toBeInTheDocument();
    expect(state.pipelines.find((p) => p.id === corpus.active_pipeline_id)!.archived).toBe(false);
  });
});
