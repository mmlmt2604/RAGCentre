// Mock OCR readiness: the states of the backend's ocr/monitor.py, derived from the mock's providers and faults.
import type { OcrService, OcrState, OcrStatus } from "../api/types";
import { ago, type MockState } from "./store";

const STEPS: Record<OcrState, OcrService["steps"]> = {
  looking: { shim: "running", engine: "unknown", text: "unknown" },
  detected: { shim: "ok", engine: "unknown", text: "unknown" },
  checking: { shim: "ok", engine: "running", text: "unknown" },
  ready: { shim: "ok", engine: "ok", text: "ok" },
  failed: { shim: "ok", engine: "failed", text: "unknown" },
  engine_down: { shim: "ok", engine: "failed", text: "unknown" },
  needs_key: { shim: "ok", engine: "unknown", text: "unknown" },
  changed: { shim: "ok", engine: "unknown", text: "unknown" },
  offline: { shim: "failed", engine: "unknown", text: "unknown" },
  no_ocr: { shim: "ok", engine: "unknown", text: "unknown" },
};

const MESSAGE: Record<OcrState, string> = {
  looking: "Looking for the shim.",
  detected: "Found a shim. Add it to test whether its OCR engine works.",
  checking: "Reading a scanned test page.",
  ready: "The OCR engine read a scanned test page.",
  failed: "The shim could not OCR a scanned page. The engine's models may be missing; check the shim's log.",
  engine_down: "The shim is running, but its OCR engine is not. Check the shim's log.",
  needs_key: "The shim rejected the test page. Set its API key on this provider.",
  changed: "Create a new provider from this one to use the shim that runs now.",
  offline: "The shim is not answering. Start it; RAGCentre tests it as soon as it is back.",
  no_ocr: "This parser accepts neither scanned PDFs nor PNG images.",
};

export function ocrStatus(state: MockState): OcrStatus {
  const parsers = state.providers.filter((p) => p.kind === "parser" && p.type === "http");
  const service = (over: Partial<OcrService> & Pick<OcrService, "id" | "base_url" | "state">): OcrService => ({
    provider_id: null,
    provider_name: null,
    suggested_name: null,
    name: "docling-shim",
    version: "2.14.0",
    steps: STEPS[over.state],
    busy: false,
    rechecking: false,
    message: MESSAGE[over.state],
    detail: null,
    checked_at: over.state === "ready" || over.state === "failed" ? ago(2) : null,
    check_seconds: over.state === "ready" || over.state === "failed" ? 3.7 : null,
    ...over,
  });
  const added = parsers.map((p) => {
    const fault = state.serviceFaults[p.name] ?? {};
    const status: OcrState = fault.ocr ?? (fault.down ? "offline" : fault.version ? "changed" : "ready");
    return service({
      id: p.id,
      provider_id: p.id,
      provider_name: p.name,
      base_url: String(p.config.base_url),
      name: String(p.config.service_name ?? "parser"),
      version: fault.version ?? String(p.config.service_version ?? ""),
      state: status,
      detail: fault.down ?? null,
    });
  });
  const found = state.runningShims
    .filter((url) => !parsers.some((p) => p.config.base_url === url))
    .map((url) => service({ id: `found-${url}`, base_url: url, state: "detected", suggested_name: state.providers.some((p) => p.name === "docling") ? "docling-2" : "docling" }));
  return { services: [...added, ...found], scanning: false, discovery_enabled: true };
}
