import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { configureTransport, setToken } from "../../api/client";
import { renderApp, server } from "../../test/render";
import type { CurrentUser, User } from "./types";

const account: User = { id: "blair-id", username: "blair", display_name: "Blair", status: "active", is_platform_admin: false, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
const member = (): CurrentUser => ({ user: account, csrf_token: "fresh-csrf", memberships: [{ team_id: "team-a", team_name: "Support", team_slug: "support", role: "viewer" }] });
const info = { username: "blair", display_name: "Blair", team_id: "team-a", team_name: "Support", role: "viewer", expires_at: "2027-01-01T00:00:00Z", mode: "activate" };
let current: CurrentUser | null;
let requests: Request[];
beforeEach(() => {
  current = null; requests = []; sessionStorage.clear(); localStorage.clear(); setToken("stale-automation-token");
  configureTransport({ mode: "legacy" });
  server.use(
    http.get("/v1/auth/config", () => HttpResponse.json({ mode: "local", setup_required: false })),
    http.get("/v1/auth/me", () => current ? HttpResponse.json(current) : HttpResponse.json({ error: { code: "unauthorized", message: "Sign in required." } }, { status: 401 })),
    http.post("/v1/auth/invitations/inspect", () => HttpResponse.json(info)),
  );
  server.events.on("request:start", ({ request }) => requests.push(request));
});
afterEach(() => { server.events.removeAllListeners(); configureTransport({ mode: "legacy" }); });

describe("one-time account links", () => {
  it("shows invitation context, clears the token fragment, and activates without an Authorization header", async () => {
    let body: unknown;
    server.use(http.post("/v1/auth/invitations/activate", async ({ request }) => {
      body = await request.json();
      current = member();
      return HttpResponse.json({ current: current, team_id: "team-a", already_member: false });
    }));
    const user = userEvent.setup(); const { router } = renderApp("/join#token=invite-secret");
    await screen.findByRole("heading", { name: "Join Support" });
    expect(screen.getByText("blair")).toBeInTheDocument();
    expect(screen.getByText("Viewer")).toBeInTheDocument();
    expect(router.state.location.hash).toBe("");
    await user.type(await screen.findByLabelText("New password"), "fifteen characters or more");
    await user.type(screen.getByLabelText("Confirm password"), "fifteen characters or more");
    await user.click(screen.getByRole("button", { name: "Create account and join team" }));
    await waitForInvitation();
    expect(body).toEqual({ token: "invite-secret", password: "fifteen characters or more" });
    expect(sessionStorage.getItem("ragcentre.selected-team")).toBe("team-a");
    expect(requests.every((r) => !r.url.includes("invite-secret"))).toBe(true);
    expect(requests.every((r) => !r.headers.has("Authorization"))).toBe(true);
  });
  it("asks an existing account to sign out instead of accepting as the wrong identity", async () => {
    current = { ...member(), user: { ...account, username: "alex", display_name: "Alex" } };
    let logout: string | null = null;
    server.use(http.post("/v1/auth/logout", ({ request }) => { logout = request.headers.get("X-RAGCentre-CSRF"); current = null; return new HttpResponse(null, { status: 204 }); }));
    const user = userEvent.setup(); renderApp("/join#token=invite-secret");
    await screen.findByText(/signed in as/i);
    expect(screen.getByText(/Sign out of alex/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept invitation" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign out of alex" }));
    await screen.findByLabelText("New password");
    expect(logout).toBe("fresh-csrf");
  });
  it("accepts a sign-in-mode invitation for the signed-in account with CSRF", async () => {
    server.use(http.post("/v1/auth/invitations/inspect", () => HttpResponse.json({ ...info, mode: "sign_in" })));
    let csrf: string | null = null;
    let body: unknown;
    server.use(http.post("/v1/auth/invitations/accept", async ({ request }) => {
      csrf = request.headers.get("X-RAGCentre-CSRF");
      body = await request.json();
      return HttpResponse.json({ current: member(), team_id: "team-a", already_member: false });
    }));
    current = member(); const user = userEvent.setup(); renderApp("/join#token=invite-secret");
    await screen.findByText(/signed in as/i);
    await user.click(screen.getByRole("button", { name: "Accept invitation" }));
    await waitForInvitation();
    expect(body).toEqual({ token: "invite-secret" });
    expect(csrf).toBe("fresh-csrf");
  });
  it("completes a recovery link without auto-sign-in and posts the token in JSON only", async () => {
    server.use(http.post("/v1/auth/recovery/inspect", () => HttpResponse.json({ username: "blair", display_name: "Blair", expires_at: "2027-01-01T00:00:00Z" })));
    let body: unknown;
    server.use(http.post("/v1/auth/recovery/complete", async ({ request }) => { body = await request.json(); return new HttpResponse(null, { status: 204 }); }));
    const user = userEvent.setup(); renderApp("/recover#token=recover-secret");
    await screen.findByRole("heading", { name: "Reset your password" });
    await user.type(await screen.findByLabelText("New password"), "fifteen characters or more");
    await user.type(screen.getByLabelText("Confirm password"), "fifteen characters or more");
    await user.click(screen.getByRole("button", { name: "Reset password" }));
    await screen.findByRole("heading", { name: "Password reset" });
    expect(body).toEqual({ token: "recover-secret", password: "fifteen characters or more" });
    expect(screen.getByRole("link", { name: "Go to sign in" })).toBeInTheDocument();
    expect(requests.every((r) => !r.url.includes("recover-secret"))).toBe(true);
  });
  it("explains how to obtain a fresh installation link and exposes no bootstrap form", async () => {
    server.use(http.post("/v1/auth/bootstrap/inspect", () => HttpResponse.json({ error: { code: "invalid_claim", message: "Installation link is invalid or expired." } }, { status: 401 })));
    renderApp("/initialize#token=expired-claim");
    await screen.findByText("ragcentre user setup-link");
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    expect(requests.every((r) => !r.url.includes("expired-claim"))).toBe(true);
  });
});

async function waitForInvitation() {
  await screen.findByLabelText("Selected team");
}
