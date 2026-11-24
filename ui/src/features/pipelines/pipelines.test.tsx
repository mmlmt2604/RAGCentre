import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { tick } from "../../mocks/engine";
import { uuidFrom } from "../../mocks/hash";
import { renderApp, server } from "../../test/render";

const support = uuidFrom("corpus:support-docs");
const pid = (name: string) => uuidFrom(`pipeline:${support}:${name}`);
const base = `/corpora/${support}/pipelines`;

describe("pipelines (UI-3)", () => {
  it("builds a valid pipeline from scratch using only the form", async () => {
    const user = userEvent.setup();
    const { router, state } = renderApp(`${base}/new`);
    await user.click(await screen.findByRole("button", { name: /^Blank/ }));
    await user.type(await screen.findByLabelText(/^Name/), "from-scratch");
    const graph = screen.getByRole("group", { name: "Pipeline steps" });
    await user.click(within(graph).getByRole("button", { name: "Chunk" }));
    await user.click(within(graph).getByRole("button", { name: "Embed" }));
    await user.selectOptions(screen.getByLabelText("Embedder"), "bge-small");
    await user.click(within(graph).getByRole("button", { name: "Vector index" }));
    await user.selectOptions(screen.getByLabelText("Vector store"), "local-sqlite");
    await user.click(screen.getByRole("button", { name: "Add source" }));
    const plan = screen.getByRole("complementary", { name: "Plan" });
    await waitFor(() => expect(within(plan).getByText("Valid")).toBeInTheDocument(), { timeout: 3000 });
    // Its parse route differs from the existing pipelines' (no PDF route), so every step is new.
    expect(within(plan).getByText(/Creating it starts/)).toHaveTextContent("Creating it starts 2,560 runs over 640 documents.");
    await user.click(within(plan).getByRole("button", { name: "Create pipeline" }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(new RegExp(`${base}/[0-9a-f-]{36}$`)));
    expect(state.pipelines.some((p) => p.name === "from-scratch")).toBe(true);
    expect(await screen.findByRole("heading", { name: "from-scratch" })).toBeInTheDocument();
  });

  it("adds an enrich node, wires the embed and keyword nodes to it, and lists only LLM providers", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`${base}/new?template=hybrid`);
    await user.type(await screen.findByLabelText(/^Name/), "contextual");
    const graph = screen.getByRole("group", { name: "Pipeline steps" });
    await user.click(within(graph).getByRole("button", { name: "Enrich" }));
    const editor = screen.getByRole("region", { name: "Settings for enrich" });
    expect(within(editor).getByLabelText("Reads from")).toHaveValue("chunk");
    const llm = within(editor).getByLabelText("LLM");
    expect(within(llm).getAllByRole("option").map((o) => o.textContent)).toEqual(["Choose…", "qwen-llm (openai_compatible)"]);
    await user.selectOptions(llm, "qwen-llm");
    await user.selectOptions(within(editor).getByLabelText("Mode"), "keywords");
    expect(within(editor).getByText(/keywords and synonyms/)).toBeInTheDocument();
    for (const id of ["embed", "fts"]) {
      await user.click(within(graph).getByText(id).closest("button")!);
      await user.selectOptions(screen.getByLabelText("Reads from"), "enrich");
    }
    const plan = screen.getByRole("complementary", { name: "Plan" });
    await waitFor(() => expect(within(plan).getByText("Valid")).toBeInTheDocument(), { timeout: 3000 });
    // Parse and chunk already exist on this corpus; the enrich step and everything after it are new.
    const enrich = within(plan).getByText("enrich").closest("li")!;
    expect(within(enrich).getByText(/new · 640 runs/)).toBeInTheDocument();
    await user.click(within(plan).getByRole("button", { name: "Create pipeline" }));
    await waitFor(() => expect(state.pipelines.some((p) => p.name === "contextual")).toBe(true));
    const created = state.pipelines.find((p) => p.name === "contextual")!;
    expect(created.config.nodes.find((n) => n.id === "enrich")).toMatchObject({ type: "enrich", provider: "qwen-llm", options: { mode: "keywords" } });
  });

  it("shows each validation problem next to the node it names", async () => {
    const user = userEvent.setup();
    renderApp(`${base}/new?template=dense`, "ready", (s) => {
      // No embedders: the template's embed node cannot be filled in.
      s.providers = s.providers.filter((p) => p.kind !== "embedder");
    });
    await user.type(await screen.findByLabelText(/^Name/), "needs-embedder");
    const plan = screen.getByRole("complementary", { name: "Plan" });
    const link = await within(plan).findByRole("button", { name: "embed" }, { timeout: 3000 });
    expect(within(plan).getByText("Choose an embedder provider.")).toBeInTheDocument();
    const graph = screen.getByRole("group", { name: "Pipeline steps" });
    const node = within(graph).getByText("embed").closest<HTMLElement>("[data-node]")!;
    expect(within(node).getByLabelText("1 problem")).toBeInTheDocument();
    await user.click(link);
    expect(screen.getByRole("region", { name: "Settings for embed" })).toBeInTheDocument();
    expect(within(plan).getByRole("button", { name: "Create pipeline" })).toBeDisabled();
  });

  it("pre-fills a clone; the plan shows parse reused before anything is created", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`${base}/new?from=${pid("baseline-hybrid")}`);
    expect(await screen.findByLabelText(/^Name/)).toHaveValue("baseline-hybrid-copy");
    const runsBefore = state.runs.length;
    const graph = screen.getByRole("group", { name: "Pipeline steps" });
    await user.click(within(graph).getByText("chunk").closest("button")!);
    await user.selectOptions(screen.getByLabelText("Strategy"), "fixed");
    const plan = screen.getByRole("complementary", { name: "Plan" });
    await waitFor(() => expect(within(plan).getByText(/Creating it starts/)).toBeInTheDocument(), { timeout: 3000 });
    const parse = within(plan).getByText("parse").closest("li")!;
    expect(within(parse).getByText(/reused/)).toBeInTheDocument();
    const chunk = within(plan).getByText("chunk").closest("li")!;
    expect(within(chunk).getByText(/new · 640 runs/)).toBeInTheDocument();
    expect(state.runs.length).toBe(runsBefore);
  });

  it("switches between form and YAML, and refuses to switch on invalid text", async () => {
    const user = userEvent.setup();
    renderApp(`${base}/new?template=hybrid`);
    await screen.findByLabelText(/^Name/);
    await user.click(screen.getByRole("tab", { name: "YAML" }));
    const editor = screen.getByLabelText("Pipeline configuration") as HTMLTextAreaElement;
    expect(editor.value).toContain("type: index_fts");
    await user.clear(editor);
    await user.click(editor);
    await user.paste("name: [unclosed");
    await user.click(screen.getByRole("tab", { name: "Form" }));
    expect(await screen.findByText(/Fix the text before switching to the form/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveAttribute("aria-selected", "true");
  });

  it.each([
    ["name: [unclosed", /Flow sequence/],
    ['name: bad\nnodes: [null]\nretrieval: {sources: [], top_k: 8}', /needs a string id/],
    ['name: bad\nnodes: [{id: parse, type: parse}]\nretrieval: {sources: [], top_k: 8}', /routes must contain/],
  ])("blocks creation for invalid YAML without submitting the last valid configuration: %s", async (text, message) => {
    const user = userEvent.setup();
    const { state } = renderApp(`${base}/new?from=${pid("baseline-hybrid")}`);
    const plan = await screen.findByRole("complementary", { name: "Plan" });
    await waitFor(() => expect(within(plan).getByRole("button", { name: "Create pipeline" })).toBeEnabled());
    const before = state.pipelines.length;
    await user.click(screen.getByRole("tab", { name: "YAML" }));
    const editor = screen.getByLabelText("Pipeline configuration");
    await user.clear(editor);
    await user.click(editor);
    await user.paste(text);
    expect(screen.getAllByText(message).length).toBeGreaterThan(0);
    expect(within(plan).getByRole("button", { name: "Create pipeline" })).toBeDisabled();
    await user.click(screen.getByRole("tab", { name: "Form" }));
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveAttribute("aria-selected", "true");
    expect(state.pipelines.length).toBe(before);
  });

  it("shows a failed plan refresh and blocks the stale valid plan until retry succeeds", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderApp(`${base}/new?from=${pid("baseline-hybrid")}`);
    const plan = await screen.findByRole("complementary", { name: "Plan" });
    await waitFor(() => expect(within(plan).getByRole("button", { name: "Create pipeline" })).toBeEnabled());
    server.use(http.post("/v1/corpora/:id/pipelines", () => HttpResponse.error()));
    await queryClient.invalidateQueries({ predicate: (query) => query.queryKey.includes("plan") });
    expect(await within(plan).findByText("Could not check the pipeline")).toBeInTheDocument();
    expect(within(plan).getByRole("button", { name: "Create pipeline" })).toBeDisabled();
    server.resetHandlers();
    await user.click(within(plan).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(plan).getByRole("button", { name: "Create pipeline" })).toBeEnabled());
  });

  it("shows per-node progress on the detail page and updates it by polling", async () => {
    const { state } = renderApp(`${base}/${pid("structure-1200")}`, "building");
    const graph = await screen.findByRole("group", { name: /structure-1200 steps/ });
    const chunk = within(graph).getByText("chunk").closest<HTMLElement>("[data-node]")!;
    await waitFor(() => expect(within(chunk).getByRole("img", { name: /pending/ })).toBeInTheDocument());
    for (let i = 0; i < 500 && state.runs.some((r) => r.status !== "succeeded" && r.status !== "failed"); i++) tick(state);
    await waitFor(() => expect(within(chunk).getByRole("img", { name: /^640 succeeded$/ })).toBeInTheDocument(), { timeout: 4000 });
    expect(within(graph).getByText("parse").closest<HTMLElement>("[data-node]")!.textContent).toMatch(/shared with/);
  });

  it("marks shared and changed nodes in the diff", async () => {
    renderApp(`${base}/compare?a=${pid("baseline-hybrid")}&b=${pid("structure-1200")}`);
    expect(await screen.findByText(/Parse is shared\. Chunk, embed and index differ\./)).toBeInTheDocument();
    const table = screen.getByRole("table");
    const parse = within(table).getByText("parse").closest("tr")!;
    expect(within(parse).getByText("identical")).toBeInTheDocument();
    const chunk = within(table).getAllByText("chunk")[0]!.closest("tr")!;
    expect(within(chunk).getByText("changed")).toBeInTheDocument();
    expect(within(table).getByText("strategy")).toBeInTheDocument();
    expect(within(table).getByText("recursive")).toBeInTheDocument();
    expect(within(table).getByText("structure")).toBeInTheDocument();
  });

  it("asks for confirmation before making a non-ready pipeline active", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`${base}/${pid("structure-1200")}`, "building");
    await user.click(await screen.findByRole("button", { name: "Set active" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Only/)).toHaveTextContent(/searchable/);
    await user.click(within(dialog).getByRole("button", { name: "Make it active anyway" }));
    await waitFor(() => expect(state.corpora.find((c) => c.id === support)!.active_pipeline_id).toBe(pid("structure-1200")));
  });

  it("explains why the active pipeline cannot be archived", async () => {
    const user = userEvent.setup();
    renderApp(`${base}/${pid("baseline-hybrid")}`);
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/the server will refuse until another one is made active/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archive pipeline" }));
    expect(await within(dialog).findByText(/is the active pipeline\. Make another pipeline active before archiving it\./)).toBeInTheDocument();
    expect(within(dialog).getByText("Make another pipeline active first, then archive this one.")).toBeInTheDocument();
  });
});
