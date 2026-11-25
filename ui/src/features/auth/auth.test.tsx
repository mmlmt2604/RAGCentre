import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { beforeEach, afterEach, describe, expect, it } from "vitest";
import { configureTransport, setToken } from "../../api/client";
import { uuidFrom } from "../../mocks/hash";
import { renderApp, server } from "../../test/render";
import type { CurrentUser, TeamMember, User } from "./types";

const corpusId = uuidFrom("corpus:support-docs");
const account: User = { id: "user-a", username: "alex", display_name: "Alex", status: "active", is_platform_admin: false, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
let current: CurrentUser | null;
let requests: Request[];
const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });
const member = (): CurrentUser => ({ user: { ...account }, csrf_token: "csrf-test", memberships: [
  { team_id: "team-a", team_name: "Team A", team_slug: "a", role: "editor" },
  { team_id: "team-b", team_name: "Team B", team_slug: "b", role: "viewer" },
] });
beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); current = member(); requests = [];
  server.use(
    http.get("/v1/auth/config", () => HttpResponse.json({ mode: "local", setup_required: false })),
    http.get("/v1/auth/me", () => current ? HttpResponse.json(current) : HttpResponse.json({ error: { code: "unauthorized", message: "Sign in required." } }, { status: 401 })),
  );
  server.events.on("request:start", ({ request }) => { requests.push(request); });
});
afterEach(() => { server.events.removeAllListeners(); configureTransport({ mode: "legacy" }); });

