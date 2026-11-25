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
    const scope = within(dialog).getByLabelText(/^Scope/);
    await user.clear(scope);
    await user.click(scope);
    await user.paste('{"bucket":"contracts"}');
    const schedule = within(dialog).getByLabelText(/Schedule/);
    await user.clear(schedule);
    await user.type(schedule, "0");
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
    const scope = within(dialog).getByLabelText(/^Scope/);
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
    const schedule = screen.getByLabelText(/Schedule \(seconds\)/);
    await user.clear(schedule);
    await user.type(schedule, "7200");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(state.sources.find((s) => s.id === id)!.schedule_seconds).toBe(7200));
  });
});
