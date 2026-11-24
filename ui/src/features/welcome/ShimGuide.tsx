import { useEffect, useEffectEvent, useState } from "react";
import { useOcrScan } from "../../api/hooks";
import type { OcrService, OcrStatus, Provider } from "../../api/types";
import { Button } from "../../components/Button";
import { CodeBlock } from "../../components/Code";
import { Field, Segmented, TextInput } from "../../components/Field";
import { ENGINES, urlPort, type EngineId, type RunWith } from "./engines";
import { guideDetails, type Errors, type GuideChoice } from "./model";

const docker = { value: "docker", label: "Docker" } as const;
const uv = { value: "uv", label: "uv (Python)" } as const;
const remote = { value: "remote", label: "Another computer" } as const;

function useShimWatch(port: number, watchUrl: string | null, services: OcrService[]) {
  const scan = useOcrScan();
  const checkNow = useEffectEvent(() => {
    if (!scan.isPending) scan.mutate();
  });
  const [timer, setTimer] = useState({ url: watchUrl, elapsed: 0 });
  const candidates = watchUrl ? services.filter((s) => !s.provider_id && urlPort(s.base_url) === port) : [];
  const match = candidates.find((s) => s.base_url === watchUrl) ?? candidates[0];
  useEffect(() => {
    if (!watchUrl || match) return;
    const start = Date.now();
    const clock = window.setInterval(() => setTimer({ url: watchUrl, elapsed: Math.floor((Date.now() - start) / 1000) }), 1000);
    const check = window.setInterval(() => checkNow(), 15000);
    // Schedule the first scan so Strict Mode can clean it up before mounting again.
    const first = window.setTimeout(() => {
      setTimer({ url: watchUrl, elapsed: 0 });
      checkNow();
    }, 0);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(clock);
      window.clearInterval(check);
    };
  }, [watchUrl, match]);
  return { scan, elapsed: timer.url === watchUrl ? timer.elapsed : 0, match };
}

export function ShimGuide({ choice, status, providers, errors, onChange, onFound, onReuse }: {
  choice: GuideChoice;
  status: OcrStatus;
  providers: Provider[];
  errors: Errors;
  onChange: (choice: GuideChoice) => void;
  onFound: (service: OcrService, label: string) => void;
  onReuse: (name: string) => void;
}) {
  const { engine, command, registered, watchUrl, manualUrl } = guideDetails(choice, status, providers);
  const { scan, elapsed, match } = useShimWatch(engine.port, watchUrl, status.services);
  useEffect(() => {
    if (match) onFound(match, engine.label);
  }, [match, engine.label, onFound]);

  const explanation = choice.runWith === "remote" ? "For a GPU box or a shared server."
    : status.in_container
      ? choice.runWith === "docker" ? "Start the shim next to RAGCentre in the same Compose project." : "RAGCentre reaches the shim through host.docker.internal."
      : choice.runWith === "uv" ? "Runs the shim on this machine, where RAGCentre runs." : `Builds the shim's image and runs it on this machine's port ${engine.port}.`;
  const change = (id: EngineId, runWith: RunWith) => onChange({ mode: "guide", engine: id, runWith });
  return (
    <div className="wl-guide">
      <p>Run these from a checkout of the RAGCentre repository.</p>
      <div className="wl-engines" role="radiogroup" aria-label="OCR engine">
        {ENGINES.map((entry) => (
          <label key={entry.id} className="wl-engine">
            <input type="radio" name="engine" checked={entry.id === engine.id} onChange={() => change(entry.id, choice.runWith)} />
            <span className="wl-engine__copy">
              <span className="wl-engine__head">
                <span className="wl-engine__name">{entry.label} <span className="mono">:{entry.port}</span></span>
                <span className="rc-badge">{entry.hardware}</span>
              </span>
              <span>{entry.summary}</span>
            </span>
          </label>
        ))}
      </div>
      <div className="wl-guide__mode">
        <span>Run with</span>
        <Segmented label="Run with" value={choice.runWith} options={status.in_container ? [docker, uv, remote] : [uv, docker, remote]} onChange={(mode) => change(engine.id, mode)} />
        {registered ? null : <p>{explanation}</p>}
      </div>
      {registered ? (
        <div className="wl-guide__status">
          <span>{registered.name} is already registered at <code>{String(registered.config.base_url)}</code>.</span>
          <Button size="sm" onClick={() => onReuse(registered.name)}>Use {registered.name}</Button>
        </div>
      ) : (
        <>
          <div className="wl-guide__command" key={`${engine.id}:${choice.runWith}`}>
            <CodeBlock code={command.lines.join("\n")} label="Terminal" maxHeight={160} copyLabel />
          </div>
          {command.notes.map((note) => <p key={note} className="wl-guide__note">{note}</p>)}
          {watchUrl ? (
            <>
              <div className="wl-guide__status wl-guide__status--waiting">
                <span className="rc-spinner" aria-hidden />
                <span>
                  <span aria-live="polite">Waiting for {engine.label} at <code>{watchUrl}</code></span>
                  {" "}<span className="mono num" aria-hidden>· {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}</span>
                </span>
                <Button size="sm" variant="ghost" loading={scan.isPending} onClick={() => scan.mutate()}>Look now</Button>
              </div>
              {elapsed >= 600 ? <p>Still nothing on port {engine.port}. Check the command's output, or that nothing else uses the port.</p>
                : elapsed >= 120 ? <p>Building the image or installing the engine can take several minutes the first time. This keeps watching.</p> : null}
            </>
          ) : null}
          {manualUrl !== undefined ? (
            <>
              {!status.discovery_enabled ? <p>Detection is off on this server (<code>RAGCENTRE_DISCOVERY_URLS=off</code>). When the shim is running, enter its address.</p> : null}
              <Field label="Service URL" error={errors.parserUrl} hint={choice.runWith === "remote" ? "To have RAGCentre find it on its own, add the URL to RAGCENTRE_DISCOVERY_URLS." : undefined}>
                {(a11y) => {
                  const selectHost = ({ currentTarget }: { currentTarget: HTMLInputElement }) => {
                    const start = manualUrl.indexOf("<host>");
                    if (start >= 0) currentTarget.setSelectionRange(start, start + 6);
                  };
                  return <TextInput {...a11y} mono value={manualUrl} onFocus={selectHost} onClick={selectHost} onChange={(e) => onChange({ ...choice, url: e.target.value })} />;
                }}
              </Field>
            </>
          ) : null}
        </>
      )}
      {errors.parserGuide ? <p className="rc-field__error" role="alert" tabIndex={-1} data-setup-error>{errors.parserGuide}</p> : null}
    </div>
  );
}
