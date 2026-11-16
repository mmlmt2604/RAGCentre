import "@testing-library/jest-dom/vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
// The options_schema each shim reports (0.3.0), as tests/fixtures/option_problems.json keeps them.
import table from "../../../../tests/fixtures/option_problems.json";
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

describe("parse route options from the parser's options_schema", () => {
  type Route = { mime: string; provider: string; options?: Record<string, unknown> };
  /** Record every plan request, so a test can read the configuration the form is building. */
  function spyOnPlans() {
    const bodies: { nodes: { type: string; routes?: Route[] }[] }[] = [];
    server.use(
      http.post("/v1/corpora/:id/pipelines", async ({ request }) => {
        bodies.push((await request.clone().json()) as (typeof bodies)[number]);
        return undefined; // fall through to the mock
      }),
    );
    const route = () => bodies.at(-1)?.nodes.find((n) => n.type === "parse")?.routes?.[1];
    return { route };
  }
  async function openParse(user: ReturnType<typeof userEvent.setup>) {
    const graph = await screen.findByRole("group", { name: "Pipeline steps" });
    await user.click(within(graph).getByText("parse").closest("button")!);
    return screen.getByRole("region", { name: "Settings for parse" });
  }
  const clonePath = `${base}/new?from=${pid("baseline-hybrid")}`;

  it("renders one control per schema property, in order, with defaults as hints", async () => {
    const user = userEvent.setup();
    renderApp(clonePath);
    const parse = await openParse(user);
    const form = within(parse).getByRole("group", { name: "Parser options" });
    const labels = within(form).getAllByText(/^(ocr|ocr_languages|include_page_images|image_dpi|tables|table_mode|page_range|engine)$/).map((e) => e.textContent);
    expect(labels).toEqual(["ocr", "ocr_languages", "include_page_images", "image_dpi", "tables", "table_mode", "page_range", "engine"]);
    // The stored option is shown; a stored false/true default is not invented for the others.
    expect(within(form).getByRole("checkbox", { name: "ocr" })).toBeChecked();
    expect(within(form).getByRole("checkbox", { name: "include_page_images" })).not.toBeChecked();
    expect(within(form).getByRole("checkbox", { name: "include_page_images" })).toHaveAccessibleDescription(/Default false\./);
    expect(within(form).getByRole("combobox", { name: "table_mode" })).toHaveValue("");
    expect(within(form).getByRole("textbox", { name: "image_dpi" })).toHaveAccessibleDescription(/Default 192\..*72–400/);
    expect(within(form).getByRole("textbox", { name: "engine" })).toBeInTheDocument();
    // A nullable pair of whole numbers is two number boxes under one label.
    const range = within(form).getByRole("group", { name: "page_range" });
    expect(within(range).getByRole("textbox", { name: "first" })).toBeInTheDocument();
    expect(within(range).getByRole("textbox", { name: "last" })).toBeInTheDocument();
    // The built-in text route has no schema and keeps its JSON box.
    expect(within(parse).getByRole("textbox", { name: "Options" })).toBeInTheDocument();
  });

  it("leaves unset controls out of the options, never writes defaults, and resets a control to unset", async () => {
    const user = userEvent.setup();
    renderApp(clonePath);
    const { route } = spyOnPlans();
    const parse = await openParse(user);
    const form = within(parse).getByRole("group", { name: "Parser options" });

    await user.click(form.querySelector<HTMLInputElement>("input[type=checkbox]:not(:checked)")!);
    await user.selectOptions(within(form).getByRole("combobox", { name: "table_mode" }), "fast");
    await user.type(within(form).getByRole("textbox", { name: "image_dpi" }), "150");
    await user.type(within(form).getByRole("textbox", { name: "ocr_languages" }), "en,  de");
    await waitFor(() => expect(route()?.options).toBeDefined(), { timeout: 3000 });
    await waitFor(() => expect(route()?.options).toEqual({ ocr: true, include_page_images: true, table_mode: "fast", image_dpi: 150, ocr_languages: ["en", "de"] }), { timeout: 3000 });

    await user.click(within(form).getByRole("button", { name: "Reset include_page_images" }));
    await user.click(within(form).getByRole("button", { name: "Reset image_dpi" }));
    await user.click(within(form).getByRole("button", { name: "Reset ocr_languages" }));
    await waitFor(() => expect(route()?.options).toEqual({ ocr: true, table_mode: "fast" }), { timeout: 3000 });
    expect(within(form).queryByRole("button", { name: "Reset include_page_images" })).not.toBeInTheDocument();

    // Clearing the last settings leaves no options key at all.
    await user.click(within(form).getByRole("button", { name: "Reset table_mode" }));
    await user.click(within(form).getByRole("button", { name: "Reset ocr" }));
    await waitFor(() => expect(route()).toEqual({ mime: "*/*", provider: "docling" }), { timeout: 3000 });
    expect(within(form).getByRole("checkbox", { name: "ocr" })).toBeChecked(); // shows the default; nothing is stored
  });

  it("checks number limits in the control and does not store a value outside them", async () => {
    const user = userEvent.setup();
    renderApp(clonePath);
    const { route } = spyOnPlans();
    const parse = await openParse(user);
    const form = within(parse).getByRole("group", { name: "Parser options" });
    await user.type(within(form).getByRole("textbox", { name: "image_dpi" }), "500");
    expect(within(form).getByText("Enter a value 72–400.")).toBeInTheDocument();
    await user.tab();
    await waitFor(() => expect(route()).toBeDefined(), { timeout: 3000 });
    expect(route()?.options).toEqual({ ocr: true });
  });

  it("switches to a JSON box for the whole object and back, keeping the values", async () => {
    const user = userEvent.setup();
    renderApp(clonePath);
    const { route } = spyOnPlans();
    const parse = await openParse(user);
    await user.click(within(parse).getByRole("button", { name: "Edit as JSON" }));
    const box = within(parse).getByRole("textbox", { name: "Options as JSON" });
    expect(box).toHaveValue('{"ocr":true}');
    expect(within(parse).queryByRole("group", { name: "Parser options" })).not.toBeInTheDocument();
    await user.clear(box);
    await user.click(box);
    await user.paste('{"ocr": false, "engine": {"beam": 4}}');
    await user.tab();
    await waitFor(() => expect(route()?.options).toEqual({ ocr: false, engine: { beam: 4 } }), { timeout: 3000 });
    await user.click(within(parse).getByRole("button", { name: "Edit as form" }));
    const form = within(parse).getByRole("group", { name: "Parser options" });
    expect(within(form).getByRole("checkbox", { name: "ocr" })).not.toBeChecked();
    expect(within(form).getByRole("textbox", { name: "engine" })).toHaveValue('{"beam":4}');
    // One key's JSON box edits that key only.
    const engine = within(form).getByRole("textbox", { name: "engine" });
    await user.clear(engine);
    await user.tab();
    await waitFor(() => expect(route()?.options).toEqual({ ocr: false }), { timeout: 3000 });
  });

  it("shows server problems on the control they name, including an option the parser does not have", async () => {
    const user = userEvent.setup();
    renderApp(clonePath);
    const parse = await openParse(user);
    await user.click(within(parse).getByRole("button", { name: "Edit as JSON" }));
    const box = within(parse).getByRole("textbox", { name: "Options as JSON" });
    await user.clear(box);
    await user.click(box);
    await user.paste('{"ocr": "yes", "image_dpi": 900, "table_mode": "slow", "page_range": [1, 2, 3], "bogus": 1}');
    await user.tab();
    // The plan panel lists the same problems; wait for them to come back from the server.
    expect(await within(parse).findByText(/bogus: This parser has no such option\./, undefined, { timeout: 3000 })).toBeInTheDocument();
    await user.click(within(parse).getByRole("button", { name: "Edit as form" }));
    const form = within(parse).getByRole("group", { name: "Parser options" });
    expect(within(form).getByRole("checkbox", { name: "ocr" })).toHaveAccessibleDescription("Must be true or false.");
    expect(within(form).getByRole("textbox", { name: "image_dpi" })).toHaveAccessibleDescription("Must be at most 400.");
    expect(within(form).getByRole("combobox", { name: "table_mode" })).toHaveAccessibleDescription(/^Must be one of: "fast", "accurate"\.$/);
    expect(within(form).getByRole("group", { name: "page_range" })).toHaveTextContent("Must have at most 2 items or be null.");
    expect(within(form).getByRole("textbox", { name: "bogus" })).toHaveAccessibleDescription("This parser has no such option.");
    // The unknown option can be removed from the form.
    await user.click(within(form).getByRole("button", { name: "Remove bogus" }));
    expect(within(form).queryByRole("textbox", { name: "bogus" })).not.toBeInTheDocument();
  });

  it("falls back to the JSON box when the provider reports no schema", async () => {
    const user = userEvent.setup();
    renderApp(clonePath, "ready", (s) => {
      s.providers.find((p) => p.name === "docling")!.config.options_schema = null;
    });
    const parse = await openParse(user);
    expect(within(parse).queryByRole("group", { name: "Parser options" })).not.toBeInTheDocument();
    expect(within(parse).queryByRole("button", { name: "Edit as JSON" })).not.toBeInTheDocument();
    const boxes = within(parse).getAllByRole("textbox", { name: "Options" });
    expect(boxes).toHaveLength(2);
    expect(boxes[1]).toHaveValue('{"ocr":true}');
  });

  it("falls back to the JSON box when the provider has no options_schema key at all", async () => {
    const user = userEvent.setup();
    renderApp(clonePath, "ready", (s) => {
      delete s.providers.find((p) => p.name === "docling")!.config.options_schema;
    });
    const parse = await openParse(user);
    expect(within(parse).getAllByRole("textbox", { name: "Options" })).toHaveLength(2);
  });
});


