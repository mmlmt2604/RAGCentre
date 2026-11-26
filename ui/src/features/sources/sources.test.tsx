import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp } from "../../test/render";

const SUPPORT = async () => (await import("../../mocks/hash")).uuidFrom("corpus:support-docs");
const SOURCE_ID = async () => (await import("../../mocks/hash")).uuidFrom("source:support-docs:s3-support");
const S3_ID = async () => (await import("../../mocks/hash")).uuidFrom("provider:s3-support");

describe("sources (UI-2)", () => {
  it("lists the corpus's sources with connector, scope, schedule and status", async () => {
    renderApp(`/corpora/${await SUPPORT()}/sources`);
    const row = (await screen.findByRole("link", { name: "Support manuals" })).closest("tr")!;
    expect(within(row).getByText("s3-support")).toBeInTheDocument();
    expect(within(row).getByText("bucket=support-docs, prefix=manuals/")).toBeInTheDocument();
    expect(within(row).getByText("Every 1 h")).toBeInTheDocument();
    expect(within(row).getByText("Idle")).toBeInTheDocument();
  });

  it("creates a source from a connector provider", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${await SUPPORT()}/sources`);
    await user.click(await screen.findByRole("button", { name: "New source" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Name/), "contracts-bucket");
    await user.selectOptions(within(dialog).getByLabelText(/Data connector/), await S3_ID());
    await user.type(await within(dialog).findByLabelText(/^Bucket/), "contracts");
    await user.click(within(dialog).getByRole("checkbox", { name: "I confirm this broad import scope" }));
    await user.selectOptions(within(dialog).getByLabelText(/^Schedule$/), "manual");
    await user.click(within(dialog).getByRole("button", { name: "Create source" }));
    await waitFor(() => expect(state.sources.some((s) => s.name === "contracts-bucket")).toBe(true));
    expect(state.sources.find((s) => s.name === "contracts-bucket")).toMatchObject({
      scope: { bucket: "contracts" },
      schedule_seconds: 0,
      status: "idle",
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("rejects a scope that is not a JSON object", async () => {
    const user = userEvent.setup();
    renderApp(`/corpora/${await SUPPORT()}/sources`);
    await user.click(await screen.findByRole("button", { name: "New source" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Name/), "bad-scope");
    await user.selectOptions(within(dialog).getByLabelText(/Data connector/), await S3_ID());
    await user.click(await within(dialog).findByRole("checkbox", { name: "Advanced scope JSON" }));
    const scope = within(dialog).getByLabelText(/^Scope JSON/);
    await user.clear(scope);
    await user.click(scope);
    await user.paste('["not", "an", "object"]');
    await user.click(within(dialog).getByRole("button", { name: "Create source" }));
    expect(await within(dialog).findByText(/Must be a JSON object/)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("syncs a source now", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${await SUPPORT()}/sources`);
    const id = await SOURCE_ID();
    const before = state.sources.find((s) => s.id === id)!.last_run_at;
    const row = (await screen.findByRole("link", { name: "Support manuals" })).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Sync Support manuals now" }));
    await waitFor(() => expect(state.sources.find((s) => s.id === id)!.last_run_at).not.toBe(before));
  });

  it("deletes a source after confirmation", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${await SUPPORT()}/sources`);
    const id = await SOURCE_ID();
    const row = (await screen.findByRole("link", { name: "Support manuals" })).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Delete Support manuals" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete source" }));
    await waitFor(() => expect(state.sources.some((s) => s.id === id)).toBe(false));
  });

  it("edits scope and schedule and lists items on the detail page", async () => {
    const user = userEvent.setup();
    const id = await SOURCE_ID();
    const { state } = renderApp(`/corpora/${await SUPPORT()}/sources/${id}`);
    await screen.findByRole("heading", { name: "Support manuals" });
    expect((await screen.findAllByText("Synced")).length).toBeGreaterThan(0);
    expect(screen.getByText("Skipped")).toBeInTheDocument();
    await user.selectOptions(await screen.findByLabelText(/^Schedule$/), "custom");
    const schedule = screen.getByLabelText(/Custom interval \(seconds\)/);
    await user.clear(schedule);
    await user.type(schedule, "7200");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(state.sources.find((s) => s.id === id)!.schedule_seconds).toBe(7200));
  });

  it("preserves older unsupported scope values when only changing the schedule", async () => {
    const user = userEvent.setup();
    const id = await SOURCE_ID();
    const legacyScope = { bucket: "docs", prefix: 123, future: { nested: [true, null] } };
    const { state } = renderApp(`/corpora/${await SUPPORT()}/sources/${id}`, "ready", (state) => {
      state.sources.find((s) => s.id === id)!.scope = legacyScope;
      state.sources.find((s) => s.id === id)!.schedule_seconds = 7201;
    });
    await screen.findByRole("heading", { name: "Support manuals" });
    expect(await screen.findByLabelText("Custom interval (seconds)")).toHaveValue("7201");
    const json = await screen.findByLabelText("Scope JSON");
    expect(JSON.parse((json as HTMLTextAreaElement).value)).toEqual(legacyScope);
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeEnabled());
    await user.selectOptions(screen.getByLabelText(/^Schedule$/), "daily");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(state.sources.find((s) => s.id === id)!.schedule_seconds).toBe(86400));
    expect(state.sources.find((s) => s.id === id)!.scope).toEqual(legacyScope);
  });

  it("blocks broad imports until explicitly confirmed", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${await SUPPORT()}/sources`);
    await user.click(await screen.findByRole("button", { name: "New source" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Name/), "broad-bucket");
    await user.selectOptions(within(dialog).getByLabelText(/Data connector/), await S3_ID());
    await user.type(await within(dialog).findByLabelText(/^Bucket/), "entire-bucket");
    await user.click(within(dialog).getByRole("button", { name: "Create source" }));
    expect(await within(dialog).findByText(/Confirm the broad import scope before creating/)).toBeInTheDocument();
    expect(state.sources.some((s) => s.name === "broad-bucket")).toBe(false);
    await user.click(within(dialog).getByRole("checkbox", { name: "I confirm this broad import scope" }));
    await user.click(within(dialog).getByRole("button", { name: "Create source" }));
    await waitFor(() => expect(state.sources.some((s) => s.name === "broad-bucket")).toBe(true));
  });

  it("creates a connector inline and returns with its source draft intact", async () => {
    const user = userEvent.setup();
    const { state, router } = renderApp(`/corpora/${await SUPPORT()}/sources`);
    await user.click(await screen.findByRole("button", { name: "New source" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Name/), "inline-source");
    await user.selectOptions(within(dialog).getByLabelText(/Data connector/), await S3_ID());
    await user.type(await within(dialog).findByLabelText(/^Bucket/), "documents");
    await user.type(within(dialog).getByLabelText(/^Prefix/), "manuals/");
    await user.selectOptions(within(dialog).getByLabelText(/^Schedule$/), "daily");
    await user.click(within(dialog).getByRole("button", { name: "Create a connector here" }));
    await user.selectOptions(within(dialog).getByLabelText("Connector type"), "s3");
    await user.type(await within(dialog).findByLabelText(/^Name/), "inline-s3");
    expect(within(dialog).getByLabelText("Value for access_key_id")).toBeInTheDocument();
    for (const key of ["access_key_id", "secret_access_key", "session_token"]) {
      await user.click(within(within(dialog).getByRole("radiogroup", { name: `Source for ${key}` })).getByRole("radio", { name: "Skip" }));
    }
    await user.click(within(dialog).getByRole("button", { name: "Create provider" }));
    await waitFor(() => expect(within(dialog).getByLabelText(/^Name/)).toHaveValue("inline-source"));
    const created = state.providers.find((p) => p.name === "inline-s3")!;
    expect(within(dialog).getByLabelText(/Data connector/)).toHaveValue(created.id);
    expect(within(dialog).getByLabelText(/^Bucket/)).toHaveValue("documents");
    expect(within(dialog).getByLabelText(/^Prefix/)).toHaveValue("manuals/");
    expect(within(dialog).getByLabelText(/^Schedule$/)).toHaveValue("daily");
    expect(router.state.location.pathname).toBe(`/corpora/${await SUPPORT()}/sources`);
    await user.click(within(dialog).getByRole("button", { name: "Verify saved connection" }));
    expect(await within(dialog).findByText("Connection verified")).toBeInTheDocument();
    expect(within(dialog).getByText(/This check does not verify access to a particular bucket/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create source" }));
    await waitFor(() => expect(state.sources.find((s) => s.name === "inline-source")).toMatchObject({ provider_id: created.id, scope: { bucket: "documents", prefix: "manuals/" }, schedule_seconds: 86400 }));
  });
});
