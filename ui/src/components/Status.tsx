import { Ban, CircleCheck, CircleDashed, CircleX, LoaderCircle, TriangleAlert } from "lucide-react";
import type { PipelineState, RunState } from "../api/types";

type AnyState = RunState | PipelineState;

const META: Record<AnyState, { label: string; tone: "ok" | "warn" | "danger" | "muted" | "accent"; Icon: typeof CircleCheck; spin?: boolean }> = {
  succeeded: { label: "Succeeded", tone: "ok", Icon: CircleCheck },
  ready: { label: "Ready", tone: "ok", Icon: CircleCheck },
  running: { label: "Running", tone: "accent", Icon: LoaderCircle, spin: true },
  building: { label: "Building", tone: "warn", Icon: LoaderCircle, spin: true },
  pending: { label: "Pending", tone: "muted", Icon: CircleDashed },
  blocked: { label: "Blocked", tone: "warn", Icon: Ban },
  failed: { label: "Failed", tone: "danger", Icon: CircleX },
  degraded: { label: "Degraded", tone: "danger", Icon: TriangleAlert },
};

/** Icon + label + colour; never colour alone. */
export function StatusIndicator({ state, label, compact = false }: { state: AnyState; label?: string; compact?: boolean }) {
  const m = META[state];
  return (
    <span className={`rc-status rc-status--${m.tone}`}>
      <m.Icon size={14} strokeWidth={2} className={m.spin ? "rc-spin" : undefined} aria-hidden />
      {compact ? <span className="sr-only">{label ?? m.label}</span> : <span>{label ?? m.label}</span>}
    </span>
  );
}

export function Badge({ children, tone = "muted" }: { children: React.ReactNode; tone?: "muted" | "accent" | "ok" | "warn" | "danger" }) {
  return <span className={`rc-badge rc-badge--${tone}`}>{children}</span>;
}

/** Coverage / progress meter with its value as text. */
export function Meter({ value, label, tone = "neutral" }: { value: number; label?: React.ReactNode; tone?: "neutral" | "warn" | "danger" }) {
  const pct = Math.max(0, Math.min(1, value));
  return (
    <span className={`rc-meter rc-meter--${tone}`}>
      {label ? <span className="rc-meter__label num">{label}</span> : null}
      <span className="rc-meter__track" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct * 100)} aria-label="Coverage">
        <span className="rc-meter__fill" style={{ width: `${pct * 100}%` }} />
      </span>
    </span>
  );
}
