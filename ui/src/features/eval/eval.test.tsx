import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { advanceRuns, metricNames, pairedBootstrap } from "../../mocks/eval";
import { uuidFrom } from "../../mocks/hash";
import { renderApp } from "../../test/render";

const support = uuidFrom("corpus:support-docs");
const pid = (name: string) => uuidFrom(`pipeline:${support}:${name}`);
const base = `/corpora/${support}/eval`;

const jsonl = (lines: unknown[]) => new File([lines.map((l) => (typeof l === "string" ? l : JSON.stringify(l))).join("\n")], "qa.jsonl", { type: "application/x-ndjson" });

describe("evaluation (UI-5)", () => {
  it("imports a JSON Lines dataset", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(base);
    const doc = state.documents.find((d) => d.corpus_id === support && d.external_id)!;
    await user.click(await screen.findByRole("button", { name: "Upload dataset" }));
    await user.type(screen.getByLabelText("Name"), "smoke");
    await user.upload(screen.getByTestId("dataset-input"), jsonl([{ question: "q one", targets: [{ document: doc.external_id }] }, { question: "q two", targets: [{ document: doc.id, pages: [1] }] }]));
    await user.click(screen.getByRole("button", { name: "Import dataset" }));
    expect(await screen.findByRole("link", { name: "smoke" })).toBeInTheDocument();
    expect(state.datasets.find((d) => d.name === "smoke")?.item_count).toBe(2);
  });

  it("lists import errors with their line numbers and imports nothing", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(base);
    await user.click(await screen.findByRole("button", { name: "Upload dataset" }));
    await user.upload(
      screen.getByTestId("dataset-input"),
      jsonl([{ question: "fine", targets: [{ document: "no-such-doc" }] }, "{not json", { question: "", targets: [] }, { question: "x", targets: [{ document: "a" }], extra: 1 }]),
    );
    await user.click(screen.getByRole("button", { name: "Import dataset" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/The file has 4 problems; nothing was imported\./)).toBeInTheDocument();
    expect(within(alert).getByText("line 1").parentElement).toHaveTextContent("targets[0]: unknown document 'no-such-doc'");
    expect(within(alert).getByText("line 2").parentElement).toHaveTextContent(/invalid JSON/);
    expect(within(alert).getByText("line 3").parentElement).toHaveTextContent("'question' must be a non-empty string");
    expect(within(alert).getByText("line 4").parentElement).toHaveTextContent("unknown keys: extra");
    expect(state.datasets.some((d) => d.name === "qa")).toBe(false);
  });

  it("starts a run for two pipelines and shows its progress", async () => {
    const user = userEvent.setup();
    const { router, state } = renderApp(`${base}?compare=${pid("baseline-hybrid")},${pid("rerank-jina")}`);
    const start = await screen.findByRole("button", { name: /Start run \(2 pipelines\)/ });
    await user.click(start);
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/eval\/runs\/[0-9a-f-]{36}$/));
    expect(await screen.findByText(/0 of 80 queries/)).toBeInTheDocument();
    advanceRuns(state, 30);
    await waitFor(() => expect(screen.getByText(/30 of 80 queries/)).toBeInTheDocument(), { timeout: 4000 });
    advanceRuns(state, 1000);
    expect(await screen.findByRole("table", undefined, { timeout: 4000 })).toBeInTheDocument();
  });

  it("disables pipelines that are not ready unless partial indexes are allowed", async () => {
    const user = userEvent.setup();
    renderApp(`${base}?panel=run`, "building");
    const box = await screen.findByRole("checkbox", { name: "structure-1200" });
    expect(box).toBeDisabled();
    expect(screen.getByText(/Comparing a partly built index is misleading/)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /Allow partial indexes/ }));
    expect(box).toBeEnabled();
  });

  it("compares every metric for every pipeline, with differences, intervals and significance", async () => {
    renderApp(`${base}/runs/seeded-run`, "ready");
    const table = await screen.findByRole("table", undefined, { timeout: 8000 });
    for (const m of metricNames([1, 3, 5, 10])) expect(within(table).getByRole("rowheader", { name: m })).toBeInTheDocument();
    const header = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(header.some((h) => h?.includes("baseline-hybrid"))).toBe(true);
    expect(header.some((h) => h?.includes("structure-1200"))).toBe(true);
    expect(header).toEqual(expect.arrayContaining(["Diff (b − a)", "95% CI", "Significant"]));
    const recall = within(table).getByRole("rowheader", { name: "recall@10" }).closest("tr")!;
    expect(within(recall).getByText(/^yes/)).toBeInTheDocument();
    expect(within(recall).getByText(/ to /)).toBeInTheDocument();
    expect(within(recall).getByLabelText("best")).toBeInTheDocument();
    // Some metrics are not significant, and the summary does not overstate them.
    expect(within(table).getAllByText("no").length).toBeGreaterThan(0);
    expect(screen.getByRole("region", { name: "Summary" })).toHaveTextContent(/are not significant with 40 questions and could be chance/);
    expect(within(table).getByRole("rowheader", { name: "latency p50" })).toBeInTheDocument();
    expect(within(table).getByRole("rowheader", { name: "mean chunk length" })).toBeInTheDocument();
  });

  it("lists the questions where two pipelines differ, with both result lists and matches marked", { timeout: 20000 }, async () => {
    const user = userEvent.setup();
    renderApp(`${base}/runs/seeded-run/items?a=${pid("baseline-hybrid")}&b=${pid("structure-1200")}&metric=recall%4010`, "ready");
    const better = await screen.findByRole("tab", { name: /Better in structure-1200/ }, { timeout: 9000 });
    expect(better).toHaveAttribute("aria-selected", "true");
    const items = screen.getAllByRole("button", { expanded: false }).filter((b) => b.className.includes("id-item__head"));
    await user.click(items[0]!);
    const a = screen.getByRole("region", { name: "baseline-hybrid results" });
    const b = screen.getByRole("region", { name: "structure-1200 results" });
    expect(within(b).getAllByLabelText(/^matches target 1/).length).toBeGreaterThan(0);
    expect(within(a).queryAllByLabelText(/^matches target/)).toHaveLength(0);
    expect(within(a).getAllByLabelText("matches no target").length).toBeGreaterThan(0);
  });

  it("refuses to delete a dataset while a run on it is in progress", async () => {
    const user = userEvent.setup();
    let dsId = "";
    renderApp(`${base}`, "ready", (s) => {
      dsId = s.datasets[0]!.id;
      s.evalRuns.push({ ...s.evalRuns[0]!, id: "busy", status: "running", deferred: undefined });
    });
    const datasets = (await screen.findByRole("heading", { name: "Datasets" })).closest("section")!;
    await user.click(await within(datasets).findByRole("link", { name: "support-golden" }));
    void dsId;
    await user.click(await screen.findByRole("button", { name: "Delete dataset" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete dataset" }));
    expect(await within(dialog).findByText("An evaluation run on this dataset is still in progress.")).toBeInTheDocument();
  });

  it("computes the paired bootstrap like the backend", () => {
    const same = pairedBootstrap([1, 0, 1, 0], [1, 0, 1, 0]);
    expect([same.diff, same.ci_low, same.ci_high, same.significant]).toEqual([0, 0, 0, false]);
    const up = pairedBootstrap(Array(30).fill(0), Array(30).fill(1));
    expect(up.significant).toBe(true);
    expect(up.ci_low).toBeGreaterThan(0);
  });
});
