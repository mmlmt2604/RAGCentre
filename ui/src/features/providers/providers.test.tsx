import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { db } from "../../mocks/db";
import { providerTypes } from "../../mocks/fixtures";
import { renderApp } from "../../test/render";

const typeOptionName = (label: string) => new RegExp(`^${label.replace(/[()]/g, "\\$&")}`);

describe("providers (UI-1)", () => {
  it("groups providers by kind with fingerprints, secrets and usage", async () => {
    renderApp("/providers");
    const embedders = (await screen.findByRole("heading", { name: /Embedders/ })).closest("section")!;
    const row = (await within(embedders).findByRole("link", { name: "bge-m3" })).closest("tr")!;
    expect(within(row).getByText("BAAI/bge-m3")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /bge-m3 fingerprint/ })).toBeInTheDocument();
    expect(within(row).getByText("not set")).toBeInTheDocument();
    const stores = screen.getByRole("heading", { name: /Vector stores/ }).closest("section")!;
    expect(within(stores).getByText("from $QDRANT_API_KEY")).toBeInTheDocument();
  });

  it.each(providerTypes.map((t) => [t.kind, t.type, t.label] as const))(
    "creates a %s/%s provider from a form generated from its config schema",
    async (kind, type, label) => {
      const user = userEvent.setup();
      const { router } = renderApp(`/providers/new?kind=${kind}`, "ready");
      await user.click(await screen.findByRole("radio", { name: typeOptionName(label) }));
      await user.type(screen.getByLabelText(/^Name/), `new-${type.replace(/_/g, "-")}`);
      const t = providerTypes.find((x) => x.kind === kind && x.type === type)!;
      const props = (t.config_schema.properties ?? {}) as Record<string, { type?: string }>;
      for (const key of (t.config_schema.required as string[] | undefined) ?? []) {
        const input = screen.getByLabelText(new RegExp(`${key}$`));
        await user.type(input, key.endsWith("url") ? "http://svc:9000" : props[key]?.type === "integer" ? "8" : "some-model");
      }
      for (const key of t.secret_keys) await user.click(within(screen.getByRole("radiogroup", { name: `Source for ${key}` })).getByRole("radio", { name: "Skip" }));
      await user.click(screen.getByRole("button", { name: /create provider|connect and create/i }));
      await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/providers\/[0-9a-f-]{36}$/));
      const created = db().providers.find((p) => p.name === `new-${type.replace(/_/g, "-")}`)!;
      expect(created).toMatchObject({ kind, type });
      if (t.fingerprint_fields.includes("service_version")) expect(created.config.service_version).toBeTruthy();
    },
  );

  it("shows field errors and keeps the user on the form", async () => {
    const user = userEvent.setup();
    renderApp("/providers/new?kind=embedder&type=openai_compatible");
    await user.type(await screen.findByLabelText(/^Name/), "Bad Name");
    await user.type(screen.getByLabelText(/batch_size/), "zero");
    await user.click(screen.getByRole("button", { name: "Create provider" }));
    expect(await screen.findByText(/Lowercase letters, digits and hyphens/)).toBeInTheDocument();
    expect(screen.getByText("Base URL is required.")).toBeInTheDocument();
    expect(screen.getByText("Enter a whole number.")).toBeInTheDocument();
  });

  it("locks fingerprint fields on the detail page and explains why", async () => {
    const id = (await import("../../mocks/hash")).uuidFrom("provider:bge-m3");
    renderApp(`/providers/${id}`);
    const perm = (await screen.findByRole("heading", { name: "Permanent settings" })).closest("section")!;
    expect(within(perm).getByText("BAAI/bge-m3")).toBeInTheDocument();
    expect(within(perm).queryByRole("textbox")).toBeNull();
    expect(within(perm).getByText(/part of its fingerprint and cannot change/)).toBeInTheDocument();
    expect(within(perm).getByRole("link", { name: "Create a new provider from this one" })).toHaveAttribute("href", `/providers/new?from=${id}`);
    const settings = screen.getByRole("heading", { name: "Settings" }).closest("form")!;
    expect(within(settings).getByLabelText(/base_url/)).toHaveValue("http://gpu-02:8080/v1");
    expect(within(settings).queryByLabelText(/^Model/)).toBeNull();
  });

  it("saves operational settings without touching the fingerprint", async () => {
    const user = userEvent.setup();
    const id = (await import("../../mocks/hash")).uuidFrom("provider:bge-m3");
    const { state } = renderApp(`/providers/${id}`);
    const before = state.providers.find((p) => p.id === id)!.fingerprint;
    const input = await screen.findByLabelText(/batch_size/);
    await user.clear(input);
    await user.type(input, "16");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(state.providers.find((p) => p.id === id)!.config.batch_size).toBe(16));
    expect(state.providers.find((p) => p.id === id)!.fingerprint).toBe(before);
  });

  it("sets a secret that is never displayed afterwards", async () => {
    const user = userEvent.setup();
    const id = (await import("../../mocks/hash")).uuidFrom("provider:bge-m3");
    renderApp(`/providers/${id}`);
    const secrets = (await screen.findByRole("heading", { name: "Secrets" })).closest("section")!;
    await user.click(within(secrets).getByRole("radio", { name: "Store value" }));
    await user.type(within(secrets).getByLabelText("Value for api_key"), "sk-very-secret");
    await user.click(within(secrets).getByRole("button", { name: "Save api_key" }));
    expect(await within(secrets).findByText("Set · stored encrypted")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("sk-very-secret");
    expect(within(secrets).queryByDisplayValue("sk-very-secret")).toBeNull();
  });

  it("disables stored secrets when the server cannot store them", async () => {
    const user = userEvent.setup();
    renderApp("/providers/new?kind=embedder&type=openai_compatible", "empty");
    const store = await screen.findByRole("radio", { name: "Store value" });
    expect(store).toBeDisabled();
    expect(screen.getByText(/this server has no/)).toBeInTheDocument();
    await user.click(store);
    expect(screen.getByLabelText("Environment variable for api_key")).toBeInTheDocument();
  });

  it("shows success, failure and version drift from the test action", async () => {
    const user = userEvent.setup();
    const { uuidFrom } = await import("../../mocks/hash");
    const { unmount } = renderApp(`/providers/${uuidFrom("provider:local-sqlite")}`, "degraded");
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connection works")).toBeInTheDocument();
    unmount();

    const second = renderApp(`/providers/${uuidFrom("provider:bge-m3")}`, "degraded");
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connection failed")).toBeInTheDocument();
    expect(screen.getByText(/503 Service Unavailable/)).toBeInTheDocument();
    second.unmount();

    renderApp(`/providers/${uuidFrom("provider:docling")}`, "degraded");
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Version changed")).toBeInTheDocument();
    const drift = screen.getByRole("alert");
    expect(within(drift).getByText("2.15.1")).toBeInTheDocument();
    expect(within(drift).getByText("2.14.0")).toBeInTheDocument();
  });

  it("runs a deep test on a vector store and shows its findings", async () => {
    const user = userEvent.setup();
    const { uuidFrom } = await import("../../mocks/hash");
    renderApp(`/providers/${uuidFrom("provider:local-sqlite")}`);
    await user.click(await screen.findByRole("button", { name: "Test with live data" }));
    expect(await screen.findByText("Connection works")).toBeInTheDocument();
    expect(await screen.findByText(/Nothing to fix\./)).toBeInTheDocument();
    expect(screen.getByText("rc_check_ab12cd34")).toBeInTheDocument();
  });

  it("offers no live-data test for providers that are not vector stores", async () => {
    const { uuidFrom } = await import("../../mocks/hash");
    renderApp(`/providers/${uuidFrom("provider:bge-m3")}`);
    await screen.findByRole("button", { name: "Test connection" });
    expect(screen.queryByRole("button", { name: "Test with live data" })).toBeNull();
  });

  it("explains why an in-use provider cannot be deleted", async () => {
    const user = userEvent.setup();
    const { uuidFrom } = await import("../../mocks/hash");
    renderApp(`/providers/${uuidFrom("provider:bge-small")}`);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/so the server will refuse until they are archived/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Delete provider" }));
    expect(await within(dialog).findByText(/is used by \d+ pipelines?\. Archive them first\./)).toBeInTheDocument();
  });

  it("marks built-in providers and offers no delete", async () => {
    const { uuidFrom } = await import("../../mocks/hash");
    renderApp(`/providers/${uuidFrom("provider:builtin-text")}`);
    expect(await screen.findByText("Built-in providers cannot be deleted.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("pre-fills a new provider from an existing one, permanent settings included", async () => {
    const { uuidFrom } = await import("../../mocks/hash");
    renderApp(`/providers/new?from=${uuidFrom("provider:bge-m3")}`);
    expect(await screen.findByRole("heading", { name: "New provider from bge-m3" })).toBeInTheDocument();
    expect(screen.getByLabelText(/^Name/)).toHaveValue("bge-m3-2");
    expect(screen.getByLabelText(/^Model/)).toHaveValue("BAAI/bge-m3");
  });
});
