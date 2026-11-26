import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { DEV_ACCOUNT } from "../../mocks/scenarios";
import { renderApp } from "../../test/render";

describe("authentication", () => {
  it("rejects a wrong password and signs in with the right one", async () => {
    const user = userEvent.setup();
    renderApp("/", "auth");
    await screen.findByRole("heading", { name: "Sign in" });

    await user.type(screen.getByLabelText("Username"), DEV_ACCOUNT.username);
    await user.type(screen.getByLabelText("Password"), "wrong-password-here");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Wrong username or password.")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), DEV_ACCOUNT.password);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("link", { name: "support-docs" })).toBeInTheDocument();
  });

  it("creates the single account from first-run setup and signs straight in", async () => {
    const user = userEvent.setup();
    const { state } = renderApp("/setup", "empty", (s) => {
      s.account = null;
      s.session = false;
    });
    await screen.findByRole("heading", { name: "Create your account" });

    await user.type(screen.getByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "correct-horse-battery-staple");
    await user.type(screen.getByLabelText("Confirm password"), "correct-horse-battery-staple");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByRole("heading", { name: "Corpora" })).toBeInTheDocument();
    expect(state.account?.username).toBe("alice");
    expect(state.session).toBe(true);
  });

  it("sends a fresh visitor to first-run setup instead of the open app", async () => {
    renderApp("/", "empty", (s) => {
      s.account = null;
      s.session = false;
    });
    await screen.findByRole("heading", { name: "Create your account" });
  });

  it("signs out and returns to the login screen", async () => {
    const user = userEvent.setup();
    renderApp("/account", "auth", (s) => {
      s.session = true;
    });
    await screen.findByRole("heading", { name: "Account" });
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("API keys", () => {
  it("mints a key, shows its secret once, then revokes it", async () => {
    const user = userEvent.setup();
    const { state } = renderApp("/account", "auth", (s) => {
      s.session = true;
    });
    await screen.findByRole("heading", { name: "Account" });

    await user.click(screen.getByRole("button", { name: "New API key" }));
    await user.type(screen.getByLabelText("Name"), "claude-mcp");
    await user.click(screen.getByRole("button", { name: "Create key" }));

    await screen.findByRole("heading", { name: "API key created" });
    const secret = screen.getByText(/^mock-key-/).textContent;
    expect(secret).toBeTruthy();
    expect(state.apiKeys).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByText("claude-mcp")).toBeInTheDocument();
    expect(screen.queryByText(secret!)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Revoke" }));
    await user.click(await screen.findByRole("button", { name: "Revoke key" }));
    expect(state.apiKeys).toHaveLength(0);
    expect(await screen.findByText("No API keys yet")).toBeInTheDocument();
  });
});
