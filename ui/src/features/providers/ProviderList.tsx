import { KeyRound, Plus } from "lucide-react";
import { Link } from "react-router";
import { useProviders, useProviderTypes } from "../../api/hooks";
import type { Provider } from "../../api/types";
import { ButtonLink } from "../../components/Button";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, SkeletonRows } from "../../components/States";
import { Badge } from "../../components/Status";
import { keySettings } from "../../lib/configSchema";
import { count } from "../../lib/format";
import { KIND_LABEL, KINDS, secretSummary } from "./model";

export function ProviderList() {
  const providers = useProviders();
  const types = useProviderTypes();
  const typeLabel = (p: Provider) => types.data?.items.find((t) => t.kind === p.kind && t.type === p.type)?.label ?? p.type;
  const items = providers.data?.items ?? [];

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
              {providers.data && rows.length === 0 ? null : (
                <div className="panel">
                  <table className="rc-table prov-table">
                    <thead>
                      <tr>
                        <th className="prov-col-name">Name</th>
                        <th className="prov-col-type">Type</th>
                        <th>Key setting</th>
                        <th className="prov-col-fp">Fingerprint</th>
                        <th className="prov-col-secrets">Secrets</th>
                        <th className="rc-right prov-col-used">Pipelines</th>
                      </tr>
                    </thead>
                    <tbody>
                      {!providers.data ? (
                        <SkeletonRows rows={2} cols={[120, 140, 220, 80, 120, 30]} />
                      ) : (
                        rows.map((p) => (
                          <tr key={p.id}>
                            <td className="prov-col-name">
                              <span className="inline-gap">
                                <Link to={`/providers/${p.id}`} className="strong-link mono">
                                  {p.name}
                                </Link>
                                {p.builtin ? <Badge>built in</Badge> : null}
                              </span>
                            </td>
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
                        ))
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
