import { ArrowLeft, Crown, GitCompareArrows, ListFilter, TriangleAlert } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";
import { useEvalRun } from "../../api/hooks";
import type { EvalRunDetail, EvalSummary } from "../../api/types";
import { Field, Select } from "../../components/Field";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, Skeleton } from "../../components/States";
import { Meter, StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { count, duration } from "../../lib/format";
import { comparisonFor, fmt, metricOrder, signed, summary } from "./model";

interface CostRow {
  label: string;
  value: (s: EvalSummary) => number;
  show: (n: number) => string;
  lowerIsBetter?: boolean;
}

const COSTS: CostRow[] = [
  { label: "latency p50", value: (s) => s.latency_ms.p50, show: duration, lowerIsBetter: true },
  { label: "latency p95", value: (s) => s.latency_ms.p95, show: duration, lowerIsBetter: true },
  { label: "errors", value: (s) => s.error_count, show: count, lowerIsBetter: true },
  { label: "chunks", value: (s) => s.chunk_count, show: count },
  { label: "mean chunk length", value: (s) => s.mean_chunk_chars, show: (n) => `${count(Math.round(n))} chars` },
];

function Best() {
  return <Crown size={12} strokeWidth={2.25} className="ev-best" aria-label="best" />;
}

export function RunComparison() {
  const { corpusId = "", runId = "" } = useParams();
  const run = useEvalRun(runId);
  const base = `/corpora/${corpusId}/eval`;
  const back = (
    <Link to={base} className="back-link">
      <ArrowLeft size={14} aria-hidden /> Evaluation
    </Link>
  );
  if (run.isError) return <div className="page-pad">{back}<ErrorState error={run.error} onRetry={() => void run.refetch()} /></div>;
  if (!run.data)
    return (
      <div className="page-pad">
        {back}
        <Skeleton width={260} height={22} />
        <Skeleton width="100%" height={400} />
      </div>
    );
  return <Detail run={run.data} corpusId={corpusId} back={back} />;
}

function Detail({ run, corpusId, back }: { run: EvalRunDetail; corpusId: string; back: React.ReactNode }) {
  const [params, setParams] = useSearchParams();
  const base = `/corpora/${corpusId}`;
  const s = run.summaries;
  const baseline = params.get("base") && run.pipeline_ids.includes(params.get("base")!) ? params.get("base")! : run.pipeline_ids[0]!;
  const others = s.filter((x) => x.pipeline_id !== baseline);
  const pair = s.length === 2;
  const a = baseline;
  const b = pair ? others[0]!.pipeline_id : null;
  const items = run.item_count;
  const metrics = metricOrder(Object.keys(s[0]?.metrics ?? {}));
  const name = (id: string) => s.find((x) => x.pipeline_id === id)?.pipeline_name ?? id.slice(0, 8);

  const bestOf = (vals: number[], lower = false) => {
    const target = lower ? Math.min(...vals) : Math.max(...vals);
    return vals.every((v) => v === target) ? null : target;
  };

  return (
    <div className="page-pad eval ev-run">
      <header className="prov-detail__head">
        {back}
        <div className="prov-detail__title">
          <h1>
            Run on{" "}
            <Link to={`${base}/eval/datasets/${run.dataset_id}`} className="mono strong-link">
              {run.dataset_name}
            </Link>
          </h1>
          {run.status === "finished" ? <StatusIndicator state="succeeded" label="Finished" /> : run.status === "failed" ? <StatusIndicator state="failed" /> : <StatusIndicator state="running" label="Running" />}
        </div>
        <dl className="prov-facts">
          <div>
            <dt>Questions</dt>
            <dd className="num">{count(items)}</dd>
          </div>
          <div>
            <dt>k</dt>
            <dd className="mono">{run.k_values.join(", ")}</dd>
          </div>
          <div>
            <dt>Started</dt>
            <dd>
              <Time iso={run.created_at} />
            </dd>
          </div>
          {run.finished_at ? (
            <div>
              <dt>Finished</dt>
              <dd>
                <Time iso={run.finished_at} />
              </dd>
            </div>
          ) : null}
        </dl>
      </header>

      {run.status === "pending" || run.status === "running" ? (
        <section className="ev-progress" aria-live="polite">
          <Meter value={run.progress.total ? run.progress.done / run.progress.total : 0} tone="warn" label={`${count(run.progress.done)} of ${count(run.progress.total)} queries`} />
          <p className="muted">Every question is asked of every pipeline. The comparison appears when all {count(run.progress.total)} queries are done.</p>
        </section>
      ) : run.status === "failed" ? (
        <ErrorState error={new Error("The evaluation run failed. Check the workers and providers, then start a new run.")} title="The run failed" />
      ) : (
        <>
          {s.length > 2 ? (
            <div className="ev-baseline">
              <Field label="Compare the others against">
                {(a11y) => (
                  <Select {...a11y} value={baseline} onChange={(e) => setParams({ base: e.target.value }, { replace: true })}>
                    {s.map((x) => (
                      <option key={x.pipeline_id} value={x.pipeline_id}>
                        {x.pipeline_name}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            </div>
          ) : null}

          <section className="ev-summary" aria-label="Summary">
            {others.map((o) => (
              <p key={o.pipeline_id}>{summary(run, baseline, o.pipeline_id, items)}</p>
            ))}
          </section>

          <div className="panel ev-tablewrap">
            <table className="rc-table ev-table">
              <caption className="sr-only">Metrics per pipeline{pair ? `, with the difference ${name(b!)} minus ${name(a)} and its 95% confidence interval` : ""}. The best value in each row is marked.</caption>
              <thead>
                <tr>
                  <th>Metric</th>
                  {s.map((x) => (
                    <th key={x.pipeline_id} className="rc-right ev-col-pl">
                      <span className="ev-plhead">
                        <Link to={`${base}/pipelines/${x.pipeline_id}`} className="mono strong-link">
                          {x.pipeline_name}
                        </Link>
                        <Fingerprint value={x.pipeline_fingerprint} length={8} label={`${x.pipeline_name} fingerprint at run time`} />
                        {x.pipeline_archived ? (
                          <span className="ev-archived">
                            <TriangleAlert size={12} aria-hidden /> archived since
                          </span>
                        ) : null}
                        {s.length > 2 && x.pipeline_id === baseline ? <span className="muted">baseline</span> : null}
                      </span>
                    </th>
                  ))}
                  {pair ? (
                    <>
                      <th className="rc-right">Diff (b − a)</th>
                      <th>95% CI</th>
                      <th>Significant</th>
                    </>
                  ) : null}
                  <th className="rc-col-tight">
                    <span className="sr-only">Questions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((m) => {
                  const vals = s.map((x) => x.metrics[m] ?? 0);
                  const best = bestOf(vals);
                  const c = pair ? comparisonFor(run, a, b!, m) : null;
                  return (
                    <tr key={m} className={c?.significant ? "ev-row--sig" : undefined}>
                      <th scope="row" className="mono ev-metric">
                        {m}
                      </th>
                      {s.map((x, i) => {
                        const vs = !pair && x.pipeline_id !== baseline ? comparisonFor(run, baseline, x.pipeline_id, m) : null;
                        return (
                          <td key={x.pipeline_id} className="rc-right num mono">
                            <span className={vals[i] === best ? "ev-bestval" : undefined}>
                              {vals[i] === best ? <Best /> : null}
                              {fmt(vals[i]!)}
                            </span>
                            {vs ? (
                              <span className={`ev-vs ${vs.significant ? (vs.diff > 0 ? "ev-vs--up" : "ev-vs--down") : ""}`}>
                                {signed(vs.diff)}
                                {vs.significant ? " *" : ""}
                              </span>
                            ) : null}
                          </td>
                        );
                      })}
                      {pair && c ? (
                        <>
                          <td className={`rc-right num mono ${c.significant ? (c.diff > 0 ? "ev-up" : "ev-down") : ""}`}>{signed(c.diff)}</td>
                          <td className="num mono muted nowrap">
                            {signed(c.ci_low)} to {signed(c.ci_high)}
                          </td>
                          <td>{c.significant ? <span className="ev-sig">yes{c.diff > 0 ? `, b better` : `, a better`}</span> : <span className="muted">no</span>}</td>
                        </>
                      ) : pair ? (
                        <td colSpan={3} />
                      ) : null}
                      <td className="rc-col-tight">
                        <Link
                          to={`${base}/eval/runs/${run.id}/items?a=${a}&b=${b ?? others[0]?.pipeline_id ?? ""}&metric=${encodeURIComponent(m)}`}
                          className="ev-items"
                          aria-label={`Questions where ${m} differs`}
                          title={`Questions where ${m} differs`}
                        >
                          <ListFilter size={14} aria-hidden />
                        </Link>
                      </td>
                    </tr>
                  );
                })}
                {COSTS.map((row, ri) => {
                  const vals = s.map(row.value);
                  const best = row.lowerIsBetter !== undefined ? bestOf(vals, row.lowerIsBetter) : null;
                  return (
                    <tr key={row.label} className={ri === 0 ? "ev-cost ev-cost--first" : "ev-cost"}>
                      <th scope="row" className="ev-metric">
                        {row.label}
                      </th>
                      {vals.map((v, i) => (
                        <td key={s[i]!.pipeline_id} className="rc-right num mono">
                          <span className={v === best ? "ev-bestval" : undefined}>
                            {v === best ? <Best /> : null}
                            {row.show(v)}
                          </span>
                        </td>
                      ))}
                      {pair ? <td colSpan={3} /> : null}
                      <td />
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="muted ev-legend">
            Differences come from a paired bootstrap over the {count(items)} questions (2,000 resamples). “Significant” means the 95% interval excludes zero.
            A quality gain usually costs something: compare the latency and chunk rows too.
          </p>

          <section className="ev-links" aria-labelledby="pairs-title">
            <h2 id="pairs-title">Look closer</h2>
            <ul>
              {others.map((o) => (
                <li key={o.pipeline_id}>
                  <span className="mono">{name(baseline)}</span> vs <span className="mono">{o.pipeline_name}</span>:{" "}
                  <Link to={`${base}/eval/runs/${run.id}/items?a=${baseline}&b=${o.pipeline_id}&metric=mrr`} className="inline-link">
                    questions that changed
                  </Link>{" "}
                  ·{" "}
                  <Link to={`${base}/pipelines/compare?a=${baseline}&b=${o.pipeline_id}`} className="inline-link">
                    <GitCompareArrows size={12} aria-hidden /> configuration diff
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
