import { Can } from "../auth/Access";
import { Archive, CircleCheck, Copy, ExternalLink, GitCompareArrows, RotateCw, Star } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { useArchivePipeline, usePipelineStatus, useRetryFailed, useSetActivePipeline } from "../../api/hooks";
import type { NodeRun, Pipeline } from "../../api/types";
import { Button, ButtonLink } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, Skeleton } from "../../components/States";
import { nextStep } from "../../lib/errors";
import { Badge, Meter, StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { useToast } from "../../lib/toast";
import { count, percent, plural } from "../../lib/format";
import { NODE_LABEL } from "./model";

interface Diagnostic {
  key: string;
  code: string;
  nodeType: string;
  runs: NodeRun[];
}

function groupFailures(failures: NodeRun[]): Diagnostic[] {
  const m = new Map<string, Diagnostic>();
  for (const f of failures) {
    const key = `${f.error_code}|${f.node_type}`;
    const d = m.get(key) ?? { key, code: f.error_code ?? "error", nodeType: f.node_type, runs: [] };
    d.runs.push(f);
    m.set(key, d);
  }
  return [...m.values()];
}

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return [e.message, nextStep(e)].filter(Boolean).join(" ");
  return "The request failed.";
}

export function PipelinePanel({
  corpusId,
  pipeline,
  documentCount,
  pipelines,
}: {
  corpusId: string;
  pipeline: Pipeline | null;
  documentCount: number;
  pipelines: Pipeline[];
}) {
  const status = usePipelineStatus(pipeline?.id);
  const retry = useRetryFailed();
  const archive = useArchivePipeline();
  const setActive = useSetActivePipeline();
  const toast = useToast();
  const [confirm, setConfirm] = useState<"archive" | "activate" | null>(null);

  if (!pipeline) {
    return <aside className="pl-panel pl-panel--empty" aria-label="Pipeline details" />;
  }

  const s = status.data;
  const failed = s?.nodes.reduce((n, x) => n + x.counts.failed, 0) ?? 0;
  const blocked = s?.nodes.reduce((n, x) => n + x.counts.blocked, 0) ?? 0;
  const inFlight = s?.nodes.reduce((n, x) => n + x.counts.pending + x.counts.running - x.counts.blocked, 0) ?? 0;
  const diagnostics = groupFailures(s?.recent_failures ?? []);
  const nameOf = (id: string) => pipelines.find((p) => p.id === id)?.name ?? "another pipeline";
  const base = `/corpora/${corpusId}/pipelines`;

  const doRetry = () =>
    retry.mutate(pipeline.id, {
      onSuccess: (r) => toast({ tone: "ok", title: `Retrying ${plural(r.retried, "run")}`, body: `${pipeline.name} will pick them up in the next worker pass.` }),
      onError: (e) => toast({ tone: "danger", title: "Retry failed", body: errorMessage(e) }),
    });

  const doArchive = () =>
    archive.mutate(pipeline.id, {
      onSuccess: () => {
        setConfirm(null);
        toast({ tone: "ok", title: `Archived ${pipeline.name}` });
      },
    });

  const doActivate = (force: boolean) =>
    setActive.mutate(
      { corpusId, pipelineId: pipeline.id, force },
      {
        onSuccess: () => {
          setConfirm(null);
          toast({ tone: "ok", title: `${pipeline.name} is now the active pipeline`, body: "Queries without a pipeline use it from now on." });
        },
        onError: (e) => {
          if (e instanceof ApiError && e.code === "index_incomplete") setConfirm("activate");
          else toast({ tone: "danger", title: "Could not make it active", body: errorMessage(e) });
        },
      },
    );

  return (
    <aside className="pl-panel" aria-label={`${pipeline.name} details`}>
      <header className="pl-panel__head">
        <div className="pl-panel__title">
          <h2>{pipeline.name}</h2>
          {pipeline.is_active ? <Badge tone="accent">active</Badge> : null}
        </div>
        <div className="pl-panel__meta">
          <StatusIndicator state={pipeline.status} />
          <Fingerprint value={pipeline.fingerprint} label="Pipeline fingerprint" />
          <span className="pl-panel__created">
            Created <Time iso={pipeline.created_at} />
          </span>
        </div>
        <Meter
          value={pipeline.coverage}
          tone={pipeline.status === "degraded" ? "danger" : pipeline.status === "building" ? "warn" : "neutral"}
          label={
            <>
              <strong>{percent(pipeline.coverage)}</strong> of documents searchable · {count(Math.round(pipeline.coverage * documentCount))} of {count(documentCount)}
            </>
          }
        />
      </header>

      <section className="pl-panel__actions" aria-label="Actions">
        <ButtonLink to={`${base}/${pipeline.id}`} icon={ExternalLink}>
          Open
        </ButtonLink>
        <Can capability="edit"><ButtonLink to={`${base}/new?from=${pipeline.id}`} icon={Copy}>
          Clone
        </ButtonLink></Can>
        <ButtonLink to={`${base}/compare?a=${pipeline.id}`} icon={GitCompareArrows}>
          Compare with…
        </ButtonLink>
        {!pipeline.is_active && !pipeline.archived ? (
          <Can capability="edit"><Button icon={Star} loading={setActive.isPending && confirm === null} onClick={() => doActivate(false)}>
            Make active
          </Button></Can>
        ) : null}
      </section>

      <section className="pl-panel__section" aria-labelledby="pl-diag">
        <h3 id="pl-diag">Diagnostics</h3>
        {status.isError && !s ? (
          <ErrorState error={status.error} onRetry={() => void status.refetch()} />
        ) : !s ? (
          <div className="pl-diag-skel">
            <Skeleton width="80%" />
            <Skeleton width="60%" />
          </div>
        ) : failed === 0 && blocked === 0 && inFlight <= 0 ? (
          <p className="pl-ok">
            <CircleCheck size={14} aria-hidden /> No problems. Every document has been through every step.
          </p>
        ) : (
          <ul className="pl-diags">
            {diagnostics.map((d) => (
              <li key={d.key} className="pl-diag pl-diag--danger">
                <span className="pl-diag__mark" aria-hidden />
                <div>
                  <p className="pl-diag__code">
                    <code>{d.code}</code> in {NODE_LABEL[d.nodeType]?.toLowerCase() ?? d.nodeType}
                  </p>
                  <p className="pl-diag__msg">
                    {plural(d.runs.length, "run")} failed. {d.runs[0]?.error_message}
                  </p>
                </div>
              </li>
            ))}
            {blocked > 0 ? (
              <li className="pl-diag pl-diag--warn">
                <span className="pl-diag__mark" aria-hidden />
                <div>
                  <p className="pl-diag__code">
                    <code>blocked</code>
                  </p>
                  <p className="pl-diag__msg">{plural(blocked, "run")} waiting on failed steps. They continue once those succeed.</p>
                </div>
              </li>
            ) : null}
            {inFlight > 0 ? (
              <li className="pl-diag pl-diag--info">
                <span className="pl-diag__mark" aria-hidden />
                <div>
                  <p className="pl-diag__code">
                    <code>building</code>
                  </p>
                  <p className="pl-diag__msg">{plural(inFlight, "run")} queued or running.</p>
                </div>
              </li>
            ) : null}
          </ul>
        )}
        {failed > 0 ? (
          <Can capability="edit"><Button variant="primary" icon={RotateCw} loading={retry.isPending} onClick={doRetry} className="pl-retry">
            Retry {plural(failed, "failed run")}
          </Button></Can>
        ) : null}
      </section>

      <section className="pl-panel__section" aria-labelledby="pl-steps">
        <h3 id="pl-steps">Steps</h3>
        <ol className="pl-nodes">
          {pipeline.nodes.map((n) => {
            const c = s?.nodes.find((x) => x.node_fp === n.node_fp)?.counts;
            const total = c ? c.pending + c.running + c.succeeded + c.failed : 0;
            return (
              <li key={n.node_id} className="pl-node">
                <span className="pl-node__type">{NODE_LABEL[n.type] ?? n.type}</span>
                <Fingerprint value={n.node_fp} label={`${NODE_LABEL[n.type]} · ${n.node_id}`} unique={n.shared_with.length === 0} />
                <span className="pl-node__count num">
                  {c ? (
                    c.failed ? (
                      <span className="tone-danger">{count(c.failed)} failed</span>
                    ) : c.succeeded === total ? (
                      <span className="muted">{count(total)}</span>
                    ) : (
                      <span className="tone-warn">
                        {count(c.succeeded)}/{count(total)}
                      </span>
                    )
                  ) : (
                    <Skeleton width={36} />
                  )}
                </span>
                <span className="pl-node__share">
                  {n.shared_with.length ? `Shared with ${n.shared_with.map(nameOf).join(", ")}` : "Only this pipeline"}
                </span>
              </li>
            );
          })}
        </ol>
      </section>

      {!pipeline.archived ? (
        <section className="pl-panel__danger" aria-label="Danger zone">
          <Can capability="edit"><Button variant="danger" icon={Archive} onClick={() => setConfirm("archive")}>
            Archive pipeline
          </Button></Can>
        </section>
      ) : null}

      <ConfirmDialog
        open={confirm === "archive"}
        onClose={() => {
          setConfirm(null);
          archive.reset();
        }}
        onConfirm={doArchive}
        destructive
        pending={archive.isPending}
        title={`Archive ${pipeline.name}?`}
        confirmLabel="Archive pipeline"
        error={archive.isError ? errorMessage(archive.error) : null}
      >
        <p>
          <strong>{pipeline.name}</strong> stops receiving new documents, and queries can no longer use it. Steps it shares with other pipelines keep running for them.
        </p>
        <p>Its indexes stay on disk until garbage collection. Archiving cannot be undone; clone it to bring the configuration back.</p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirm === "activate"}
        onClose={() => {
          setConfirm(null);
          setActive.reset();
        }}
        onConfirm={() => doActivate(true)}
        pending={setActive.isPending}
        title={`Make ${pipeline.name} active while it is ${pipeline.status}?`}
        confirmLabel="Make active anyway"
      >
        <p>
          Only <strong>{percent(pipeline.coverage)}</strong> of documents are searchable through {pipeline.name}. Queries that do not name a pipeline will use it immediately and miss the rest.
        </p>
      </ConfirmDialog>
    </aside>
  );
}
