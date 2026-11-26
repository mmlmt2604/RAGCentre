import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { configureTransport } from "../../api/client";
import { loadScenario } from "../../mocks/db";
import { server } from "../../test/render";
import { AuthContext, type AuthState } from "../auth/context";
import { DurableSetup } from "./DurableSetup";
import type { SetupOperation, SetupState, SetupTask } from "./setup";

const teamId = "00000000-0000-0000-0000-000000000001";
const order: SetupTask[] = ["parser", "embedder", "store", "corpus", "pipeline", "active", "check_services"];
function initial(): SetupState {
  return { draft: { version: 0, draft: { mode: "trial", corpus_name: "", pipeline_name: "first-pipeline", retrieval: "hybrid", parser: null, embedder: null, store: null, dismissed: false, choice_step: 0 } }, draft_hash: "a".repeat(64), operation: null, readiness: { owner: true, corpus_id: null, pipeline_id: null, pipeline_fingerprint: null, services: false, providers: [], indexed_documents: 0, first_result_at: null, ready: false } };
}
function fixtures(state: SetupState) {
  const db = loadScenario("ready");
  const corpus = db.corpora[0]!;
  const pipeline = db.pipelines.find((p) => p.corpus_id === corpus.id)!;
  const calls: SetupTask[] = [];
  const drafts: unknown[] = [];
  const resource = (step: SetupTask) => step === "corpus" ? corpus.id : ["pipeline", "active"].includes(step) ? pipeline.id : db.providers.find((p) => p.kind === ({ parser: "parser", embedder: "embedder", store: "vector_store" } as Record<string, string>)[step])?.id ?? null;
  const operation = (): SetupOperation => ({ id: "saved-operation", input_version: state.draft.version, input_hash: state.draft_hash, draft: state.draft.draft, steps: order.map((step) => ({ step, state: "pending", resource_id: null, attempts: 0, error_code: null, lease_expires_at: null })), created_at: "2026-11-26", updated_at: "2026-11-26" });
  server.use(
    http.get("/v1/teams/:team/setup", () => HttpResponse.json(state)),
    http.put("/v1/teams/:team/setup-draft", async ({ request }) => { const body = await request.json() as { expected_version: number; draft: SetupState["draft"]["draft"] }; drafts.push(body); if (body.expected_version !== state.draft.version) return HttpResponse.json({ error: { code: "conflict", message: "Setup changed in another tab. Reload it." } }, { status: 409 }); state.draft = { version: state.draft.version + 1, draft: body.draft }; return HttpResponse.json(state); }),
    http.post("/v1/teams/:team/setup-operations", () => { state.operation ??= operation(); return HttpResponse.json(state.operation); }),
    http.get("/v1/teams/:team/setup-operations/:id", () => HttpResponse.json(state.operation)),
    http.post("/v1/teams/:team/setup-operations/:id/steps/:step", ({ params }) => { const step = params.step as SetupTask; calls.push(step); const op = state.operation!; const item = op.steps.find((s) => s.step === step)!; item.state = "succeeded"; item.resource_id = resource(step); if (step === "active") { state.readiness.corpus_id = corpus.id; state.readiness.pipeline_id = pipeline.id; } if (step === "check_services") state.readiness.services = true; return HttpResponse.json(op); }),
  );
  return { calls, drafts, operation, resource };
}
function show(role: "owner" | "editor" | "viewer" = "owner") {
  configureTransport({ mode: "local", teamId, csrf: "test-csrf" });
  const auth = { mode: "local", teamId, current: { user: { is_platform_admin: false }, memberships: [{ team_id: teamId, role }] }, teams: [], generation: 1, localAvailable: true } as unknown as AuthState;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const view = render(<QueryClientProvider client={qc}><AuthContext.Provider value={auth}><MemoryRouter><DurableSetup embedded /></MemoryRouter></AuthContext.Provider></QueryClientProvider>);
  return { ...view, qc };
}
afterEach(() => { cleanup(); configureTransport({ mode: "legacy" }); });

describe("durable team setup", () => {
  it("offers the text/hash/SQLite trial without global OCR discovery", async () => {
    const state = initial();
    fixtures(state);
    let discoveries = 0;
    server.use(http.get("/v1/ocr-services", () => { discoveries++; return HttpResponse.json({ services: [] }); }));
    show();
    expect(await screen.findByRole("radio", { name: /Try with built-in components/ })).toBeChecked();
    expect(screen.getByText(/Demonstrates retrieval mechanics/)).toBeInTheDocument();
    expect(discoveries).toBe(0);
  });

  it("saves choices, executes explicit steps and ends at an indexed-document search", async () => {
    const state = initial();
    const api = fixtures(state);
    const user = userEvent.setup();
    const { qc } = show();
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    await user.type(await screen.findByRole("textbox", { name: "Corpus" }), "trial-docs");
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));
    expect(await screen.findByRole("button", { name: "Add sample text document" })).toBeInTheDocument();
    expect(api.calls).toEqual(order);
    expect(state.readiness.ready).toBe(false);
    expect(screen.queryByText(/Setup complete:/)).not.toBeInTheDocument();
    expect(JSON.stringify(api.drafts)).not.toMatch(/api_key|Authorization|password/);
    expect(state.draft.draft.corpus_name).toBe("trial-docs");
    state.readiness.indexed_documents = 1;
    await qc.invalidateQueries({ queryKey: ["team-setup", teamId] });
    expect(await screen.findByRole("button", { name: "Search indexed documents" })).toBeEnabled();
  });

  it("reloads a partial operation and reuses completed resource references", async () => {
    const state = initial();
    state.draft.version = 2;
    state.draft.draft.corpus_name = "trial-docs";
    const api = fixtures(state);
    state.operation = api.operation();
    for (const step of state.operation.steps.slice(0, 4)) { step.state = "succeeded"; step.resource_id = api.resource(step.step); }
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByRole("button", { name: "Resume setup" }));
    expect(await screen.findByRole("button", { name: "Add sample text document" })).toBeInTheDocument();
    expect(api.calls).toEqual(["pipeline", "active", "check_services"]);
  });

  it("dismisses a personal preference without claiming team completion", async () => {
    const state = initial();
    fixtures(state);
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByRole("button", { name: "Set up later" }));
    await waitFor(() => expect(state.draft.draft.dismissed).toBe(true));
    expect(state.readiness.ready).toBe(false);
    expect(state.readiness.first_result_at).toBeNull();
  });

  it.each(["viewer", "editor"] as const)("keeps setup unavailable to %s members", async (role) => {
    let requests = 0;
    fixtures(initial());
    server.use(http.get("/v1/teams/:team/setup", () => { requests++; return HttpResponse.json(initial()); }));
    const { container } = show(role);
    expect(container).toBeEmptyDOMElement();
    expect(requests).toBe(0);
  });
});
