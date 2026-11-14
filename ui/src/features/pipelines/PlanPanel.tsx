import { CircleAlert, CircleCheck, Plus, Recycle } from "lucide-react";
import type { Pipeline, PipelinePlan } from "../../api/types";
import { Button } from "../../components/Button";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, Skeleton } from "../../components/States";
import { count, plural } from "../../lib/format";
import { NODE_LABEL } from "./model";

/**
 * Always visible beside the builder: the dry-run result for the current configuration. Problems
 * link to the node they name; a valid plan says, per node, whether its work already exists.
 */
export function PlanPanel({
  plan,
  loading,
  error,
  onRetry,
  stale,
  pipelines,
  documentCount,
  onSelectNode,
  onCreate,
  creating,
  createError,
}: {
  plan: PipelinePlan | undefined;
  loading: boolean;
  error: unknown;
  onRetry?: () => void;
  stale: boolean;
  pipelines: Pipeline[];
  documentCount: number;
  onSelectNode: (id: string) => void;
  onCreate: () => void;
  creating: boolean;
  createError: React.ReactNode;
}) {
  const name = (id: string) => pipelines.find((p) => p.id === id)?.name ?? id.slice(0, 8);
  const allReused = plan?.valid && plan.nodes.length > 0 && plan.total_runs_to_create === 0;
  return (
    <aside className="plan" aria-label="Plan" aria-busy={loading || stale || undefined}>
      <header className="plan__head">
        <h2>Plan</h2>
        <span className="plan__status" aria-live="polite">
          {error ? "Needs attention" : loading || stale ? "Checking…" : plan ? (plan.valid ? "Valid" : plural(plan.problems.length, "problem")) : ""}
        </span>
      </header>

      {error ? (
        <ErrorState error={error} title="Could not check the pipeline" onRetry={onRetry} />
      ) : !plan ? (
        <div className="plan__body">
          <Skeleton width="80%" />
          <Skeleton width="60%" />
          <Skeleton width="70%" />
        </div>
      ) : !plan.valid ? (
        <ol className="plan__problems">
          {plan.problems.map((p, i) => (
            <li key={i}>
              <CircleAlert size={14} strokeWidth={2} aria-hidden className="plan__icon--bad" />
              <div>
                {p.node_id ? (
                  <button type="button" className="link-btn mono" onClick={() => onSelectNode(p.node_id!)}>
                    {p.node_id}
                  </button>
                ) : (
                  <span className="mono muted">{p.path}</span>
                )}
                <p>{p.message}</p>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <div className="plan__body">
          {allReused ? (
            <p className="plan__callout">
              <Recycle size={16} strokeWidth={2} aria-hidden /> No ingestion needed — ready immediately. Every step already exists in this corpus.
            </p>
          ) : (
            <p className="plan__total">
              Creating it starts <strong className="num">{count(plan.total_runs_to_create)}</strong> runs over {plural(documentCount, "document")}.
            </p>
          )}
          <ul className="plan__nodes">
            {plan.nodes.map((n) => (
              <li key={n.node_id}>
                <button type="button" className="plan__node" onClick={() => onSelectNode(n.node_id)}>
                  <span className="plan__nodeid">
                    <span className="mono">{n.node_id}</span> <span className="muted">{NODE_LABEL[n.type]}</span>
                  </span>
                  <span className="plan__work">
                    {n.reused ? (
                      <>
                        <CircleCheck size={13} strokeWidth={2} aria-hidden className="plan__icon--ok" /> reused
                        {n.runs_to_create ? `, ${count(n.runs_to_create)} new docs` : ""}
                      </>
                    ) : (
                      <>
                        <Plus size={13} strokeWidth={2} aria-hidden /> new · {count(n.runs_to_create)} runs
                      </>
                    )}
                  </span>
                </button>
                <span className="plan__fp">
                  <Fingerprint value={n.node_fp} length={6} unique={!n.reused} label={`${n.node_id} step`} note={n.shared_with.length ? `Shared with ${n.shared_with.map(name).join(", ")}` : n.reused ? "Built by an archived pipeline" : "New: no pipeline has this step yet"} />
                  {n.shared_with.length ? <span className="plan__shared">{n.shared_with.map(name).join(", ")}</span> : null}
                </span>
              </li>
            ))}
          </ul>
          {plan.pipeline_fingerprint ? (
            <p className="plan__fpline">
              Pipeline fingerprint <Fingerprint value={plan.pipeline_fingerprint} length={10} label="Pipeline fingerprint" />
            </p>
          ) : null}
        </div>
      )}

      <footer className="plan__foot">
        {createError}
        <Button variant="primary" loading={creating} disabled={!plan?.valid || stale || loading || Boolean(error)} onClick={onCreate}>
          Create pipeline
        </Button>
      </footer>
    </aside>
  );
}
