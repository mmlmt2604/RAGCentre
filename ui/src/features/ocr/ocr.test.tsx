import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { keys } from "../../api/hooks";
import type { OcrService, OcrState, OcrStatus } from "../../api/types";
import { db } from "../../mocks/db";
import { renderApp } from "../../test/render";
import { ocrSummary, OCR_STATE } from "./model";

const parsers = async () => (await screen.findByRole("heading", { name: /Parsers/ })).closest("section")!;
const bar = () => screen.getByRole("contentinfo");

describe("OCR readiness", () => {
  it("adds a found shim in one click and shows it ready", async () => {
    const user = userEvent.setup();
    renderApp("/providers", "empty");
    expect(await within(await screen.findByRole("contentinfo")).findByText("OCR shim found")).toBeInTheDocument();
    const found = await screen.findByRole("region", { name: "OCR shims found" });
    expect(within(found).getByText(/http:\/\/docling:8701/)).toBeInTheDocument();
    await user.click(within(found).getByRole("button", { name: "Add and test OCR" }));

    const link = await within(await parsers()).findByRole("link", { name: "docling" });
    expect(await within(link.closest("tr")!).findByText("OCR ready")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "OCR shims found" })).not.toBeInTheDocument();
    expect(db().providers.find((p) => p.name === "docling")).toMatchObject({ kind: "parser", type: "http", config: { base_url: "http://docling:8701" } });
    await waitFor(() => expect(link).toHaveFocus());
  });

  it("explains what to do when nothing is running", async () => {
    renderApp("/providers", "empty", (s) => void (s.runningShims = []));
    expect(await screen.findByText(/No OCR shim is running on this machine/)).toBeInTheDocument();
    expect(await within(bar()).findByText("No OCR shim")).toBeInTheDocument();
  });

  it.each<OcrState>(["failed", "engine_down", "needs_key", "changed", "offline"])("shows %s with its next step", async (state) => {
    renderApp("/providers", "ready", (s) => void (s.serviceFaults.docling = { ocr: state }));
    const row = (await within(await parsers()).findByRole("link", { name: "docling" })).closest("tr")!;
    expect(await within(row).findByText(OCR_STATE[state].label)).toBeInTheDocument();
    const why = row.nextElementSibling as HTMLElement;
    expect(why).toHaveClass("ocr-why");
    expect(within(why).getByRole("link")).toHaveAttribute("href", expect.stringMatching(/^\/providers\//));
    expect(within(bar()).getByText(OCR_STATE[state].label)).toBeInTheDocument();
  });

  it("shows the three facts on the provider page", async () => {
    const { state } = renderApp("/providers", "ready", (s) => void (s.serviceFaults.docling = { ocr: "engine_down" }));
    const user = userEvent.setup();
    await user.click(await within(await parsers()).findByRole("link", { name: "docling" }));
    const panel = (await screen.findByRole("heading", { name: "OCR" })).closest("section")!;
    expect(within(panel).getByText("Shim running")).toBeInTheDocument();
    expect(within(panel).getByText("OCR engine not working")).toBeInTheDocument();
    expect(within(panel).getByText("Scanned text not checked")).toBeInTheDocument();

    state.serviceFaults.docling = {};
    await user.click(within(panel).getByRole("button", { name: "Test OCR" }));
    expect(await within(panel).findByText("OCR engine running")).toBeInTheDocument();
    expect(within(panel).getByText("Scanned text read")).toBeInTheDocument();
  });

  it("announces a change, but not the state the page opened with", async () => {
    const { state, queryClient } = renderApp("/providers");
    await within(await screen.findByRole("contentinfo")).findByText("OCR ready");
    const notices = screen.getByRole("region", { name: "Notifications" });
    expect(notices).not.toHaveTextContent("OCR ready");

    state.serviceFaults.docling = { ocr: "engine_down" };
    await queryClient.invalidateQueries({ queryKey: keys.ocr });
    expect(await within(notices).findByText("docling: OCR engine down")).toBeInTheDocument();

    state.serviceFaults.docling = {};
    await queryClient.invalidateQueries({ queryKey: keys.ocr });
    expect(await within(notices).findByText("docling: OCR ready")).toBeInTheDocument();
  });

  it("announces a shim that starts while the page is open", async () => {
    const { state, queryClient } = renderApp("/providers");
    await within(await screen.findByRole("contentinfo")).findByText("OCR ready");
    state.runningShims = ["http://surya:8704"];
    await queryClient.invalidateQueries({ queryKey: keys.ocr });
    expect(await within(screen.getByRole("region", { name: "Notifications" })).findByText("Found surya-shim")).toBeInTheDocument();
  });

  it("offers found shims when adding a parser", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/providers/new?kind=parser", "empty");
    await user.click(await screen.findByRole("button", { name: "Add and test OCR" }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/providers\/[0-9a-f-]{36}$/));
  });

  it("carries the status into the pipeline's parser picker", async () => {
    const { state, router } = renderApp("/providers", "ready", (s) => void (s.serviceFaults.docling = { ocr: "failed" }));
    await router.navigate(`/corpora/${state.corpora[0]!.id}/pipelines/new?from=${state.pipelines[0]!.id}`);
    const user = userEvent.setup();
    const parse = await screen.findByRole("button", { name: /^parse/i, pressed: false });
    await user.click(parse);
    expect((await screen.findAllByRole("option", { name: "docling (http, OCR not working)" })).length).toBeGreaterThan(0);
  });
});

describe("status bar summary", () => {
  const service = (state: OcrState, added = true) => ({ state, busy: false, provider_id: added ? state : null }) as OcrService;
  const summary = (...services: OcrService[]) => ocrSummary({ services, scanning: false, discovery_enabled: true, in_container: true } satisfies OcrStatus);

  it("never calls a mix ready", () => {
    expect(summary(service("ready"), service("offline"))).toEqual({ state: "offline", label: "1 of 2 OCR ready" });
    expect(summary(service("ready"), service("checking")).state).toBe("checking");
  });
  it("counts only what reads scans", () => {
    expect(summary(service("ready"), service("no_ocr"))).toEqual({ state: "ready", label: "OCR ready" });
  });
  it("does not call a found shim ready", () => {
    expect(summary(service("detected", false))).toEqual({ state: "detected", label: "OCR shim found" });
  });
});
