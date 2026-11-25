import { Can } from "../auth/Access";
import { RotateCw, Search } from "lucide-react";
import { Link, useSearchParams } from "react-router";
import { useCorpora, useNodeRuns, usePipelines, useRetryRun } from "../../api/hooks";
import type { NodeRun, RunState } from "../../api/types";
import { Button } from "../../components/Button";
import { SearchInput, Segmented, Select } from "../../components/Field";
import { EmptyState, ErrorState, SkeletonRows } from "../../components/States";
import { StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { useToast } from "../../lib/toast";
import { NODE_LABEL } from "../pipelines/model";

type StateFilter = "all" | RunState;

const ORDER: RunState[] = ["failed", "blocked", "running", "pending", "succeeded"];

/** Every node run across all corpora, filterable by status, corpus, pipeline and document. */
export function ActivityPage() {
  const [params, setParams] = useSearchParams();
  const toast = useToast();
  const corpora = useCorpora();
  const corpusId = params.get("corpus") ?? "";
  const pipelineId = params.get("pipeline") ?? "";
  const status = (params.get("status") as StateFilter | null) ?? "all";
  const q = params.get("q") ?? "";
  const pipelines = usePipelines(corpusId || undefined);
  const runs = useNodeRuns({
    corpus_id: corpusId || undefined,
    pipeline_id: pipelineId || undefined,
    status: status === "all" ? undefined : status,
  });
  const retry = useRetryRun();
  const corpusName = (id: string) => corpora.data?.items.find((c) => c.id === id)?.name ?? id.slice(0, 8);
  const pipelineName = (id: string) => pipelines.data?.items.find((p) => p.id === id)?.name ?? id.slice(0, 8);

  const set = (k: string, v: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (v === null || v === "" || v === "all") next.delete(k);
        else next.set(k, v);
        return next;
      },
      { replace: true },
    );

  const items = (runs.data?.items ?? []).filter(
    (r) => !q || r.document_filename.toLowerCase().includes(q.toLowerCase()) || (r.error_code ?? "").toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <div className="page-pad activity">
      <header className="page-head">
        <div>
          <h1>Activity</h1>
          <p className="page-head__sub">Every ingestion run across all corpora, newest first. Failures show the error and a retry.</p>
        </div>
      </header>

      <div className="pl-toolbar activity__toolbar">
        <Segmented<StateFilter>
          label="Status"
          value={status}
          onChange={(v) => set("status", v)}
          options={[
            { value: "all", label: "All" },
            { value: "failed", label: "Failed" },
            { value: "blocked", label: "Blocked" },
            { value: "running", label: "Running" },
            { value: "pending", label: "Pending" },
            { value: "succeeded", label: "Succeeded" },
          ]}
        />
        <Select
          aria-label="Corpus"
          value={corpusId}
          onChange={(e) =>
            setParams(
              (prev) => {
                const next = new URLSearchParams(prev);
                if (e.target.value) next.set("corpus", e.target.value);
                else next.delete("corpus");
                next.delete("pipeline");
                return next;
              },
              { replace: true },
            )
          }
        >
          <option value="">All corpora</option>
          {corpora.data?.items.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </Select>
        <Select aria-label="Pipeline" value={pipelineId} disabled={!corpusId} onChange={(e) => set("pipeline", e.target.value)}>
          <option value="">All pipelines</option>
          {pipelines.data?.items.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </Select>
        <span className="pl-toolbar__spacer" />
        <SearchInput aria-label="Filter by document or error" placeholder="Document or error" value={q} onChange={(e) => set("q", e.target.value)} className="pl-search" />
      </div>

      {runs.isError && !runs.data ? (
        <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
      ) : runs.data && items.length === 0 && !q && status === "all" && !corpusId ? (
        <EmptyState icon={Search} title="No runs yet">
          <p>Runs appear when a pipeline processes a document.</p>
        </EmptyState>
      ) : (
        <div className="panel activity__tablewrap">
          <table className="rc-table activity__table">
            <caption className="sr-only">Node runs</caption>
            <thead>
              <tr>
                <th>Status</th>
                <th>Document</th>
                <th>Step</th>
                <th>Pipeline</th>
                <th>Corpus</th>
                <th className="rc-right">Attempt</th>
                <th>Error</th>
                <th>Started</th>
                <th className="rc-col-tight"><span className="sr-only">Retry</span></th>
              </tr>
            </thead>
            <tbody>
              {!runs.data ? (
                <SkeletonRows rows={10} cols={[80, 200, 60, 120, 80, 40, 240, 80, 40]} />
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={9}>
                    <div className="pl-nomatch">
                      No runs match these filters.{" "}
                      <button type="button" className="link-btn" onClick={() => setParams(new URLSearchParams(), { replace: true })}>Clear filters</button>
                    </div>
                  </td>
                </tr>
              ) : (
                [...items]
                  .sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || Number(b.id) - Number(a.id))
                  .map((r) => <RunRow key={r.id} r={r} corpusName={corpusName(r.corpus_id)} pipelineName={pipelineName} retrying={retry.isPending && retry.variables === r.id} onRetry={() => retry.mutate(r.id, { onSuccess: () => toast({ tone: "ok", title: "Retrying run", body: r.document_filename }), onError: (e) => toast({ tone: "danger", title: "Retry failed", body: String(e) }) })} />)
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function RunRow({ r, corpusName, pipelineName, retrying, onRetry }: { r: NodeRun; corpusName: string; pipelineName: (id: string) => string; retrying: boolean; onRetry: () => void }) {
  return (
    <tr className="activity__row" data-status={r.status}>
      <td><StatusIndicator state={r.status} /></td>
      <td className="activity__doc">
        <Link to={`/corpora/${r.corpus_id}/documents/${r.document_id}`} className="strong-link mono">{r.document_filename}</Link>
      </td>
      <td className="mono">{r.pipelines[0]?.node_id ?? NODE_LABEL[r.node_type] ?? r.node_type}</td>
      <td className="mono muted">{r.pipelines.map((p) => pipelineName(p.pipeline_id)).join(", ") || "—"}</td>
      <td className="muted">{corpusName}</td>
      <td className="rc-right num mono">{r.attempt}/{r.max_attempts}</td>
      <td className="activity__error">
        {r.error_code ? (
          <span>
            <code>{r.error_code}</code> {r.error_message ? <span className="activity__errmsg">{r.error_message}</span> : null}
          </span>
        ) : r.status === "blocked" ? (
          <span className="muted">waiting on run #{r.blocked_by_run_id}</span>
        ) : null}
      </td>
      <td className="muted nowrap">{r.started_at ? <Time iso={r.started_at} compact /> : "—"}</td>
      <td className="rc-col-tight">
        {r.status === "failed" ? (
          <Can capability="edit"><Button size="sm" icon={RotateCw} loading={retrying} onClick={onRetry}>
            Retry
          </Button></Can>
        ) : null}
      </td>
    </tr>
  );
}
