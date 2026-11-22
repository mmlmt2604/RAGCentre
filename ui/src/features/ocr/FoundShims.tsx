import { Plus, RotateCw } from "lucide-react";
import { useCreateProvider, useOcr, useOcrScan } from "../../api/hooks";
import type { OcrService, Provider } from "../../api/types";
import { Button } from "../../components/Button";
import { nextStep } from "../../lib/errors";
import { OcrServiceStatus } from "./OcrIndicator";

/** Shims that answer but are not providers yet, each one click from being tested. */
export function FoundShims({ onAdded, showEmpty = false }: { onAdded: (provider: Provider) => void; showEmpty?: boolean }) {
  const ocr = useOcr();
  const scan = useOcrScan();
  const status = ocr.data;

  if (!status) {
    return ocr.isError ? (
      <p className="ocr-note" role="status">
        <span>OCR status is unavailable. Trying again.</span>
      </p>
    ) : null;
  }
  const found = status.services.filter((s) => !s.provider_id);
  const none = !found.length;
  if (none && (!showEmpty || status.services.length > 0)) return null;

  const lookAgain = (
    <Button size="sm" variant="ghost" icon={RotateCw} loading={scan.isPending} onClick={() => scan.mutate()}>
      Look again
    </Button>
  );
  if (none) {
    return (
      <div className="ocr-note">
        <p>
          {!status.discovery_enabled ? (
            <>OCR shim detection is off. Add a parser with its URL.</>
          ) : status.scanning ? (
            <>Looking for OCR shims on this machine.</>
          ) : (
            <>No OCR shim is running on this machine. One appears here when it starts. For a shim on another computer, add a parser with its URL.</>
          )}
        </p>
        {status.discovery_enabled ? lookAgain : null}
      </div>
    );
  }
  return (
    <section className="ocr-found" aria-label="OCR shims found">
      <header className="ocr-found__head">
        <h3>{found.length === 1 ? "Found 1 running shim" : `Found ${found.length} running shims`}</h3>
        <span className="page-head__spacer" />
        {lookAgain}
      </header>
      <ul>
        {found.map((service) => (
          <FoundShim key={service.id} service={service} onAdded={onAdded} />
        ))}
      </ul>
    </section>
  );
}

function FoundShim({ service, onAdded }: { service: OcrService; onAdded: (provider: Provider) => void }) {
  const create = useCreateProvider();
  const add = async () => {
    try {
      onAdded(await create.mutateAsync({ name: service.suggested_name ?? "ocr", kind: "parser", type: "http", config: { base_url: service.base_url } }));
    } catch {
      /* shown below */
    }
  };
  return (
    <li className="ocr-found__row">
      <span className="ocr-found__id">
        <span className="mono ocr-found__name">{service.name}</span>
        <span className="mono ocr-found__meta">
          {service.base_url} · {service.version}
        </span>
      </span>
      <OcrServiceStatus service={service} />
      <Button
        size="sm"
        variant={service.state === "detected" ? "primary" : "secondary"}
        icon={Plus}
        loading={create.isPending}
        disabled={service.state === "engine_down"}
        title={service.state === "engine_down" ? service.message : undefined}
        onClick={() => void add()}
      >
        {service.state === "no_ocr" ? "Add parser" : "Add and test OCR"}
      </Button>
      {service.state === "engine_down" ? <p className="ocr-found__note">{service.message}</p> : null}
      {create.error ? (
        <p className="ocr-found__note ocr-found__note--error" role="alert">
          {create.error.message} {nextStep(create.error)}
        </p>
      ) : null}
    </li>
  );
}
