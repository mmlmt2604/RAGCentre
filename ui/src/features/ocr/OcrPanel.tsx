import { Circle, CircleCheck, CircleX, KeyRound, LoaderCircle, ScanText, type LucideIcon } from "lucide-react";
import { useOcr, useOcrCheck } from "../../api/hooks";
import type { OcrStep, Provider } from "../../api/types";
import { Button } from "../../components/Button";
import { ErrorState, Skeleton } from "../../components/States";
import { Time } from "../../components/Time";
import { duration } from "../../lib/format";
import { OcrServiceStatus } from "./OcrIndicator";
import { STEP_LABEL, type OcrTone } from "./model";

const STEP: Record<OcrStep, { Icon: LucideIcon; tone: OcrTone }> = {
  ok: { Icon: CircleCheck, tone: "ok" },
  failed: { Icon: CircleX, tone: "danger" },
  running: { Icon: LoaderCircle, tone: "accent" },
  unknown: { Icon: Circle, tone: "muted" },
};

/** The three facts behind "OCR ready", and what to do about the one that is not true. */
export function OcrPanel({ provider }: { provider: Provider }) {
  const ocr = useOcr();
  const check = useOcrCheck(provider.id);
  const service = ocr.data?.services.find((s) => s.provider_id === provider.id);

  if (ocr.isError && !service) return <ErrorState error={ocr.error} title="OCR status is unavailable" onRetry={() => void ocr.refetch()} />;
  if (!service) return <Skeleton width="100%" height={112} />;

  const testing = service.state === "checking" || service.rechecking;
  return (
    <section className="ocr-panel" aria-labelledby="ocr-title">
      <header className="ocr-panel__head">
        <h2 id="ocr-title">OCR</h2>
        <span role="status">
          <OcrServiceStatus service={service} />
        </span>
        <span className="page-head__spacer" />
        {service.state === "needs_key" ? (
          <a className="rc-btn rc-btn--secondary rc-btn--sm" href="#secrets">
            <KeyRound size={14} strokeWidth={1.75} aria-hidden />
            Set API key
          </a>
        ) : null}
        {service.state === "changed" ? null : (
          <Button size="sm" icon={ScanText} loading={check.isPending || testing} onClick={() => check.mutate()}>
            Test OCR
          </Button>
        )}
      </header>
      <ol className="ocr-steps">
        {(["shim", "engine", "text"] as const).map((key) => {
          const step = service.steps[key];
          const { Icon, tone } = STEP[step];
          return (
            <li key={key} className={`rc-status rc-status--${tone}`}>
              <Icon size={14} strokeWidth={2} className={step === "running" ? "rc-spin" : undefined} aria-hidden />
              {STEP_LABEL[key][step]}
            </li>
          );
        })}
      </ol>
      <p className="ocr-panel__msg">
        {service.message}
        {service.checked_at && !testing ? (
          <span className="ocr-panel__when">
            {" "}
            Tested <Time iso={service.checked_at} />
            {service.check_seconds === null ? null : (
              <>
                {" "}
                in <span className="num mono">{duration(service.check_seconds * 1000)}</span>
              </>
            )}
            , with the shim's default options.
          </span>
        ) : null}
      </p>
      {service.detail ? <code className="ocr-panel__detail">{service.detail}</code> : null}
      {check.error ? (
        <p className="rc-field__error" role="alert">
          {check.error.message}
        </p>
      ) : null}
    </section>
  );
}
