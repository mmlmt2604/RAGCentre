import { Ban, CircleCheck, CircleDot, CircleX, KeyRound, LoaderCircle, Minus, TriangleAlert, Unplug, type LucideIcon } from "lucide-react";
import type { OcrService, OcrState } from "../../api/types";
import { Time } from "../../components/Time";
import { OCR_STATE, ocrLabel } from "./model";

const ICON: Record<OcrState, LucideIcon> = {
  looking: LoaderCircle,
  detected: CircleDot,
  checking: LoaderCircle,
  ready: CircleCheck,
  failed: CircleX,
  engine_down: Ban,
  needs_key: KeyRound,
  changed: TriangleAlert,
  offline: Unplug,
  no_ocr: Minus,
};

/** Icon + label + colour; never colour alone. */
export function OcrIndicator({ state, label }: { state: OcrState; label?: string }) {
  const Icon = ICON[state];
  const spin = state === "looking" || state === "checking";
  return (
    <span className={`rc-status rc-status--${OCR_STATE[state].tone}`}>
      <Icon size={14} strokeWidth={2} className={spin ? "rc-spin" : undefined} aria-hidden />
      <span>{label ?? OCR_STATE[state].label}</span>
    </span>
  );
}

/** A service's status with the age of its evidence: "ready" is only as good as its last test. */
export function OcrServiceStatus({ service }: { service: OcrService }) {
  return (
    <span className="ocr-cell">
      <OcrIndicator state={service.state} label={ocrLabel(service)} />
      {service.rechecking ? (
        <span className="ocr-cell__age">retesting</span>
      ) : service.state === "ready" && service.checked_at ? (
        <span className="ocr-cell__age">
          <Time iso={service.checked_at} compact />
        </span>
      ) : null}
    </span>
  );
}
