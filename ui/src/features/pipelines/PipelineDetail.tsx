import { ArrowLeft, Copy, GitCompareArrows, RotateCw, Search } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import YAML from "yaml";
import { ApiError } from "../../api/client";
import { useCorpus, usePipeline, usePipelines, usePipelineStatus, useRetryRun } from "../../api/hooks";
import type { NodeRun, Pipeline, PipelineStatus, RunState } from "../../api/types";
import { Button, ButtonLink } from "../../components/Button";
import { CodeBlock } from "../../components/Code";
import { Select, Segmented } from "../../components/Field";
import { Fingerprint } from "../../components/Fingerprint";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { Badge, Meter, StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { count, percent } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { inputsOf, nodeSummary } from "./graph";
import { PipelineActions } from "./PipelineActions";
import { PipelineGraph, type GraphNode } from "./PipelineGraph";

const ORDER: RunState[] = ["succeeded", "running", "pending", "blocked", "failed"];

/** A per-node stacked bar of run states, with the counts as text. */
function RunBar({ counts, total }: { counts: Record<RunState, number>; total: number }) {
  const label = ORDER.filter((s) => counts[s]).map((s) => `${counts[s]} ${s}`).join(", ") || "no runs";
  return (
    <span className="runbar">
      <span className="runbar__track" role="img" aria-label={label}>
        {ORDER.map((s) => (counts[s] ? <span key={s} className={`runbar__seg runbar__seg--${s}`} style={{ width: `${(counts[s] / Math.max(1, total)) * 100}%` }} /> : null))}
      </span>
      <span className="runbar__text num">
        {count(counts.succeeded)}/{count(total)}
        {counts.failed ? <span className="runbar__failed"> · {count(counts.failed)} failed</span> : null}
        {counts.blocked ? <span className="runbar__blocked"> · {count(counts.blocked)} blocked</span> : null}
        {counts.running ? <span> · {count(counts.running)} running</span> : null}
      </span>
    </span>
  );
}

export function PipelineDetail() {
  const { corpusId = "", pipelineId = "" } = useParams();
  const pipeline = usePipeline(pipelineId);
  const status = usePipelineStatus(pipelineId);
  const corpus = useCorpus(corpusId);
  const back = (
    <Link to={`/corpora/${corpusId}/pipelines`} className="back-link">
      <ArrowLeft size={14} aria-hidden /> Pipelines
    </Link>
  );
  if (pipeline.isError) return <div className="page-pad">{back}<ErrorState error={pipeline.error} onRetry={() => void pipeline.refetch()} /></div>;
  if (!pipeline.data)
    return (
      <div className="page-pad">
        {back}
        <Skeleton width={240} height={22} />
        <Skeleton width="100%" height={260} />
      </div>
    );
  return <Detail p={pipeline.data} status={status.data} statusError={status.error} corpusId={corpusId} documentCount={corpus.data?.document_count ?? 0} back={back} />;
}

function Detail({ p, status, statusError, corpusId, documentCount, back }: { p: Pipeline; status: PipelineStatus | undefined; statusError: unknown; corpusId: string; documentCount: number; back: React.ReactNode }) {
  const navigate = useNavigate();
  const others = usePipelines(corpusId);
  const [configView, setConfigView] = useState<"summary" | "yaml">("summary");
  const nameOf = (id: string) => others.data?.items.find((x) => x.id === id)?.name ?? id.slice(0, 8);
  const active = others.data?.items.find((x) => x.is_active);
  const failedRuns = status?.nodes.reduce((n, x) => n + x.counts.failed, 0) ?? 0;
  const base = `/corpora/${corpusId}`;

  const graph: GraphNode[] = p.config.nodes.map((n) => {
    const ref = p.nodes.find((x) => x.node_id === n.id);
    const st = status?.nodes.find((x) => x.node_id === n.id);
    const total = st ? Object.values(st.counts).reduce((a, b) => a + b, 0) : 0;
    return {
      id: n.id,
      type: n.type,
      inputs: inputsOf(n),
      summary: nodeSummary(n),
      tone: st?.counts.failed ? "problem" : "default",
      extra: (
        <>
          {ref ? (
            <Fingerprint
              value={ref.node_fp}
              length={6}
              unique={ref.shared_with.length === 0}
              label={`${n.id} step`}
              note={ref.shared_with.length ? `Shared with ${ref.shared_with.map(nameOf).join(", ")}: computed once` : "Used only by this pipeline"}
            />
          ) : null}
          {ref?.shared_with.length ? <span className="pg__shared">shared with {ref.shared_with.map(nameOf).join(", ")}</span> : null}
          {st ? <RunBar counts={st.counts} total={total} /> : <Skeleton width={120} />}
        </>
      ),
    };
  });

  return (
    <div className="page-pad pl-detail">
      <header className="prov-detail__head">
        {back}
        <div className="prov-detail__title">
          <h1 className="mono">{p.name}</h1>
          {p.is_active ? <Badge tone="accent">active</Badge> : null}
          {p.archived ? <Badge>archived</Badge> : null}
          <StatusIndicator state={p.status} />
        </div>
        <dl className="prov-facts">
          <div>
            <dt>Fingerprint</dt>
            <dd>
              <Fingerprint value={p.fingerprint} length={12} label="Pipeline fingerprint" />
            </dd>
          </div>
          <div>
            <dt>Coverage</dt>
            <dd>
              <Meter
                value={p.coverage}
                tone={p.status === "degraded" ? "danger" : p.status === "building" ? "warn" : "neutral"}
                label={
                  <>
                    <strong>{percent(p.coverage)}</strong> {count(Math.round(p.coverage * documentCount))}/{count(documentCount)} documents
                  </>
                }
              />
            </dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd>
              <Time iso={p.created_at} />
            </dd>
          </div>
        </dl>
        <div className="prov-detail__actions">
          <ButtonLink to={`${base}/pipelines/new?from=${p.id}`} icon={Copy} variant="primary">
            Clone
          </ButtonLink>
          <span className="pl-compare">
            <GitCompareArrows size={14} aria-hidden />
            <Select aria-label="Compare with" value="" onChange={(e) => e.target.value && navigate(`${base}/pipelines/compare?a=${p.id}&b=${e.target.value}`)}>
              <option value="">Compare with…</option>
              {(others.data?.items ?? [])
                .filter((x) => x.id !== p.id)
                .map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.name}
                  </option>
                ))}
            </Select>
          </span>
          <ButtonLink to={`${base}/query?a=${p.id}`} icon={Search}>
            Query
          </ButtonLink>
          <PipelineActions corpusId={corpusId} pipeline={p} failedRuns={failedRuns} activeName={active && active.id !== p.id ? active.name : null} />
        </div>
        <p className="pl-detail__immutable muted">Pipelines cannot be edited once created. To change something, clone it: the steps you keep are shared, so only the changed ones run again.</p>
      </header>

      <section className="prov-section" aria-labelledby="progress-title">
        <h2 id="progress-title">Progress</h2>
        {statusError ? <ErrorState error={statusError} /> : null}
        <PipelineGraph label={`${p.name} steps and their progress`} nodes={graph} />
        <p className="pg-legend muted">Dashed fingerprints are used only by this pipeline; solid ones are shared, so their runs serve every pipeline that has them.</p>
      </section>

      <Failures failures={status?.recent_failures ?? []} corpusId={corpusId} pipelineId={p.id} loading={!status} />

      <section className="prov-section" aria-labelledby="config-title">
        <div className="section-head">
          <h2 id="config-title">Configuration</h2>
          <span className="page-head__spacer" />
          <Segmented
            label="Configuration view"
            value={configView}
            onChange={setConfigView}
            options={[
              { value: "summary", label: "Summary" },
              { value: "yaml", label: "YAML" },
            ]}
          />
        </div>
        {configView === "yaml" ? (
          <CodeBlock code={YAML.stringify(p.config, { lineWidth: 0 })} label={`${p.name}.yaml`} maxHeight={560} />
        ) : (
          <dl className="facts-list">
            {p.config.nodes.map((n) => (
              <div key={n.id}>
                <dt className="mono">{n.id}</dt>
                <dd className="mono">{nodeSummary(n)}</dd>
              </div>
            ))}
            <div>
              <dt>Sources</dt>
              <dd className="mono">{p.config.retrieval.sources.map((s) => `${s.id}: ${s.index} top ${s.top_k}`).join(" · ")}</dd>
            </div>
            {p.config.retrieval.fusion ? (
              <div>
                <dt>Fusion</dt>
                <dd className="mono">
                  {p.config.retrieval.fusion.method}
                  {p.config.retrieval.fusion.k ? ` k=${p.config.retrieval.fusion.k}` : ""}
                </dd>
              </div>
            ) : null}
            <div>
              <dt>Rerank</dt>
              <dd className="mono">{p.config.retrieval.rerank ? `${p.config.retrieval.rerank.provider} top ${p.config.retrieval.rerank.top_n}` : "none"}</dd>
            </div>
            <div>
              <dt>Expansion</dt>
              <dd className="mono">{p.config.retrieval.expand ? `${p.config.retrieval.expand.neighbors} neighbours` : "none"}</dd>
            </div>
            <div>
              <dt>Final top_k</dt>
              <dd className="mono">{p.config.retrieval.top_k}</dd>
            </div>
          </dl>
        )}
      </section>
    </div>
  );
}

