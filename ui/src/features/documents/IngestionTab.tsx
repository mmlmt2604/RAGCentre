import { Can } from "../auth/Access";
import { RotateCw } from "lucide-react";
import { ApiError } from "../../api/client";
import { useNodeRuns, useRetryRun } from "../../api/hooks";
import type { Corpus, Document, NodeRun, Pipeline } from "../../api/types";
import { Button } from "../../components/Button";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, Skeleton } from "../../components/States";
import { Badge, StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { duration } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { NODE_LABEL } from "../pipelines/model";

/** What a run's stats say, in words. */
function statsText(run: NodeRun | undefined): string {
  const s = run?.stats;
  if (!s) return "";
  const parts: string[] = [];
  if (typeof s.n_chunks === "number") parts.push(`${s.n_chunks} chunks`);
  if (typeof s.blocks === "number") parts.push(`${s.blocks} blocks`);
  if (typeof s.n_texts === "number") parts.push(`${s.n_texts} texts${typeof s.n_cache_hits === "number" ? `, ${s.n_cache_hits} from cache` : ""}`);
  if (typeof s.n_records === "number") parts.push(`${s.n_records} records`);
  if (s.cache_hit === true) parts.push("from cache");
  if (s.warning === "no_chunks") parts.push("no chunks produced");
  return parts.join(" · ");
}

/** Every pipeline's runs for this document, in graph order, with errors and retry. */
export function IngestionTab({ doc, corpus, pipelines }: { doc: Document; corpus: Corpus; pipelines: Pipeline[] }) {
  const runs = useNodeRuns({ document_id: doc.id });
  const retry = useRetryRun();
  const toast = useToast();
  const byId = new Map((runs.data?.items ?? []).map((r) => [r.id, r]));
  const ordered = [...doc.ingestion].sort(
    (a, b) => Number(b.pipeline_id === corpus.active_pipeline_id) - Number(a.pipeline_id === corpus.active_pipeline_id),
  );
  // How many of this document's pipelines use each node, to show that a run is shared.
  const users = new Map<string, string[]>();
  for (const ing of doc.ingestion) {
    const name = pipelines.find((p) => p.id === ing.pipeline_id)?.name ?? ing.pipeline_id.slice(0, 8);
    for (const n of ing.nodes) users.set(n.node_fp, [...(users.get(n.node_fp) ?? []), name]);
  }

  if (runs.isError && !runs.data) return <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />;
  if (!doc.ingestion.length) return <p className="muted">This corpus has no pipelines yet, so nothing processes the document.</p>;

  return (
    <div className="ing">
      {ordered.map((ing) => {
        const p = pipelines.find((x) => x.id === ing.pipeline_id);
        return (
          <section key={ing.pipeline_id} className="ing-pl" aria-label={`Ingestion in ${p?.name ?? "pipeline"}`}>
            <header className="ing-pl__head">
              <h3 className="mono">{p?.name ?? ing.pipeline_id}</h3>
              {ing.pipeline_id === corpus.active_pipeline_id ? <Badge tone="accent">active</Badge> : null}
              <StatusIndicator state={ing.state} />
            </header>
            <table className="rc-table ing-table">
              <thead>
                <tr>
                  <th>Step</th>
                  <th className="ing-col-fp">Fingerprint</th>
                  <th className="ing-col-status">Status</th>
                  <th className="rc-right ing-col-attempt">Attempt</th>
                  <th className="rc-right ing-col-dur">Duration</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {ing.nodes.map((n) => {
                  const run = byId.get(n.run_id);
                  const others = (users.get(n.node_fp) ?? []).filter((u) => u !== p?.name);
                  const blocker = run?.blocked_by_run_id ? byId.get(run.blocked_by_run_id) : undefined;
                  const ms = typeof run?.stats?.duration_ms === "number" ? run.stats.duration_ms : null;
                  return (
                    <tr key={n.node_id} className={n.status === "failed" ? "ing-row--failed" : undefined}>
                      <td>
                        <span className="mono">{n.node_id}</span> <span className="muted">{NODE_LABEL[run?.node_type ?? ""] ?? run?.node_type ?? ""}</span>
                      </td>
                      <td className="ing-col-fp">
                        <Fingerprint value={n.node_fp} length={6} unique={others.length === 0} label={`${n.node_id} step`} note={others.length ? `Also used by ${others.join(", ")}: the same run serves both.` : "Used only by this pipeline"} />
                      </td>
                      <td className="ing-col-status">
                        <StatusIndicator state={n.status} />
                      </td>
                      <td className="rc-right num mono ing-col-attempt">{run ? `${run.attempt}/${run.max_attempts}` : !runs.data ? <Skeleton width={24} /> : "—"}</td>
                      <td className="rc-right num mono ing-col-dur">{ms !== null ? duration(ms) : "—"}</td>
                      <td className="ing-result">
                        {n.status === "failed" ? (
                          <div className="ing-error">
                            <code>{run?.error_code ?? n.error_code}</code>
                            {run?.error_message ? <p>{run.error_message}</p> : null}
                            <Can capability="edit"><Button
                              size="sm"
                              icon={RotateCw}
                              loading={retry.isPending && retry.variables === n.run_id}
                              onClick={() =>
                                retry.mutate(n.run_id, {
                                  onSuccess: () => toast({ tone: "ok", title: `Retrying ${n.node_id}`, body: "The steps after it continue once it succeeds." }),
                                  onError: (e) => toast({ tone: "danger", title: "Retry failed", body: e instanceof ApiError ? e.message : String(e) }),
                                })
                              }
                            >
                              Retry
                            </Button></Can>
                          </div>
                        ) : n.status === "blocked" ? (
                          <span className="ing-blocked">
                            Waiting on {blocker ? <><span className="mono">{blocker.node_type}</span> run #{blocker.id}</> : `run #${run?.blocked_by_run_id ?? "?"}`}, which failed. Retry that step to continue.
                          </span>
                        ) : n.status === "succeeded" ? (
                          <span className="muted">
                            {statsText(run)}
                            {run?.finished_at ? <> · <Time iso={run.finished_at} /></> : null}
                          </span>
                        ) : n.status === "running" ? (
                          <span className="muted">Started {run?.started_at ? <Time iso={run.started_at} /> : "just now"}</span>
                        ) : (
                          <span className="muted">Waiting for the steps before it</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        );
      })}
    </div>
  );
}