describe("local account UI", () => {
  it("finishes public bootstrap and session validation before protected requests", async () => {
    let release!: () => void;
    server.use(http.get("/v1/auth/me", async () => { await new Promise<void>((resolve) => { release = resolve; }); return HttpResponse.json(current); }));
    setToken("old-token"); renderApp("/");
    await waitFor(() => expect(release).toBeDefined());
    expect(requests.map((r) => new URL(r.url).pathname)).toEqual(["/v1/auth/config", "/v1/auth/me"]);
    expect(requests.every((r) => !r.headers.has("Authorization"))).toBe(true);
    release(); await screen.findByRole("link", { name: "support-docs" });
    const corpora = requests.find((r) => new URL(r.url).pathname === "/v1/corpora")!;
    expect(corpora.headers.get("X-RAGCentre-Team-ID")).toBe("team-a");
    expect(corpora.headers.has("Authorization")).toBe(false);
  });
  it("logs in with normalized username and leaves no-team accounts at access pending", async () => {
    current = null; const user = userEvent.setup();
    let body: unknown;
    server.use(http.post("/v1/auth/login", async ({ request }) => { body = await request.json(); current = { ...member(), memberships: [] }; return HttpResponse.json(current); }));
    renderApp("/");
    await user.type(await screen.findByLabelText("Username"), " ALEX ");
    await user.type(screen.getByLabelText("Password"), "a long password 123");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Team access pending" });
    expect(body).toEqual({ username: "alex", password: "a long password 123" });
    expect(requests.some((r) => new URL(r.url).pathname === "/v1/corpora")).toBe(false);
    expect(screen.getByRole("link", { name: "Account and password" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });
  it("keeps viewer pages readable without privileged dependencies or edit controls", async () => {
    current!.memberships[0]!.role = "viewer";
    const { router } = renderApp(`/corpora/${corpusId}/documents?edit=1`);
    await waitFor(() => expect(screen.getAllByRole("row").length).toBeGreaterThan(2));
    expect(screen.queryByRole("button", { name: "Upload files" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Sources" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Providers" })).not.toBeInTheDocument();
    expect(requests.some((r) => /\/(ocr-services|providers|provider-types)$/.test(new URL(r.url).pathname))).toBe(false);
    await act(() => router.navigate("/providers/new"));
    await screen.findByRole("heading", { name: "Access restricted" });
    expect(requests.some((r) => /\/(providers|provider-types)$/.test(new URL(r.url).pathname))).toBe(false);
  });
  it("switches editor to viewer with a fresh cache and rejects delayed prior-team data", async () => {
    server.use(http.get("/v1/corpora", async ({ request }) => {
      const team = request.headers.get("X-RAGCentre-Team-ID");
      if (team === "team-a") await delay(120);
      return page([{ id: `corpus-${team}`, name: team === "team-a" ? "Old team data" : "New team data", pipelines: [], document_count: 0, active_pipeline_id: null, created_at: "2026-01-01T00:00:00Z" }]);
    }));
    const user = userEvent.setup(); renderApp("/");
    await screen.findByRole("link", { name: "New corpus" });
    await user.selectOptions(screen.getByLabelText("Selected team"), "team-b");
    await screen.findByRole("link", { name: "New team data" });
    expect(screen.queryByRole("link", { name: "New corpus" })).not.toBeInTheDocument();
    await act(() => delay(150));
    expect(screen.queryByText("Old team data")).not.toBeInTheDocument();
  });
  it("revalidates role loss on focus and removes the old editing view", async () => {
    const { router } = renderApp("/corpora/new");
    await screen.findByRole("button", { name: "Create corpus" });
    current!.memberships[0]!.role = "viewer";
    fireEvent.focus(window);
    await screen.findByRole("link", { name: "support-docs" });
    expect(screen.queryByRole("button", { name: "Create corpus" })).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });
  it("consumes a fragment setup token, clears the address, and posts it only in JSON", async () => {
    let body: unknown;
    server.use(http.post("/v1/auth/setup", async ({ request }) => { body = await request.json(); return new HttpResponse(null, { status: 204 }); }));
    const user = userEvent.setup(); const { router } = renderApp("/setup#token=one-time-secret");
    await user.type(await screen.findByLabelText("New password"), "fifteen characters or more");
    expect(router.state.location.hash).toBe("");
    await user.type(screen.getByLabelText("Confirm password"), "fifteen characters or more");
    await user.click(screen.getByRole("button", { name: "Set password" }));
    await screen.findByRole("heading", { name: "Account ready" });
    expect(body).toEqual({ token: "one-time-secret", password: "fifteen characters or more" });
    expect(requests.every((r) => !r.url.includes("one-time-secret"))).toBe(true);
    expect(requests.some((r) => new URL(r.url).pathname === "/v1/auth/me")).toBe(false);
  });
  it("changes password with CSRF and returns to login after sessions are revoked", async () => {
    let csrf: string | null = null;
    server.use(http.post("/v1/auth/password", ({ request }) => { csrf = request.headers.get("X-RAGCentre-CSRF"); current = null; return new HttpResponse(null, { status: 204 }); }));
    const user = userEvent.setup(); renderApp("/account");
    await user.type(await screen.findByLabelText("Current password"), "existing password");
    await user.type(screen.getByLabelText("New password"), "replacement password 123");
    await user.click(screen.getByRole("button", { name: "Change password and sign out" }));
    await screen.findByRole("heading", { name: "Sign in to RAGCentre" });
    expect(csrf).toBe("csrf-test");
  });
  it("lets an owner manage exact usernames and explains final-owner failures", async () => {
    current!.memberships[0]!.role = "owner";
    let members: TeamMember[] = [{ user_id: account.id, username: account.username, display_name: account.display_name, status: "active", role: "owner" }];
    server.use(
      http.get("/v1/teams/team-a/members", () => page(members)),
      http.put("/v1/teams/team-a/members/:name", async ({ params, request }) => { const body = await request.json() as { role: TeamMember["role"] }; const added: TeamMember = { user_id: "new-member", username: String(params.name), display_name: "New member", status: "active", role: body.role }; members = [...members, added]; return HttpResponse.json(added); }),
      http.delete("/v1/teams/team-a/members/alex", () => HttpResponse.json({ error: { code: "last_owner", message: "Add another active owner before removing the final owner." } }, { status: 409 })),
    );
    const user = userEvent.setup(); renderApp("/members");
    await user.type(await screen.findByLabelText("Existing username"), " BLAIR ");
    await user.click(screen.getByRole("button", { name: "Add member" }));
    await screen.findByText("blair");
    const mutation = requests.find((r) => r.method === "PUT")!;
    expect(new URL(mutation.url).pathname).toBe("/v1/teams/team-a/members/blair");
    expect(mutation.headers.get("X-RAGCentre-CSRF")).toBe("csrf-test");
    await user.click(screen.getByRole("button", { name: "Remove alex" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove member" }));
    await within(dialog).findByText("Add another active owner before removing the final owner.");
    expect(requests.some((r) => new URL(r.url).pathname === "/v1/users")).toBe(false);
  });
  it.each(["viewer", "editor"] as const)("does not open privileged welcome setup for an empty %s team", async (role) => {
    current!.memberships[0]!.role = role;
    renderApp("/?setup", "empty");
    await screen.findByRole("heading", { name: "No corpora yet" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(requests.some((r) => /\/(ocr-services|providers|provider-types)$/.test(new URL(r.url).pathname))).toBe(false);
  });
  it("lets owners use guided setup without global OCR discovery", async () => {
    current!.memberships[0]!.role = "owner";
    renderApp("/", "empty");
    await screen.findByRole("heading", { name: "Set up your first pipeline" });
    expect(requests.some((r) => new URL(r.url).pathname === "/v1/providers")).toBe(true);
    expect(requests.some((r) => new URL(r.url).pathname === "/v1/ocr-services")).toBe(false);
  });
  it("does not sign out on an ordinary cross-team resource 404", async () => {
    server.use(http.get("/v1/documents/missing", () => HttpResponse.json({ error: { code: "not_found", message: "Document was not found." } }, { status: 404 })));
    renderApp(`/corpora/${corpusId}/documents/missing`);
    await screen.findByText("Document was not found.");
    expect(screen.getByLabelText("Selected team")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Sign in to RAGCentre" })).not.toBeInTheDocument();
    expect(requests.filter((r) => new URL(r.url).pathname === "/v1/auth/me")).toHaveLength(1);
  });
  it("logs out using CSRF and clears the protected view", async () => {
    let csrf: string | null = null;
    server.use(http.post("/v1/auth/logout", ({ request }) => { csrf = request.headers.get("X-RAGCentre-CSRF"); current = null; return new HttpResponse(null, { status: 204 }); }));
    const user = userEvent.setup(); renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    await screen.findByRole("heading", { name: "Sign in to RAGCentre" });
    expect(csrf).toBe("csrf-test");
    expect(screen.queryByRole("link", { name: "support-docs" })).not.toBeInTheDocument();
  });
  it("falls back to a stable denied screen when a 403 cannot change permissions", async () => {
    // A session request that keeps returning 403 while /auth/me is unchanged must not
    // revalidate forever: the gate settles on an explicit denied screen instead.
    server.use(http.get("/v1/corpora", () => HttpResponse.json({ error: { code: "forbidden", message: "Not allowed." } }, { status: 403 })));
    renderApp("/");
    await screen.findByRole("heading", { name: "Access restricted" });
    expect(screen.getByRole("button", { name: "Return to corpora" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    await act(() => delay(150));
    const meChecks = () => requests.filter((r) => new URL(r.url).pathname === "/v1/auth/me").length;
    expect(meChecks()).toBe(2); // bootstrap plus the single denied revalidation
    expect(requests.filter((r) => new URL(r.url).pathname === "/v1/corpora")).toHaveLength(1);
  });
  it("lets platform admins browse all teams and create a one-time account setup link", async () => {
    current!.user.is_platform_admin = true;
    current!.memberships = [];
    let payload: unknown;
    server.use(
      http.get("/v1/teams", () => page([{ id: "team-a", name: "Team A", slug: "a", created_at: account.created_at }, { id: "team-b", name: "Team B", slug: "b", created_at: account.created_at }])),
      http.get("/v1/users", () => page([account])),
      http.post("/v1/users", async ({ request }) => { payload = await request.json(); return HttpResponse.json({ user: { ...account, id: "new-user", username: "blair", status: "pending" }, setup_token: "setup-secret" }, { status: 201 }); }),
    );
    const user = userEvent.setup(); renderApp("/users");
    await user.type(await screen.findByLabelText("Username"), "blair");
    await user.type(screen.getByLabelText("Display name"), "Blair");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    const link = await screen.findByRole("textbox", { name: "One-time setup link" });
    expect(link).toHaveValue(`${window.location.origin}/setup#token=setup-secret`);
    expect(payload).toEqual({ username: "blair", display_name: "Blair" });
    expect(within(screen.getByLabelText("Selected team")).getAllByRole("option")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("textbox", { name: "One-time setup link" })).not.toBeInTheDocument();
  });
  it("includes an active initial owner when a platform admin creates a team", async () => {
    current!.user.is_platform_admin = true;
    let payload: unknown;
    server.use(
      http.get("/v1/teams", () => page([{ id: "team-a", name: "Team A", slug: "a", created_at: account.created_at }])),
      http.get("/v1/users", () => page([account, { ...account, id: "pending-user", username: "pending-user", status: "pending" }])),
      http.post("/v1/teams", async ({ request }) => { payload = await request.json(); return HttpResponse.json({ id: "new-team", name: "New team", slug: "new-team", created_at: account.created_at }, { status: 201 }); }),
    );
    const user = userEvent.setup(); renderApp("/teams");
    await user.type(await screen.findByLabelText("Team name"), "New team");
    await waitFor(() => expect(screen.getByLabelText("Initial owner")).toHaveTextContent("alex"));
    expect(screen.getByLabelText("Initial owner")).not.toHaveTextContent("pending-user");
    await user.selectOptions(screen.getByLabelText("Initial owner"), account.id);
    await user.click(screen.getByRole("button", { name: "Create team" }));
    await waitFor(() => expect(payload).toEqual({ name: "New team", owner_user_id: account.id }));
  });
});
