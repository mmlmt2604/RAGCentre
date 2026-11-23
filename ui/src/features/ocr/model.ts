import type { OcrService, OcrState, OcrStatus, OcrStep } from "../../api/types";

export type OcrTone = "ok" | "warn" | "danger" | "muted" | "accent";

export const OCR_STATE: Record<OcrState, { label: string; tone: OcrTone }> = {
  looking: { label: "Looking for shim", tone: "accent" },
  detected: { label: "Found, not tested", tone: "muted" },
  checking: { label: "Testing OCR", tone: "accent" },
  ready: { label: "OCR ready", tone: "ok" },
  failed: { label: "OCR not working", tone: "danger" },
  engine_down: { label: "OCR engine down", tone: "danger" },
  needs_key: { label: "Needs API key", tone: "warn" },
  changed: { label: "Shim changed", tone: "warn" },
  offline: { label: "Shim not running", tone: "danger" },
  no_ocr: { label: "No OCR", tone: "muted" },
};

const PROBLEMS: ReadonlySet<OcrState> = new Set(["failed", "engine_down", "needs_key", "changed", "offline"]);
export const isProblem = (state: OcrState) => PROBLEMS.has(state);

export function ocrLabel(s: Pick<OcrService, "state" | "busy">): string {
  return s.state === "ready" && s.busy ? "OCR ready, busy" : OCR_STATE[s.state].label;
}

/** Each step says what is true, so a failed one never reads like a success with a red icon. */
export const STEP_LABEL: Record<keyof OcrService["steps"], Record<OcrStep, string>> = {
  shim: { ok: "Shim running", failed: "Shim not running", running: "Looking for shim", unknown: "Shim not checked" },
  engine: { ok: "OCR engine running", failed: "OCR engine not working", running: "Starting OCR engine", unknown: "OCR engine not confirmed" },
  text: { ok: "Scanned text read", failed: "No scanned text read", running: "Reading scanned page", unknown: "Scanned text not checked" },
};

/** The status bar's one line for every shim together. */
export function ocrSummary(status: OcrStatus): { state: OcrState; label: string } {
  const added = status.services.filter((s) => s.provider_id && s.state !== "no_ocr");
  const found = status.services.filter((s) => !s.provider_id && s.state === "detected");
  if (added.length === 1) return { state: added[0]!.state, label: ocrLabel(added[0]!) };
  if (added.length) {
    const ready = added.filter((s) => s.state === "ready").length;
    if (ready === added.length) return { state: "ready", label: `OCR ready (${ready})` };
    const first = added.find((s) => isProblem(s.state)) ?? added.find((s) => s.state !== "ready")!;
    return { state: first.state, label: `${ready} of ${added.length} OCR ready` };
  }
  if (found.length) return { state: "detected", label: found.length === 1 ? "OCR shim found" : `${found.length} OCR shims found` };
  return status.scanning ? { state: "looking", label: "Looking for OCR" } : { state: "no_ocr", label: "No OCR shim" };
}
