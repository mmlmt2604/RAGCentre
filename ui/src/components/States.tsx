import { CircleAlert, RotateCw, WifiOff, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import { nextStep } from "../lib/errors";
import { Button } from "./Button";

/** Explains what the thing is and offers the action that creates it. */
export function EmptyState({ icon: Icon, title, children, action }: { icon?: LucideIcon; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rc-empty">
      {Icon ? <Icon size={20} strokeWidth={1.5} aria-hidden className="rc-empty__icon" /> : null}
      <h2>{title}</h2>
      {children ? <div className="rc-empty__body">{children}</div> : null}
      {action ? <div className="rc-empty__action">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const api = error instanceof ApiError ? error : null;
  const Icon = api?.isNetwork ? WifiOff : CircleAlert;
  return (
    <div className="rc-error" role="alert">
      <Icon size={18} strokeWidth={1.75} aria-hidden className="rc-error__icon" />
      <div className="rc-error__text">
        <h2>{title ?? (api?.isNetwork ? "Can't reach RAGCentre" : api?.status === 401 ? "Access token required" : "Something went wrong")}</h2>
        <p>{api?.message ?? (error instanceof Error ? error.message : "An unexpected error occurred.")}</p>
        {nextStep(error) ? <p className="rc-error__next">{nextStep(error)}</p> : null}
        {api && !api.isNetwork ? <code className="rc-error__code">{api.code} · HTTP {api.status}</code> : null}
      </div>
      {onRetry ? (
        <Button icon={RotateCw} onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

export function Skeleton({ width, height = 12, className }: { width?: number | string; height?: number; className?: string }) {
  return <span className={`rc-skel ${className ?? ""}`} style={{ width, height }} aria-hidden />;
}

/** Skeleton rows for a table while it loads. */
export function SkeletonRows({ rows = 6, cols }: { rows?: number; cols: (number | string)[] }) {
  return (
    <>
      {Array.from({ length: rows }, (_, r) => (
        <tr key={r} aria-hidden>
          {cols.map((w, c) => (
            <td key={c}>
              <Skeleton width={w} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}
