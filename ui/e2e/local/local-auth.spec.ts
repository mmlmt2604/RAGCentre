// Real local-account flows against an isolated server bootstrapped by e2e/local/server.mjs.
// No page.route(), MSW interception or database edits: every step drives the browser session.
import { expect, test, type Page } from "@playwright/test";
import { documents } from "../live/fixtures";

const port = Number(process.env.RAGCENTRE_E2E_LOCAL_PORT ?? 8712);
const baseURL = `http://127.0.0.1:${port}`;
// Kept in sync with the defaults in e2e/local/server.mjs; synthetic test credentials only.
const adminUsername = "e2e-admin";
const adminPassword = "local-admin-password-for-e2e";
const memberPassword = "member-password-for-e2e";
const memberNewPassword = "member-new-password-for-e2e";
const DEFAULT_TEAM_ID = "00000000-0000-0000-0000-000000000001";

/** Collect every request that leaks an Authorization header; local mode must never send one. */
function watchAuthorizationLeaks(page: Page): string[] {
  const leaked: string[] = [];
  page.on("request", (request) => {
    const value = request.headers()["authorization"];
    if (value) leaked.push(`${request.method()} ${request.url()}`);
  });
  return leaked;
}

async function signIn(page: Page, username: string, password: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to RAGCentre" })).toBeVisible();
  await page.getByRole("textbox", { name: "Username" }).fill(username);
  await page.getByRole("textbox", { name: "Password" }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

/** Dismiss the first-run wizard modal. It appears per context generation on empty teams. */
async function dismissWizard(page: Page): Promise<void> {
  const dialog = page.getByRole("dialog", { name: "Set up your first pipeline" });
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Set up on my own" }).click();
  await expect(dialog).toHaveCount(0);
}

/** Wait for the shell, then dismiss the first-run wizard modal when the server is still empty. */
async function readyShell(page: Page): Promise<void> {
  await expect(page.getByLabel("Selected team")).toBeVisible();
  const corpora = await (await page.context().request.get("/v1/corpora", {
    headers: { "X-RAGCentre-Team-ID": DEFAULT_TEAM_ID },
  })).json() as { items: unknown[] };
  if (corpora.items.length === 0) await dismissWizard(page);
}

async function createCorpus(page: Page, name: string): Promise<{ id: string }> {
  await page.getByRole("link", { name: "New corpus" }).first().click();
  await page.getByRole("textbox", { name: "Name *", exact: true }).fill(name);
  const response = page.waitForResponse((r) => r.url().endsWith("/v1/corpora") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Create corpus" }).click();
  const result = await response;
  expect(result.ok(), await result.text()).toBeTruthy();
  return result.json() as Promise<{ id: string }>;
}

async function createPendingUser(page: Page, username: string, displayName: string): Promise<string> {
  await page.goto("/users");
  await page.getByRole("textbox", { name: "Username" }).fill(username);
  await page.getByRole("textbox", { name: "Display name", exact: true }).fill(displayName);
  const response = page.waitForResponse((r) => r.url().endsWith("/v1/users") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Create account" }).click();
  const result = await response;
  expect(result.ok(), await result.text()).toBeTruthy();
  const link = page.getByRole("textbox", { name: "One-time setup link" });
  await expect(link).toBeVisible();
  const value = await link.inputValue();
  expect(value).toContain("/setup#token=");
  await page.getByRole("button", { name: "Dismiss" }).click();
  return value;
}

async function addMember(page: Page, username: string, role: "viewer" | "editor" | "owner"): Promise<void> {
  await page.goto("/members");
  await page.getByRole("textbox", { name: "Existing username" }).fill(username);
  await page.getByLabel("Role", { exact: true }).selectOption(role);
  const response = page.waitForResponse((r) => r.url().endsWith(`/members/${username}`) && r.request().method() === "PUT");
  await page.getByRole("button", { name: "Add member" }).click();
  const result = await response;
  expect(result.ok(), await result.text()).toBeTruthy();
}

async function createTeam(page: Page, name: string): Promise<void> {
  await page.goto("/teams");
  await page.getByRole("textbox", { name: "Team name" }).fill(name);
  // init-admin sets the display name to the username, so the owner option label is fixed.
  await page.getByLabel("Initial owner").selectOption({ label: `${adminUsername} · ${adminUsername}` });
  const create = page.getByRole("button", { name: "Create team" });
  await expect(create).toBeEnabled();
  const response = page.waitForResponse((r) => r.url().endsWith("/v1/teams") && r.request().method() === "POST");
  await create.click();
  const result = await response;
  expect(result.ok(), await result.text()).toBeTruthy();
}

async function setupAccount(page: Page, link: string, password: string): Promise<void> {
  await page.goto(link);
  await expect(page).toHaveURL(/\/setup$/); // the one-time fragment is stripped immediately
  await page.getByRole("textbox", { name: "New password" }).fill(password);
  await page.getByRole("textbox", { name: "Confirm password" }).fill(password);
  await page.getByRole("button", { name: "Set password" }).click();
  await expect(page.getByRole("heading", { name: "Account ready" })).toBeVisible();
  await page.getByRole("button", { name: "Go to sign in" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to RAGCentre" })).toBeVisible();
}

async function changePassword(page: Page, current: string, next: string): Promise<void> {
  await page.getByRole("textbox", { name: "Current password" }).fill(current);
  await page.getByRole("textbox", { name: "New password" }).fill(next);
  await page.getByRole("button", { name: "Change password and sign out" }).click();
}

test("1. admin signs in over session cookies and mutates with CSRF and team headers", async ({ browser }, info) => {
  const context = await browser.newContext({ baseURL });
  // A stale legacy bearer must be ignored in local mode.
  await context.addInitScript(() => sessionStorage.setItem("ragcentre.token", "stale-legacy-bearer"));
  const page = await context.newPage();
  const leaked = watchAuthorizationLeaks(page);

  await signIn(page, adminUsername, adminPassword);
  await readyShell(page);

  const cookies = await context.cookies(baseURL);
  const session = cookies.find((c) => c.name === "ragcentre_session_dev");
  expect(session).toBeTruthy();
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Lax");
  expect(session?.secure).toBe(false);

  const config = await (await context.request.get("/v1/auth/config")).json();
  expect(config).toEqual({ mode: "local", setup_required: false });

  const name = `admin-${info.retry}-corpus`;
  const mutation = page.waitForRequest((r) => r.url().endsWith("/v1/corpora") && r.method() === "POST");
  await createCorpus(page, name);
  const headers = (await mutation).headers();
  expect(headers["x-ragcentre-csrf"]).toBeTruthy();
  expect(headers["x-ragcentre-team-id"]).toBe(DEFAULT_TEAM_ID);
  // Chromium does not expose the Origin header through CDP; the successful mutation above
  // already proves the origin check accepts the browser, and test 2 proves the rejection side.
  await page.goto("/");
  await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
  expect(leaked).toEqual([]);
  await context.close();
});

test("2. pending member activates a one-time link, gets per-team roles and team switching", async ({ browser }, info) => {
  test.setTimeout(180_000);
  const member = `e2e-member-${info.retry}`;
  const defaultCorpus = `default-corpus-${info.retry}`;
  const teamB = `Team B ${info.retry}`;
  const teamBCorpus = `team-b-corpus-${info.retry}`;

  // Admin: create the pending account and read its one-time setup link.
  const adminContext = await browser.newContext({ baseURL });
  const adminPage = await adminContext.newPage();
  await signIn(adminPage, adminUsername, adminPassword);
  await readyShell(adminPage);
  const setupLink = await createPendingUser(adminPage, member, "E2E Member");

  // Member: activate the link, sign in, and land on the no-team pending state.
  const memberContext = await browser.newContext({ baseURL });
  await memberContext.addInitScript(() => sessionStorage.setItem("ragcentre.token", "stale-legacy-bearer"));
  const memberPage = await memberContext.newPage();
  const leaked = watchAuthorizationLeaks(memberPage);
  await setupAccount(memberPage, setupLink, memberPassword);
  await signIn(memberPage, member, memberPassword);
  await expect(memberPage.getByRole("heading", { name: "Team access pending" })).toBeVisible();
  await expect(memberPage.getByLabel("Selected team")).toHaveCount(0);

  // Admin: editor in Default, then a second team owned by the admin, with the member as viewer.
  await addMember(adminPage, member, "editor");
  await createTeam(adminPage, teamB);
  await adminPage.getByLabel("Selected team").selectOption({ label: `${teamB} · admin` });
  await expect(adminPage.getByLabel("Selected team").locator("option:checked")).toHaveText(`${teamB} · admin`);
  // A fresh team context has no corpora, so the wizard reopens for this generation.
  await dismissWizard(adminPage);
  const teamBCorpusId = (await createCorpus(adminPage, teamBCorpus)).id;
  await addMember(adminPage, member, "viewer");
  await adminPage.getByLabel("Selected team").selectOption({ label: "Default · admin" });
  const defaultCorpusId = (await createCorpus(adminPage, defaultCorpus)).id;

  // Member: check access again, now an editor in Default.
  await memberPage.getByRole("button", { name: "Check access" }).click();
  await expect(memberPage.getByLabel("Selected team").locator("option:checked")).toHaveText("Default · editor");
  await expect(memberPage.getByRole("link", { name: "New corpus" }).first()).toBeVisible();
  await expect(memberPage.getByRole("link", { name: "Providers", exact: true })).toBeVisible();
  await expect(memberPage.getByRole("link", { name: "Members", exact: true })).toHaveCount(0);
  await expect(memberPage.getByRole("link", { name: "Team tokens", exact: true })).toHaveCount(0);
  await expect(memberPage.getByRole("link", { name: "Users", exact: true })).toHaveCount(0);
  await expect(memberPage.getByRole("link", { name: "Teams", exact: true })).toHaveCount(0);
  await expect(memberPage.getByRole("link", { name: "Admin", exact: true })).toHaveCount(0);

  // Editor mutation with the browser session: upload into the Default corpus.
  await memberPage.goto(`/corpora/${defaultCorpusId}/documents`);
  await expect(memberPage.getByRole("button", { name: "Upload files" }).first()).toBeVisible();
  await memberPage.getByTestId("upload-input").setInputFiles(documents);
  await memberPage.getByRole("button", { name: /Upload 2 files/ }).click();
  await expect(memberPage.getByRole("button", { name: "Done", exact: true })).toBeVisible({ timeout: 30_000 });
  await memberPage.getByRole("button", { name: "Done", exact: true }).click();
  for (const doc of documents) await expect(memberPage.getByRole("link", { name: doc.name, exact: true })).toBeVisible();

  // Owner-only surfaces stay denied for an editor.
  await memberPage.goto(`/corpora/${defaultCorpusId}/sources`);
  await expect(memberPage.getByRole("heading", { name: "Access restricted" })).toBeVisible();

  // Switch to Team B as a viewer: a separate context with its own visible content.
  await memberPage.getByLabel("Selected team").selectOption({ label: `${teamB} · viewer` });
  await expect(memberPage).toHaveURL(/\/$/);
  await expect(memberPage.getByText(teamBCorpus, { exact: true }).first()).toBeVisible();
  await expect(memberPage.getByText(defaultCorpus, { exact: true })).toHaveCount(0);
  await expect(memberPage.getByRole("link", { name: "New corpus" })).toHaveCount(0);
  await expect(memberPage.getByRole("link", { name: "Providers", exact: true })).toHaveCount(0);

  await memberPage.goto(`/corpora/${teamBCorpusId}/documents`);
  await expect(memberPage.getByRole("button", { name: "Upload files" })).toHaveCount(0);
  await expect(memberPage.getByText("Drop files anywhere on this page to upload.")).toHaveCount(0);
  await memberPage.goto("/providers");
  await expect(memberPage.getByRole("heading", { name: "Access restricted" })).toBeVisible();
  await memberPage.goto("/members");
  await expect(memberPage.getByRole("heading", { name: "Access restricted" })).toBeVisible();

  // An ordinary cross-team 404 does not sign the session out.
  await memberPage.goto(`/corpora/${defaultCorpusId}/documents`);
  await expect(memberPage.getByLabel("Selected team")).toBeVisible();
  await expect(memberPage.getByText(defaultCorpus, { exact: true })).toHaveCount(0);

  // The session cookie alone is not enough for a mutation: CSRF and the browser Origin are required.
  const probe = await memberContext.request.post("/v1/corpora", {
    headers: { Origin: baseURL, "X-RAGCentre-Team-ID": DEFAULT_TEAM_ID },
    data: { name: "csrf-probe" },
  });
  expect(probe.status()).toBe(403);
  expect(await probe.text()).toContain("CSRF");
  const noOrigin = await memberContext.request.post("/v1/corpora", {
    headers: { "X-RAGCentre-Team-ID": DEFAULT_TEAM_ID },
    data: { name: "origin-probe" },
  });
  expect(noOrigin.status()).toBe(403);
  expect(await noOrigin.text()).toContain("origin");
  expect(leaked).toEqual([]);

  await adminContext.close();
  await memberContext.close();
});

test("3. password change signs out every tab, rotates the credential and resyncs tabs", async ({ browser }, info) => {
  test.setTimeout(120_000);
  const member = `e2e-member-pw-${info.retry}`;

  const adminContext = await browser.newContext({ baseURL });
  const adminPage = await adminContext.newPage();
  await signIn(adminPage, adminUsername, adminPassword);
  await readyShell(adminPage);
  const setupLink = await createPendingUser(adminPage, member, "Password Member");

  const memberContext = await browser.newContext({ baseURL });
  const page1 = await memberContext.newPage();
  await setupAccount(page1, setupLink, memberPassword);
  await signIn(page1, member, memberPassword);
  await expect(page1.getByRole("heading", { name: "Team access pending" })).toBeVisible();
  await addMember(adminPage, member, "viewer");
  await page1.getByRole("button", { name: "Check access" }).click();
  await expect(page1.getByLabel("Selected team")).toBeVisible();

  // Rotate the password in a second tab of the same browser context.
  const page2 = await memberContext.newPage();
  await page2.goto("/account");
  await expect(page2.getByRole("heading", { name: "Account" })).toBeVisible();
  await changePassword(page2, memberPassword, memberNewPassword);
  await expect(page2.getByRole("heading", { name: "Sign in to RAGCentre" })).toBeVisible();
  // The other tab follows without interaction (BroadcastChannel invalidation).
  await expect(page1.getByRole("heading", { name: "Sign in to RAGCentre" })).toBeVisible({ timeout: 20_000 });

  // The new password signs in; the shared tab follows the new session.
  await signIn(page1, member, memberNewPassword);
  await expect(page1.getByLabel("Selected team")).toBeVisible();
  await expect(page2.getByLabel("Selected team")).toBeVisible({ timeout: 20_000 });

  // The old password no longer works, in an unrelated context.
  const stale = await browser.newContext({ baseURL });
  const stalePage = await stale.newPage();
  await signIn(stalePage, member, memberPassword);
  await expect(stalePage.getByText("Sign-in failed")).toBeVisible();
  await expect(page1.getByLabel("Selected team")).toBeVisible();

  await adminContext.close();
  await memberContext.close();
  await stale.close();
});
