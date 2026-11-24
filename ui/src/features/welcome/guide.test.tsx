import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { keys } from "../../api/hooks";
import { renderApp } from "../../test/render";

async function startParse() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Start" }));
  return { user, dialog: within(screen.getByRole("dialog")) };
}

describe("first-run OCR guide", () => {
  beforeEach(() => localStorage.clear());

  it("defaults to the Docker guide and copies the command", async () => {
    renderApp("/", "no-shim");
    const { user, dialog } = await startParse();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    expect(dialog.getByRole("radio", { name: /Start an OCR shim/ })).toBeChecked();
    const command = "docker compose --profile docling up -d --build docling";
    expect(dialog.getByText(command)).toBeVisible();
    await user.click(dialog.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(command);
    expect(dialog.getByRole("button", { name: "Copied" })).toBeVisible();
    writeText.mockRestore();
  });

  it("selects the command when clipboard access is unavailable", async () => {
    renderApp("/", "no-shim");
    const { user, dialog } = await startParse();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Clipboard unavailable"));
    try {
      await user.click(dialog.getByRole("button", { name: "Copy" }));
      expect(dialog.getByText("Press ⌘C / Ctrl+C")).toBeVisible();
      expect(window.getSelection()?.toString()).toBe("docker compose --profile docling up -d --build docling");
    } finally {
      window.getSelection()?.removeAllRanges();
      writeText.mockRestore();
    }
  });

  it.each([
    ["Docling", "http://docling:8701"],
    ["Surya", "http://surya:8704"],
  ])("selects %s when it appears and leaves Continue focused", async (label, url) => {
    const { state, queryClient } = renderApp("/", "no-shim");
    const { user, dialog } = await startParse();
    if (label !== "Docling") await user.click(dialog.getByRole("radio", { name: new RegExp(label) }));
    expect(dialog.getByText(url)).toBeVisible();
    state.runningShims.push(url);
    await act(() => queryClient.invalidateQueries({ queryKey: keys.ocr }));
    expect(await dialog.findByText(`${label} found at ${url}. Selected as the parser.`)).toHaveAttribute("role", "status");
    expect(dialog.getByRole("radio", { name: /found running/ })).toBeChecked();
    const next = dialog.getByRole("button", { name: "Continue" });
    expect(next).toHaveFocus();
    await user.click(next);
    expect(dialog.getByRole("heading", { name: /embed/i })).toBeVisible();
  });

  it("defaults to uv with a loopback listener on the host", async () => {
    renderApp("/", "no-shim", (state) => { state.inContainer = false; });
    const { dialog } = await startParse();
    expect(dialog.getByRole("radio", { name: "uv (Python)" })).toBeChecked();
    expect(dialog.getByText(/--host 127\.0\.0\.1 --port 8701/)).toBeVisible();
  });

  it.each([
    ["http://gpu-box:8701", true],
    ["http://unreachable:8701", false],
  ])("checks the remote parser at %s during creation", async (url, works) => {
    const { state } = renderApp("/", "no-shim");
    const { user, dialog } = await startParse();
    await user.click(dialog.getByRole("radio", { name: "Another computer" }));
    const input = dialog.getByRole("textbox", { name: "Service URL" });
    const next = () => user.click(dialog.getByRole("button", { name: "Continue" }));
    await next();
    expect(input).toHaveAttribute("aria-invalid", "true");
    await user.clear(input);
    await user.type(input, url);
    await next();
    await next();
    await next();
    await user.type(dialog.getByRole("textbox", { name: "Corpus" }), "first-docs");
    await user.click(dialog.getByRole("button", { name: "Create pipeline" }));
    if (works) {
      expect(await dialog.findByRole("heading", { name: /ready for documents/ }, { timeout: 4000 })).toBeVisible();
      expect(state.providers).toContainEqual(expect.objectContaining({ kind: "parser", config: expect.objectContaining({ base_url: url }) }));
    } else {
      expect(await dialog.findByRole("button", { name: "Change parse choice" })).toBeVisible();
      expect(dialog.getByRole("button", { name: "Try again" })).toBeVisible();
    }
  });

  it("offers a prefilled service URL when detection is off", async () => {
    renderApp("/", "no-shim", (state) => { state.discoveryEnabled = false; });
    const { user, dialog } = await startParse();
    expect(dialog.getByRole("radio", { name: /Text files only/ })).toBeChecked();
    await user.click(dialog.getByRole("radio", { name: /Start an OCR shim/ }));
    expect(dialog.queryByRole("button", { name: "Look now" })).toBeNull();
    expect(dialog.getByRole("textbox", { name: "Service URL" })).toHaveValue("http://docling:8701");
    await user.click(dialog.getByRole("button", { name: "Continue" }));
    expect(dialog.getByRole("heading", { name: /embed/i })).toBeVisible();
  });

  it("offers reuse when the selected shim is already registered", async () => {
    renderApp("/", "empty", (state) => {
      state.providers.push({ ...state.providers[0]!, id: "docling", name: "docling", builtin: false, type: "http", config: { base_url: "http://docling:8701" } });
    });
    const { user, dialog } = await startParse();
    await user.click(dialog.getByRole("radio", { name: /Start an OCR shim/ }));
    expect(dialog.getByText(/docling is already registered/)).toBeVisible();
    await user.click(dialog.getByRole("radio", { name: "Another computer" }));
    expect(dialog.getByRole("textbox", { name: "Service URL" })).toHaveValue("http://<host>:8701");
    await user.click(dialog.getByRole("radio", { name: "Docker" }));
    await user.click(dialog.getByRole("button", { name: "Use docling" }));
    expect(dialog.getByRole("radio", { name: /^docling *registered/ })).toBeChecked();
  });

  it("keeps the user on Parse while a shim is pending", async () => {
    renderApp("/", "no-shim");
    const { user, dialog } = await startParse();
    await user.click(dialog.getByRole("button", { name: "Continue" }));
    expect(dialog.getByRole("alert")).toHaveTextContent("Start the shim, or choose Text files only.");
    expect(dialog.getByRole("heading", { name: "How should documents be read?" })).toBeVisible();
  });

  it("adds a build hint after two minutes", async () => {
    renderApp("/", "no-shim");
    const start = await screen.findByRole("button", { name: "Start" });
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    try {
      const user = userEvent.setup();
      await user.click(start);
      await act(async () => { vi.advanceTimersByTime(120_000); });
      expect(within(screen.getByRole("dialog")).getByText(/Building the image or installing/)).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });
});
