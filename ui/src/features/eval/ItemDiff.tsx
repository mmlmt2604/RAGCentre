import { ArrowLeft, Check, ChevronDown, ChevronRight, X } from "lucide-react";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useEvalRun, usePipelines, useRunItems } from "../../api/hooks";
import type { EvalItemComparison, EvalItemResult } from "../../api/types";
import { Field, Select } from "../../components/Field";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { Badge } from "../../components/Status";
import { Tabs } from "../../components/Tabs";
import { count } from "../../lib/format";
import { fmt, metricOrder, signed } from "./model";

type Outcome = "improved" | "regressed" | "unchanged";

function ResultList({ label, name, result, targets }: { label: string; name: string; result: EvalItemResult; targets: number }) {
  return (
    <section className="id-col" aria-label={`${name} results`}>
      <header className="id-col__head">
        <Badge>{label}</Badge> <span className="mono strong">{name}</span>
        {result.latency_ms !== null ? <span className="muted num">{Math.round(result.latency_ms)} ms</span> : null}
      </header>
      {result.error ? (
        <p className="id-error mono">{result.error}</p>
      ) : !result.retrieved.length ? (
        <p className="muted">Nothing retrieved.</p>
      ) : (
        <ol className="id-list">
          {result.retrieved.map((r) => {
            const hit = r.matched_targets.length > 0;
            return (
              <li key={r.chunk_id} className={`id-res ${hit ? "id-res--hit" : ""}`}>
                <span className="id-res__mark" aria-label={hit ? `matches target ${r.matched_targets.map((t) => t + 1).join(", ")}` : "matches no target"}>
                  {hit ? <Check size={13} strokeWidth={2.5} aria-hidden /> : <X size={13} strokeWidth={2} aria-hidden />}
                </span>
                <span className="id-res__body">
                  <span className="id-res__meta">
                    <span className="mono id-res__rank">#{r.rank}</span>
                    <span className="mono">{r.document_label}</span>
                    {r.page_start !== null ? <span className="muted num">p.{r.page_start}{r.page_end !== r.page_start ? `–${r.page_end}` : ""}</span> : null}
                    {hit ? <span className="id-res__target">target {r.matched_targets.map((t) => t + 1).join(", ")}{targets > 1 ? ` of ${targets}` : ""}</span> : null}
                  </span>
                  <span className="id-res__text">{r.text.length > 260 ? `${r.text.slice(0, 260).trimEnd()}…` : r.text}</span>
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function ItemRow({ c, metric, aName, bName, corpusId }: { c: EvalItemComparison; metric: string; aName: string; bName: string; corpusId: string }) {
  const [open, setOpen] = useState(false);
  const va = c.a.metrics[metric] ?? 0;
  const vb = c.b.metrics[metric] ?? 0;
  return (
    <li className={`id-item id-item--${c.outcome}`}>
      <button type="button" className="id-item__head" aria-expanded={open} onClick={() => setOpen((x) => !x)}>
        {open ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
        <span className="id-item__q">{c.item.question}</span>
        <span className="id-item__vals num mono">
          {fmt(va)} → {fmt(vb)} <span className={`id-delta id-delta--${c.outcome}`}>{signed(vb - va)}</span>
        </span>
      </button>
      {open ? (
        <div className="id-item__body">
          <div className="id-targets">
            <span className="muted">Targets:</span>
            {c.item.targets.map((t, i) => (
              <span key={i} className="id-target">
                <span className="muted">{i + 1}.</span>{" "}
                <Link to={`/corpora/${corpusId}/documents/${t.document_id}`} className="mono strong-link">
                  {t.document_label}
                </Link>
                {t.pages ? <span className="muted"> p.{t.pages.join(", ")}</span> : null}
                {t.text_contains ? <span className="ds-snippet"> “{t.text_contains}”</span> : null}
              </span>
            ))}
            {c.item.filter ? <span className="mono muted">filter {JSON.stringify(c.item.filter)}</span> : null}
          </div>
          <div className="id-cols">
            <ResultList label="a" name={aName} result={c.a} targets={c.item.targets.length} />
            <ResultList label="b" name={bName} result={c.b} targets={c.item.targets.length} />
          </div>
        </div>
      ) : null}
    </li>
  );
}

/** For a pair of pipelines and one metric: which questions got better, worse, or stayed the same. */
export function ItemDiff() {
  const { corpusId = "", runId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const run = useEvalRun(runId);
  const pipelines = usePipelines(corpusId);
  const ids = run.data?.pipeline_ids ?? [];
  const a = params.get("a") ?? ids[0] ?? null;
  const b = params.get("b") ?? ids[1] ?? null;
  const metric = params.get("metric") ?? "mrr";
  const tab = (params.get("show") as Outcome | null) ?? "improved";
  const all = useRunItems(runId, a, b, metric, null);
  const set = (k: string, v: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(k, v);
        return next;
      },
      { replace: true },
    );
  // Names from the corpus's pipeline list: a run's own summaries are empty while it is pending.
  const name = (id: string | null) => pipelines.data?.items.find((p) => p.id === id)?.name ?? id?.slice(0, 8) ?? "…";
  const metrics = metricOrder(Object.keys(run.data?.summaries[0]?.metrics ?? {}));
  const list = all.data?.items ?? [];
  const by = (o: Outcome) => list.filter((x) => x.outcome === o);
  const shown = by(tab);

  return (
    <div className="page-pad eval id">
      <header className="page-head">
        <div>
          <Link to={`/corpora/${corpusId}/eval/runs/${runId}`} className="back-link">
            <ArrowLeft size={14} aria-hidden /> Run on {run.data?.dataset_name ?? "…"}
          </Link>
          <h1>Questions that changed</h1>
        </div>
      </header>
      {run.isError ? <ErrorState error={run.error} onRetry={() => void run.refetch()} /> : null}
      <div className="diff-pickers">
        <Field label="a">
          {(a11y) => (
            <Select {...a11y} value={a ?? ""} onChange={(e) => set("a", e.target.value)}>
              {ids.map((id) => (
                <option key={id} value={id} disabled={id === b}>
                  {name(id)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="b">
          {(a11y) => (
            <Select {...a11y} value={b ?? ""} onChange={(e) => set("b", e.target.value)}>
              {ids.map((id) => (
                <option key={id} value={id} disabled={id === a}>
                  {name(id)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Metric">
          {(a11y) => (
            <Select {...a11y} value={metric} onChange={(e) => set("metric", e.target.value)}>
              {metrics.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </Select>
          )}
        </Field>
      </div>
      {all.isError ? (
        <ErrorState error={all.error} onRetry={() => void all.refetch()} />
      ) : !all.data && run.data && run.data.status !== "finished" ? (
        <EmptyState title="The run is not finished">
          <p>Questions can be compared once every query has run.</p>
        </EmptyState>
      ) : all.data ? (
        <>
          <Tabs<Outcome>
            label="Outcome"
            value={tab}
            onChange={(v) => set("show", v)}
            items={[
              { value: "improved", label: `Better in ${name(b)}`, count: by("improved").length },
              { value: "regressed", label: `Worse in ${name(b)}`, count: by("regressed").length },
              { value: "unchanged", label: "Unchanged", count: by("unchanged").length },
            ]}
          />
          {shown.length ? (
            <>
              <p className="muted">
                {count(shown.length)} of {count(list.length)} questions. Open one to see both result lists, with each retrieved chunk marked as matching a target or not.
              </p>
              <ol className="id-items">
                {shown.map((c) => (
                  <ItemRow key={c.item.id} c={c} metric={metric} aName={name(a)} bName={name(b)} corpusId={corpusId} />
                ))}
              </ol>
            </>
          ) : (
            <EmptyState title={tab === "unchanged" ? "No unchanged questions" : `No question is ${tab === "improved" ? "better" : "worse"} in ${name(b)}`}>
              <p>On {metric}, for this pair.</p>
            </EmptyState>
          )}
        </>
      ) : (
        <Skeleton width="100%" height={240} />
      )}
    </div>
  );
}