describe("the parser options form with the schemas the four shims report", () => {
  type Route = { mime: string; provider: string; options?: Record<string, unknown> };
  const clonePath = `${base}/new?from=${pid("baseline-hybrid")}`;
  const schemas = table.schemas as Record<string, unknown>;

  /** Render the pipeline editor with the "docling" provider reporting another shim's schema, and open its parse node. */
  async function openWith(shim: "docling" | "mineru" | "paddleocr" | "surya") {
    const user = userEvent.setup();
    renderApp(clonePath, "ready", (s) => {
      s.providers.find((p) => p.name === "docling")!.config.options_schema = structuredClone(schemas[shim]);
    });
    const bodies: { nodes: { type: string; routes?: Route[] }[] }[] = [];
    server.use(
      http.post("/v1/corpora/:id/pipelines", async ({ request }) => {
        bodies.push((await request.clone().json()) as (typeof bodies)[number]);
        return undefined;
      }),
    );
    const graph = await screen.findByRole("group", { name: "Pipeline steps" });
    await user.click(within(graph).getByText("parse").closest("button")!);
    const parse = screen.getByRole("region", { name: "Settings for parse" });
    const form = within(parse).getByRole("group", { name: "Parser options" });
    const options = () => bodies.at(-1)?.nodes.find((n) => n.type === "parse")?.routes?.[1]?.options;
    const route = () => bodies.at(-1)?.nodes.find((n) => n.type === "parse")?.routes?.[1];
    /** What the route holds right now, read from the JSON view (a plan for a configuration planned before is cached, so no request says it). */
    const stored = async () => {
      await user.click(within(parse).getByRole("button", { name: "Edit as JSON" }));
      const text = (within(parse).getByRole("textbox", { name: "Options as JSON" }) as HTMLInputElement).value;
      await user.click(within(parse).getByRole("button", { name: "Edit as form" }));
      return text ? (JSON.parse(text) as Record<string, unknown>) : {};
    };
    return { user, parse, form, options, route, bodies, stored };
  }

  it("shows a control for every option, each one usable", async () => {
    const roles = {
      docling: { checkbox: ["ocr", "include_page_images", "tables", "force_full_page_ocr", "formulas", "code", "picture_classification", "picture_description", "charts"], combobox: ["pipeline", "vlm_model", "table_mode", "ocr_engine"], textbox: ["languages", "images_scale", "engine"], group: ["page_range"] },
      mineru: { checkbox: ["image_analysis"], combobox: ["tier", "ocr"], textbox: [], group: ["page_range"] },
      paddleocr: { checkbox: ["tables", "formulas", "charts", "seals", "orientation", "unwarp", "heading_levels"], combobox: ["ocr_version", "textline_orientation", "region_detection"], textbox: ["languages", "engine"], group: [] },
      surya: { checkbox: ["include_page_images", "ocr_figures"], combobox: [], textbox: ["dpi"], group: ["page_range"] },
    } as const;
    for (const shim of ["docling", "mineru", "paddleocr", "surya"] as const) {
      const { form } = await openWith(shim);
      for (const [role, names] of Object.entries(roles[shim]))
        for (const name of names) expect(within(form).getByRole(role as "textbox", { name }), `${shim}: ${name}`).toBeInTheDocument();
      cleanup();
    }
  });

  it("writes the JSON value a choice stands for, and nothing for \"Not set\"", async () => {
    const { user, form, options, route } = await openWith("mineru"); // ocr: true | false | "auto"
    const ocr = within(form).getByRole("combobox", { name: "ocr" });
    expect(within(ocr).getAllByRole("option").map((o) => o.textContent)).toEqual(['Not set ("auto")', "true", "false", '"auto"']);
    expect(ocr).toHaveValue("true"); // the route stores ocr: true
    await user.selectOptions(ocr, "false");
    await waitFor(() => expect(options()).toEqual({ ocr: false }), { timeout: 3000 });
    await user.selectOptions(ocr, '"auto"');
    await waitFor(() => expect(options()).toEqual({ ocr: "auto" }), { timeout: 3000 });
    await user.selectOptions(ocr, "");
    await waitFor(() => expect(route()).toEqual({ mime: "*/*", provider: "docling" }), { timeout: 3000 });
  });

  it("shows a nullable boolean as three states", async () => {
    const { user, form, options } = await openWith("paddleocr");
    const sel = within(form).getByRole("combobox", { name: "textline_orientation" });
    expect(sel).toHaveValue(""); // unset is null: PP-StructureV3's own choice
    expect(within(sel).getAllByRole("option").map((o) => o.textContent)).toEqual(["Not set", "true", "false"]);
    await user.selectOptions(sel, "false");
    await waitFor(() => expect(options()).toMatchObject({ textline_orientation: false }), { timeout: 3000 });
    const version = within(form).getByRole("combobox", { name: "ocr_version" });
    expect(within(version).getAllByRole("option")[0]).toHaveTextContent(/^Not set$/); // a null default is no value to name
    await user.selectOptions(version, "PP-OCRv4");
    await waitFor(() => expect(options()).toMatchObject({ textline_orientation: false, ocr_version: "PP-OCRv4" }), { timeout: 3000 });
  });

  it("takes a list of one language as a comma-separated box", async () => {
    const { user, form, options } = await openWith("paddleocr"); // minItems = maxItems = 1
    await user.type(within(form).getByRole("textbox", { name: "languages" }), "en");
    await waitFor(() => expect(options()).toMatchObject({ languages: ["en"] }), { timeout: 3000 });
  });

  it("writes page_range as a complete pair, and keeps what was typed when one box is cleared", async () => {
    const { user, form, options, stored } = await openWith("mineru");
    const range = within(form).getByRole("group", { name: "page_range" });
    const first = within(range).getByRole("textbox", { name: "first" });
    const last = within(range).getByRole("textbox", { name: "last" });
    await user.type(first, "2");
    expect(range).toHaveTextContent("Enter both numbers, or clear both.");
    // The message describes both boxes and marks them invalid, as it does for any other control.
    for (const box of [first, last]) {
      expect(box).toHaveAccessibleDescription(/Enter both numbers, or clear both\./);
      expect(box).toBeInvalid();
    }
    expect(range).toHaveAccessibleDescription(/^Enter both numbers, or clear both\.$/);
    await user.type(last, "5");
    await waitFor(() => expect(options()).toEqual({ ocr: true, page_range: [2, 5] }), { timeout: 3000 });
    expect(range).not.toHaveTextContent("Enter both numbers");
    expect(first).toBeValid();

    await user.clear(last); // half a pair is not stored, and the first box keeps its number
    expect(first).toHaveValue("2");
    expect(range).toHaveTextContent("Enter both numbers, or clear both.");
    expect(await stored()).toEqual({ ocr: true });
  });

  it("does not store a pair the wrong way round or below the minimum", async () => {
    const { user, form, bodies, stored } = await openWith("mineru");
    const range = within(form).getByRole("group", { name: "page_range" });
    await user.type(within(range).getByRole("textbox", { name: "first" }), "5");
    await user.type(within(range).getByRole("textbox", { name: "last" }), "1");
    expect(range).toHaveTextContent("The first number must not be larger than the last.");
    await user.clear(within(range).getByRole("textbox", { name: "last" }));
    await user.type(within(range).getByRole("textbox", { name: "last" }), "0");
    expect(within(range).getByText("Enter a value at least 1.")).toBeInTheDocument();
    expect(await stored()).toEqual({ ocr: true });
    expect(JSON.stringify(bodies)).not.toContain("page_range");
  });

  it("shows a stored page_range and clears it with Reset", async () => {
    const { user, parse, form, stored } = await openWith("mineru");
    await user.click(within(parse).getByRole("button", { name: "Edit as JSON" }));
    const box = within(parse).getByRole("textbox", { name: "Options as JSON" });
    await user.clear(box);
    await user.click(box);
    await user.paste('{"page_range": [3, 4]}');
    await user.tab();
    await user.click(within(parse).getByRole("button", { name: "Edit as form" }));
    const range = within(within(parse).getByRole("group", { name: "Parser options" })).getByRole("group", { name: "page_range" });
    expect(within(range).getByRole("textbox", { name: "first" })).toHaveValue("3");
    expect(within(range).getByRole("textbox", { name: "last" })).toHaveValue("4");
    await user.click(within(form).queryByRole("button", { name: "Reset page_range" })!);
    expect(await stored()).toEqual({});
    const again = within(within(parse).getByRole("group", { name: "Parser options" })).getByRole("group", { name: "page_range" });
    expect(within(again).getByRole("textbox", { name: "first" })).toHaveValue("");
  });

  it("says what an exclusive limit is, and refuses a value on it", async () => {
    const { user, form, options } = await openWith("docling");
    const scale = within(form).getByRole("textbox", { name: "images_scale" });
    expect(scale).toHaveAccessibleDescription(/greater than 0, at most 4/);
    await user.type(scale, "0");
    expect(within(form).getByText("Enter a value greater than 0, at most 4.")).toBeInTheDocument();
    await user.clear(scale);
    await user.type(scale, "2.5");
    await user.tab();
    await waitFor(() => expect(options()).toMatchObject({ images_scale: 2.5 }), { timeout: 3000 });
  });

  it("does not repeat a default the description states, nor print a null one", async () => {
    const { form } = await openWith("docling");
    // "Default: the engine's own default." is in the description; the schema's default is null.
    const languages = within(form).getByRole("textbox", { name: "languages" });
    expect(languages).toHaveAccessibleDescription(/Default: the engine's own default\./);
    expect(languages).not.toHaveAccessibleDescription(/Default none/);
    expect(within(form).getByRole("combobox", { name: "ocr_engine" })).toHaveAccessibleDescription(/Default "auto"\./);
    const mineru = await openWith("mineru");
    expect(within(mineru.form).getByRole("group", { name: "page_range" })).not.toHaveTextContent(/Default none/);
  });

  it("keeps JSON that does not parse in the box, says so, and does not store it", async () => {
    const { user, parse, form, bodies, stored } = await openWith("docling");
    const engine = within(form).getByRole("textbox", { name: "engine" });
    await user.type(engine, "{{oops");
    await user.tab();
    expect(engine).toHaveValue("{oops");
    expect(engine).toHaveAccessibleDescription("Not valid JSON.");
    expect(engine).toBeInvalid();
    await waitFor(() => expect(bodies.length).toBeGreaterThan(0), { timeout: 3000 });
    expect(JSON.stringify(bodies)).not.toContain("engine");

    await user.clear(engine);
    await user.type(engine, '{{"beam": 4}');
    await user.tab();
    // The stored value rebuilds the box, so look it up again.
    expect(within(parse).getByRole("textbox", { name: "engine" })).not.toHaveAccessibleDescription("Not valid JSON.");
    expect(await stored()).toEqual({ ocr: true, engine: { beam: 4 } });
  });

  it("clears an error on a JSON box when the option is reset", async () => {
    const { user, form, parse, stored } = await openWith("docling");
    const engine = within(form).getByRole("textbox", { name: "engine" });
    await user.type(engine, '{{"beam": 4}');
    await user.tab();
    await user.click(within(parse).getByRole("button", { name: "Edit as JSON" }));
    await user.click(within(parse).getByRole("button", { name: "Edit as form" }));
    const box = within(parse).getByRole("textbox", { name: "engine" });
    await user.clear(box);
    await user.type(box, "{{bad");
    await user.tab();
    expect(box).toHaveAccessibleDescription("Not valid JSON.");
    await user.click(within(parse).getByRole("button", { name: "Reset engine" }));
    expect(within(parse).getByRole("textbox", { name: "engine" })).not.toHaveAccessibleDescription("Not valid JSON.");
    expect(await stored()).toEqual({ ocr: true });
  });

  it("explains a whole-object JSON box that is not an object", async () => {
    const { user, parse } = await openWith("docling");
    await user.click(within(parse).getByRole("button", { name: "Edit as JSON" }));
    const box = within(parse).getByRole("textbox", { name: "Options as JSON" });
    await user.clear(box);
    await user.click(box);
    await user.paste("[1, 2]");
    await user.tab();
    expect(box).toHaveAccessibleDescription('Must be a JSON object, like {"ocr": true}.');
    await user.clear(box);
    await user.click(box);
    await user.paste('{"ocr": ');
    await user.tab();
    expect(box).toHaveAccessibleDescription("Not valid JSON.");
  });

  it("puts a server problem on the choice and pair controls it names", async () => {
    const { user, parse } = await openWith("mineru");
    await user.click(within(parse).getByRole("button", { name: "Edit as JSON" }));
    const box = within(parse).getByRole("textbox", { name: "Options as JSON" });
    await user.clear(box);
    await user.click(box);
    await user.paste('{"ocr": "maybe", "page_range": [0, 2], "tier": 3}');
    await user.tab();
    expect(await within(parse).findByText(/page_range: Must be at least 1 or null\./, undefined, { timeout: 3000 })).toBeInTheDocument();
    await user.click(within(parse).getByRole("button", { name: "Edit as form" }));
    const form = within(parse).getByRole("group", { name: "Parser options" });
    expect(within(form).getByRole("combobox", { name: "ocr" })).toHaveAccessibleDescription('Must be true or false or "auto".');
    expect(within(form).getByRole("combobox", { name: "ocr" })).toHaveValue('"maybe"'); // shown, as not allowed
    expect(within(form).getByRole("group", { name: "page_range" })).toHaveTextContent("Must be at least 1 or null.");
    expect(within(form).getByRole("combobox", { name: "tier" })).toHaveAccessibleDescription(/^Must be one of: "flash", "basic", "standard", "advanced"\.$/);
  });
});
