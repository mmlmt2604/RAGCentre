import { ArrowLeft, ArrowRight, FlaskConical, GitCompareArrows, Search } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";
import { usePipeline, usePipelineDiff, usePipelines } from "../../api/hooks";
import type { ConfigChange, Pipeline, PipelineDiff as Diff } from "../../api/types";
import { ButtonLink } from "../../components/Button";
import { Field, Select } from "../../components/Field";
import { Fingerprint } from "../../components/Fingerprint";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { Badge } from "../../components/Status";
import { inputsOf, nodeSummary, summariseDiff } from "./graph";
import { NODE_LABEL } from "./model";
import { PipelineGraph, type GraphNode } from "./PipelineGraph";

function show(v: unknown): string {
  if (v === null || v === undefined) return "—";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function graphFor(p: Pipeline, d: Diff, side: "a" | "b"): GraphNode[] {
  return p.config.nodes.map((n) => {
    const row = d.nodes.find((x) => x.node_id === n.id);
    const only = row && !(row.in_a && row.in_b);
    const ref = p.nodes.find((x) => x.node_id === n.id);
    return {
      id: n.id,
      type: n.type,
      inputs: inputsOf(n),
      summary: nodeSummary(n),
      tone: only ? "dim" : row?.same ? "same" : "changed",
      extra: (
        <>
          {ref ? <Fingerprint value={ref.node_fp} length={6} label={`${n.id} in ${p.name}`} /> : null}
          <span className={`pg__mark pg__mark--${only ? "only" : row?.same ? "same" : "changed"}`}>{only ? `only in ${side}` : row?.same ? "identical" : "changed"}</span>
        </>
      ),
    };
  });
}

export function PipelineDiff() {
  const { corpusId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const a = params.get("a");
  const b = params.get("b");
  const list = usePipelines(corpusId);
  const pa = usePipeline(a ?? undefined);
  const pb = usePipeline(b ?? undefined);
  const diff = usePipelineDiff(a, b);
  const items = list.data?.items ?? [];
  const pick = (key: "a" | "b", id: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (id) next.set(key, id);
        else next.delete(key);
        return next;
      },
      { replace: true },
    );
  const base = `/corpora/${corpusId}`;

  return (
    <div className="page-pad pl-diff">
      <header className="page-head">
        <div>
          <Link to={`${base}/pipelines`} className="back-link">
            <ArrowLeft size={14} aria-hidden /> Pipelines
          </Link>
          <h1>Compare pipelines</h1>
        </div>
      </header>
      <div className="diff-pickers">
        <Field label="Pipeline a">
          {(a11y) => (
            <Select {...a11y} value={a ?? ""} onChange={(e) => pick("a", e.target.value)}>
              <option value="">Choose…</option>
              {items.map((p) => (
                <option key={p.id} value={p.id} disabled={p.id === b}>
                  {p.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <ArrowRight size={16} aria-hidden className="diff-pickers__arrow" />
        <Field label="Pipeline b">
          {(a11y) => (
            <Select {...a11y} value={b ?? ""} onChange={(e) => pick("b", e.target.value)}>
              <option value="">Choose…</option>
              {items.map((p) => (
                <option key={p.id} value={p.id} disabled={p.id === a}>
                  {p.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        {a && b ? (
          <span className="diff-pickers__links">
            <ButtonLink to={`${base}/query?a=${a}&b=${b}`} icon={Search}>
              Query both
            </ButtonLink>
            <ButtonLink to={`${base}/eval?compare=${a},${b}`} icon={FlaskConical}>
              Evaluate both
            </ButtonLink>
          </span>
        ) : null}
      </div>

      {!a || !b ? (
        <EmptyState icon={GitCompareArrows} title="Choose two pipelines">
          <p>See which steps they share and which settings differ. Shared steps are computed once; only the differing ones cost extra work.</p>
        </EmptyState>
      ) : diff.isError ? (
        <ErrorState error={diff.error} onRetry={() => void diff.refetch()} />
      ) : !diff.data || !pa.data || !pb.data ? (
        <Skeleton width="100%" height={300} />
      ) : (
        <>
          <p className="diff-summary" aria-live="polite">
            {summariseDiff(diff.data)}
          </p>
          <div className="diff-graphs">
            <section aria-label={`Pipeline a: ${pa.data.name}`}>
              <h2 className="diff-graphs__title">
                <Badge>a</Badge> <span className="mono">{pa.data.name}</span>
              </h2>
              <PipelineGraph label={`${pa.data.name} steps`} nodes={graphFor(pa.data, diff.data, "a")} />
            </section>
            <section aria-label={`Pipeline b: ${pb.data.name}`}>
              <h2 className="diff-graphs__title">
                <Badge>b</Badge> <span className="mono">{pb.data.name}</span>
              </h2>
              <PipelineGraph label={`${pb.data.name} steps`} nodes={graphFor(pb.data, diff.data, "b")} />
            </section>
          </div>

          <section className="prov-section" aria-labelledby="changes-title">
            <h2 id="changes-title">Steps, paired by node id</h2>
            <div className="panel">
              <table className="rc-table diff-table">
                <thead>
                  <tr>
                    <th>Node</th>
                    <th>Result</th>
                    <th>Setting</th>
                    <th>a</th>
                    <th>b</th>
                  </tr>
                </thead>
                <tbody>
                  {diff.data.nodes.map((n) => {
                    const rows: (ConfigChange | null)[] = n.changes.length ? n.changes : [null];
                    const verdict = !n.in_a ? "only in b" : !n.in_b ? "only in a" : n.same ? "identical" : "changed";
                    return rows.map((c, i) => (
                      <tr key={`${n.node_id}-${i}`} className={`diff-row diff-row--${n.same ? "same" : "changed"}`}>
                        {i === 0 ? (
                          <>
                            <td rowSpan={rows.length}>
                              <span className="mono">{n.node_id}</span> <span className="muted">{NODE_LABEL[n.type]}</span>
                            </td>
                            <td rowSpan={rows.length}>
                              <span className={`pg__mark pg__mark--${n.same ? "same" : n.in_a && n.in_b ? "changed" : "only"}`}>{verdict}</span>
                            </td>
                          </>
                        ) : null}
                        {c ? (
                          <>
                            <td className="mono">{c.path}</td>
                            <td className="mono diff-a">{show(c.a)}</td>
                            <td className="mono diff-b">{show(c.b)}</td>
                          </>
                        ) : (
                          <td colSpan={3} className="muted">
                            {n.same ? "Same fingerprint: the work is shared." : n.in_a && n.in_b ? "Same settings, but a step before it differs." : "—"}
                          </td>
                        )}
                      </tr>
                    ));
                  })}
                  {diff.data.retrieval_changes.length ? (
                    diff.data.retrieval_changes.map((c, i) => (
                      <tr key={`r-${c.path}`} className="diff-row diff-row--changed">
                        {i === 0 ? (
                          <>
                            <td rowSpan={diff.data!.retrieval_changes.length}>Retrieval</td>
                            <td rowSpan={diff.data!.retrieval_changes.length}>
                              <span className="pg__mark pg__mark--changed">changed</span>
                            </td>
                          </>
                        ) : null}
                        <td className="mono">{c.path}</td>
                        <td className="mono diff-a">{show(c.a)}</td>
                        <td className="mono diff-b">{show(c.b)}</td>
                      </tr>
                    ))
                  ) : (
                    <tr className="diff-row diff-row--same">
                      <td>Retrieval</td>
                      <td>
                        <span className="pg__mark pg__mark--same">identical</span>
                      </td>
                      <td colSpan={3} className="muted">
                        Same sources, fusion, reranking and top_k.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