function Failures({ failures, corpusId, pipelineId, loading }: { failures: NodeRun[]; corpusId: string; pipelineId: string; loading: boolean }) {
  const retry = useRetryRun();
  const toast = useToast();
  return (
    <section className="prov-section" aria-labelledby="failures-title">
      <h2 id="failures-title">Recent failures</h2>
      {loading ? (
        <Skeleton width="100%" height={60} />
      ) : !failures.length ? (
        <EmptyState title="No failed runs">
          <p>Every run of this pipeline's steps has succeeded or is still in progress.</p>
        </EmptyState>
      ) : (
        <div className="panel">
          <table className="rc-table fail-table">
            <thead>
              <tr>
                <th>Document</th>
                <th>Step</th>
                <th>Error</th>
                <th className="rc-right">Attempts</th>
                <th>When</th>
                <th className="rc-col-tight">
                  <span className="sr-only">Retry</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {failures.map((f) => (
                <tr key={f.id}>
                  <td className="fail-doc">
                    <Link to={`/corpora/${corpusId}/documents/${f.document_id}`} className="strong-link mono">
                      {f.document_filename}
                    </Link>
                  </td>
                  <td className="mono">{f.pipelines.find((x) => x.pipeline_id === pipelineId)?.node_id ?? f.node_type}</td>
                  <td className="fail-err">
                    <code>{f.error_code}</code>
                    <p>{f.error_message}</p>
                  </td>
                  <td className="rc-right num mono">
                    {f.attempt}/{f.max_attempts}
                  </td>
                  <td className="muted nowrap">{f.finished_at ? <Time iso={f.finished_at} compact /> : "—"}</td>
                  <td className="rc-col-tight">
                    <Button
                      size="sm"
                      icon={RotateCw}
                      loading={retry.isPending && retry.variables === f.id}
                      onClick={() =>
                        retry.mutate(f.id, {
                          onSuccess: () => toast({ tone: "ok", title: `Retrying ${f.document_filename}` }),
                          onError: (e) => toast({ tone: "danger", title: "Retry failed", body: e instanceof ApiError ? e.message : String(e) }),
                        })
                      }
                    >
                      Retry
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
