import { KeyRound, Plus } from "lucide-react";
import { Fragment, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { useOcr, useProviders, useProviderTypes } from "../../api/hooks";
import type { Provider } from "../../api/types";
import { ButtonLink } from "../../components/Button";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, SkeletonRows } from "../../components/States";
import { Badge } from "../../components/Status";
import { keySettings } from "../../lib/configSchema";
import { count } from "../../lib/format";
import { FoundShims } from "../ocr/FoundShims";
import { OcrIndicator, OcrServiceStatus } from "../ocr/OcrIndicator";
import { isProblem } from "../ocr/model";
import { KIND_LABEL, KINDS, secretSummary } from "./model";

export function ProviderList() {
  const providers = useProviders();
  const types = useProviderTypes();
  const typeLabel = (p: Provider) => types.data?.items.find((t) => t.kind === p.kind && t.type === p.type)?.label ?? p.type;
  const items = providers.data?.items ?? [];
  const ocr = useOcr();
  const ocrById = new Map(ocr.data?.services.map((s) => [s.provider_id, s]));

  // Adding a found shim removes the button that had focus, so focus follows it to the new row.
  const [added, setAdded] = useState<string | null>(null);
  const addedLink = useRef<HTMLAnchorElement>(null);
  const addedShown = Boolean(added && items.some((p) => p.id === added));
  useEffect(() => {
    if (addedShown) addedLink.current?.focus();
  }, [addedShown, added]);

  return (
    <div className="page-pad prov">
      <header className="page-head">
        <div>
          <h1>Providers</h1>
          <p className="page-head__sub">The services your pipelines call. Pipelines refer to them by name.</p>
        </div>
        <span className="page-head__spacer" />
        <ButtonLink to="/providers/new" variant="primary" icon={Plus}>
          New provider
        </ButtonLink>
      </header>

      {providers.isError && !providers.data ? (
        <ErrorState error={providers.error} onRetry={() => void providers.refetch()} />
      ) : (
        KINDS.map((kind) => {
          const rows = items.filter((p) => p.kind === kind).sort((a, b) => Number(b.builtin) - Number(a.builtin) || a.name.localeCompare(b.name));
          const label = KIND_LABEL[kind];
          const parsers = kind === "parser";
          return (
            <section key={kind} className="prov-group" aria-labelledby={`prov-${kind}`}>
              <header className="prov-group__head">
                <h2 id={`prov-${kind}`}>
                  {label.many}
                  {providers.data ? <span className="prov-group__count num">{rows.length}</span> : null}
                </h2>
                <p className="prov-group__blurb">{label.blurb}</p>
                <span className="page-head__spacer" />
                <ButtonLink to={`/providers/new?kind=${kind}`} size="sm" variant="ghost" icon={Plus}>
                  Add {label.one.toLowerCase()}
                </ButtonLink>
              </header>
              {parsers && providers.data ? <FoundShims showEmpty onAdded={(p) => setAdded(p.id)} /> : null}
              {providers.data && rows.length === 0 ? null : (
                <div className="panel">
                  <table className={`rc-table prov-table${parsers ? " prov-table--parsers" : ""}`}>
                    <thead>
                      <tr>
                        <th className="prov-col-name">Name</th>
                        {parsers ? <th className="prov-col-ocr">OCR</th> : null}
                        <th className="prov-col-type">Type</th>
                        <th>Key setting</th>
                        <th className="prov-col-fp">Fingerprint</th>
                        <th className="prov-col-secrets">Secrets</th>
                        <th className="rc-right prov-col-used">Pipelines</th>
                      </tr>
                    </thead>
                    <tbody>
                      {!providers.data ? (
                        <SkeletonRows rows={2} cols={parsers ? [120, 100, 140, 220, 80, 120, 30] : [120, 140, 220, 80, 120, 30]} />
                      ) : (
                        rows.map((p) => {
                          const service = parsers ? ocrById.get(p.id) : undefined;
                          const problem = service && isProblem(service.state) ? service : undefined;
                          return (
                          <Fragment key={p.id}>
                          <tr className={problem ? "ocr-row--problem" : undefined}>
                            <td className="prov-col-name">
                              <span className="inline-gap">
                                <Link to={`/providers/${p.id}`} className="strong-link mono" ref={p.id === added ? addedLink : undefined}>
                                  {p.name}
                                </Link>
                                {p.builtin ? <Badge>built in</Badge> : null}
                              </span>
                            </td>
                            {parsers ? (
                              <td className="prov-col-ocr">
                                {p.type !== "http" ? <OcrIndicator state="no_ocr" /> : service ? <OcrServiceStatus service={service} /> : ocr.isError ? <span className="muted">Unknown</span> : <OcrIndicator state="looking" />}
                              </td>
                            ) : null}
                            <td className="prov-col-type muted">{typeLabel(p)}</td>
                            <td className="prov-key mono" title={keySettings(p.config) || undefined}>
                              {keySettings(p.config) || <span className="muted">—</span>}
                            </td>
                            <td className="prov-col-fp">
                              <Fingerprint value={p.fingerprint} label={`${p.name} fingerprint`} />
                            </td>
                            <td className="prov-col-secrets">
                              {secretSummary(p).length === 0 ? (
                                <span className="muted">none needed</span>
                              ) : (
                                <span className="prov-secrets">
                                  {secretSummary(p).map((s) => (
                                    <span key={s.key} className={`prov-secret ${s.set ? "" : "prov-secret--unset"}`}>
                                      <KeyRound size={12} strokeWidth={2} aria-hidden />
                                      <span className="mono">{s.key}</span> <span className="prov-secret__detail">{s.detail}</span>
                                    </span>
                                  ))}
                                </span>
                              )}
                            </td>
                            <td className="rc-right num mono prov-col-used">{p.used_by_pipelines ? count(p.used_by_pipelines) : <span className="muted">0</span>}</td>
                          </tr>
                          {problem ? (
                            <tr className="ocr-why">
                              <td colSpan={6}>
                                <span className="ocr-why__text">{problem.message}</span>{" "}
                                <Link to={`/providers/${p.id}`} className="inline-link">
                                  {problem.state === "needs_key" ? "Set the key" : "See details"}
                                </Link>
                              </td>
                              {/* Its own cell, because the type column hides on narrow screens. */}
                              <td className="prov-col-type" />
                            </tr>
                          ) : null}
                          </Fragment>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          );
        })
      )}
    </div>
  );
}
