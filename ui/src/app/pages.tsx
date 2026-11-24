import { Database, FolderSearch, Hammer, Plus, Sparkles } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Link, useRouteError } from "react-router";
import { useCorpora } from "../api/hooks";
import { ButtonLink } from "../components/Button";
import { EmptyState, ErrorState, SkeletonRows } from "../components/States";
import { StatusIndicator } from "../components/Status";
import { Time } from "../components/Time";
import { count, percent } from "../lib/format";

export function NotFound() {
  return (
    <div className="page-pad">
      <EmptyState icon={FolderSearch} title="Nothing at this address" action={<ButtonLink to="/">Go to corpora</ButtonLink>}>
        <p>The page may have moved, or the link may point at something that was archived or deleted.</p>
      </EmptyState>
    </div>
  );
}

/** Stand-in for screens built in later milestones (IMPLEMENTATION_PLAN.md §18, Phase A). */
export function Placeholder({ title, milestone }: { title: string; milestone: string }) {
  return (
    <div className="page-pad">
      <header className="page-head">
        <h1>{title}</h1>
      </header>
      <EmptyState icon={Hammer} title={`${title} is not built yet`}>
        <p>
          This screen arrives in milestone <span className="mono">{milestone}</span>. The navigation, data and mock API around it already work.
        </p>
      </EmptyState>
    </div>
  );
}

export function RouteError() {
  const error = useRouteError();
  return <ErrorState error={error} title="This page failed to render" onRetry={() => window.location.reload()} />;
}

/** Catches render errors below the shell so one broken screen never blanks the app. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: unknown }> {
  state = { error: null as unknown };
  static getDerivedStateFromError(error: unknown) {
    return { error };
  }
  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error(error, info.componentStack);
  }
  render() {
    if (this.state.error) return <ErrorState error={this.state.error} title="This page failed to render" onRetry={() => this.setState({ error: null })} />;
    return this.props.children;
  }
}

export function CorpusList() {
  const corpora = useCorpora();
  return (
    <div className="page-pad">
      <header className="page-head">
        <h1>Corpora</h1>
        <span className="page-head__spacer" />
        <ButtonLink to="/corpora/new" variant="primary" icon={Plus}>
          New corpus
        </ButtonLink>
      </header>
      {corpora.isError && !corpora.data ? (
        <ErrorState error={corpora.error} onRetry={() => void corpora.refetch()} />
      ) : corpora.data && corpora.data.items.length === 0 ? (
        <EmptyState
          icon={Database}
          title="No corpora yet"
          action={
            <span className="inline-gap">
              <ButtonLink to="/?setup" variant="primary" icon={Sparkles}>
                Guided setup
              </ButtonLink>
              <ButtonLink to="/corpora/new" icon={Plus}>
                New corpus
              </ButtonLink>
            </span>
          }
        >
          <p>A corpus is a set of documents that you search together. Pipelines, queries and evaluations all belong to one corpus.</p>
          <p>Guided setup picks a parser, embedder and vector store, creates a corpus and its first pipeline, and takes about two minutes.</p>
        </EmptyState>
      ) : (
        <div className="panel">
          <table className="rc-table">
            <thead>
              <tr>
                <th>Corpus</th>
                <th className="rc-right">Documents</th>
                <th>Active pipeline</th>
                <th>Pipelines</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {!corpora.data ? (
                <SkeletonRows rows={3} cols={[140, 60, 160, 120, 80]} />
              ) : (
                corpora.data.items.map((c) => {
                  const active = c.pipelines.find((p) => p.id === c.active_pipeline_id);
                  const degraded = c.pipelines.filter((p) => p.status === "degraded").length;
                  const building = c.pipelines.filter((p) => p.status === "building").length;
                  return (
                    <tr key={c.id}>
                      <td>
                        <Link to={`/corpora/${c.id}/pipelines`} className="strong-link">
                          {c.name}
                        </Link>
                      </td>
                      <td className="rc-right num mono">{count(c.document_count)}</td>
                      <td>
                        {active ? (
                          <span className="inline-gap">
                            <span className="mono">{active.name}</span>
                            <StatusIndicator state={active.status} label={active.status === "building" ? `Building ${percent(active.coverage)}` : undefined} />
                          </span>
                        ) : (
                          <span className="muted">None</span>
                        )}
                      </td>
                      <td>
                        <span className="inline-gap">
                          <span className="num mono">{c.pipelines.length}</span>
                          {degraded ? <StatusIndicator state="degraded" label={`${degraded} degraded`} /> : null}
                          {building ? <StatusIndicator state="building" label={`${building} building`} /> : null}
                        </span>
                      </td>
                      <td className="muted">
                        <Time iso={c.created_at} />
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
