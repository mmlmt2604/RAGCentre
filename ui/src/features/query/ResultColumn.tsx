import { ArrowDown, ArrowUp, Link2, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../../api/client";
import type { Pipeline, QueryResponse, QueryResult } from "../../api/types";
import { Button } from "../../components/Button";
import { Fingerprint } from "../../components/Fingerprint";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { Badge, StatusIndicator } from "../../components/Status";
import { duration, percent } from "../../lib/format";

const STAGES = ["transform", "embed", "fusion", "rerank"];

const PROBE_LABEL: Record<string, string> = { original: "original", rewrite: "rewrite", paraphrase: "paraphrase", hypothetical: "passage", sub_question: "sub-question" };

function pages(r: QueryResult): string | null {
  if (r.page_start === null) return null;
  return r.page_end === null || r.page_end === r.page_start ? `p.${r.page_start}` : `pp.${r.page_start}–${r.page_end}`;
}

const CLAMP = 420;

/** Chunk text, shortened to a few lines with a toggle for the rest. */
function ResultText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > CLAMP;
  return (
    <>
      <p className="qres__text">{long && !open ? `${text.slice(0, CLAMP).trimEnd()}…` : text}</p>
      {long ? (
        <button type="button" className="link-btn qres__more" aria-expanded={open} onClick={() => setOpen((x) => !x)}>
          {open ? "Show less" : `Show full chunk (${text.length.toLocaleString("en")} chars)`}
        </button>
      ) : null}
    </>
  );
}

/** The other column's view of a result: same chunk (with its rank) or only the same document. */
export interface Twin {
  rank: number | null;
  sameDocRank: number | null;
}

/** One pipeline's results, its timings and its errors. In side-by-side mode, shared chunks are linked. */
export function ResultColumn({
  label,
  pipeline,
  data,
  error,
  loading,
  corpusId,
  twins,
  otherLabel,
  hovered,
  onHover,
  onQueryAnyway,
}: {
  label: string | null;
  pipeline: Pipeline | undefined;
  data: QueryResponse | undefined;
  error: unknown;
  loading: boolean;
  corpusId: string;
  twins: Map<string, Twin> | null;
  otherLabel: string;
  hovered: string | null;
  onHover: (chunkId: string | null) => void;
  onQueryAnyway: () => void;
}) {
  const api = error instanceof ApiError ? error : null;
  const coverage = typeof api?.details?.coverage === "number" ? api.details.coverage : null;
  const sourceIds = pipeline?.config.retrieval.sources.map((s) => s.id) ?? [];
  return (
    <section className="qcol" aria-label={`Results from ${pipeline?.name ?? "pipeline"}`} aria-busy={loading || undefined}>
      <header className="qcol__head">
        {label ? <Badge>{label}</Badge> : null}
        <span className="mono strong">{pipeline?.name ?? "…"}</span>
        {pipeline ? <StatusIndicator state={pipeline.status} compact /> : null}
        {data ? <Fingerprint value={data.pipeline_fingerprint} length={8} label={`${pipeline?.name} fingerprint at query time`} /> : null}
        {data?.partial ? (
          <span className="qcol__partial">
            <TriangleAlert size={13} strokeWidth={2} aria-hidden /> Partial: the index is still {pipeline?.status ?? "incomplete"}
          </span>
        ) : null}
      </header>
      {data ? (
        <p className="qcol__timings num" aria-label="Timings">
          {[...STAGES.filter((k) => k in data.timings_ms), ...sourceIds.filter((k) => k in data.timings_ms)].map((k) => (
            <span key={k}>
              <span className="muted">{k}</span> {duration(data.timings_ms[k]!)}
            </span>
          ))}
          <span>
            <span className="muted">total</span> <strong>{duration(data.timings_ms.total ?? 0)}</strong>
          </span>
        </p>
      ) : null}

      {data?.transform ? (
        <div className="qprobes" aria-label="Query transform">
          <p className="qprobes__head">
            <span className="strong">{data.transform.mode.replace("_", "-")}</span>
            <span className="muted">{data.transform.probes.length === 1 ? "searched the query as asked" : `searched ${data.transform.probes.length} queries`}</span>
            {data.transform.cached ? <Badge>cached</Badge> : null}
          </p>
          <ol className="qprobes__list">
            {data.transform.probes.map((pr) => (
              <li key={pr.index} className="qprobes__item">
                <span className="qprobes__kind">{PROBE_LABEL[pr.kind] ?? pr.kind}</span>
                <span className="mono">{pr.text}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {loading ? (
        <div className="qlist">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} width="100%" height={92} />
          ))}
        </div>
      ) : api?.code === "index_incomplete" ? (
        <div className="qerr" role="alert">
          <h3>{pipeline?.name ?? "This pipeline"} is still {String(api.details?.status ?? "building")}</h3>
          <p>
            {coverage !== null ? <>Only {percent(coverage)} of documents are searchable through it. </> : null}
            Results would miss the rest. Wait for it to finish, or query it anyway and see partial results.
          </p>
          <div className="form-actions">
            <Button onClick={onQueryAnyway}>Query anyway</Button>
            {pipeline ? (
              <Link to={`/corpora/${corpusId}/pipelines/${pipeline.id}`} className="inline-link">
                See its progress
              </Link>
            ) : null}
          </div>
        </div>
      ) : error ? (
        <ErrorState error={error} title={api?.code === "pipeline_archived" ? "This pipeline is archived" : api?.code === "invalid_filter" ? "The filter is not valid" : undefined} />
      ) : !data ? null : data.results.length === 0 ? (
        <EmptyState title="No results">
          <p>Nothing matched{data.partial ? " in the part of the corpus indexed so far" : ""}. Try other words, or loosen the filter.</p>
        </EmptyState>
      ) : (
        <ol className="qlist">
          {data.results.map((r) => {
            const twin = twins?.get(r.chunk_id) ?? twins?.get(`doc:${r.document_id}`) ?? null;
            const shared = twin?.rank !== null && twin?.rank !== undefined;
            const delta = shared ? twin!.rank! - r.rank : 0;
            return (
              <li
                key={r.chunk_id}
                className="qres"
                data-hot={hovered === r.chunk_id || undefined}
                data-shared={shared || undefined}
                onMouseEnter={() => shared && onHover(r.chunk_id)}
                onMouseLeave={() => shared && onHover(null)}
              >
                <div className="qres__meta">
                  <span className="qres__rank mono">#{r.rank}</span>
                  <span className="qres__score num">{r.score.toFixed(4)}</span>
                  <Link to={`/corpora/${corpusId}/documents/${r.document_id}`} className="strong-link mono qres__doc" title={r.document.filename}>
                    {r.document.filename}
                  </Link>
                  {pages(r) ? <span className="num muted">{pages(r)}</span> : null}
                  {twins ? (
                    shared ? (
                      <span className="qres__twin" title={`The same chunk is #${twin!.rank} in ${otherLabel}`}>
                        <Link2 size={12} strokeWidth={2} aria-hidden /> {otherLabel} #{twin!.rank}
                        {delta > 0 ? (
                          <span className="qres__delta qres__delta--up">
                            <ArrowUp size={11} aria-hidden />
                            {delta} higher here
                          </span>
                        ) : delta < 0 ? (
                          <span className="qres__delta qres__delta--down">
                            <ArrowDown size={11} aria-hidden />
                            {-delta} lower here
                          </span>
                        ) : (
                          <span className="qres__delta">same rank</span>
                        )}
                      </span>
                    ) : twin?.sameDocRank ? (
                      <span className="qres__twin qres__twin--doc">same document at {otherLabel} #{twin.sameDocRank}</span>
                    ) : (
                      <span className="qres__twin qres__twin--none">not in {otherLabel}</span>
                    )
                  ) : null}
                </div>
                {r.probes ? (
                  <p className="qres__probes num" aria-label="Queries that found this chunk">
                    found by {r.probes.map((i) => `#${i}`).join(", ") || "none"}
                  </p>
                ) : null}
                {r.heading_path.length ? <p className="qres__path">{r.heading_path.join(" › ")}</p> : null}
                {r.context ? (
                  <p className="chunk__context">
                    <span className="chunk__context-label">context</span>
                    {r.context}
                  </p>
                ) : null}
                <ResultText text={r.text} />
                <p className="qres__sources num" aria-label="Rank and score in each source">
                  {Object.entries(r.sources)
                    .sort((a, b) => a[1].rank - b[1].rank)
                    .map(([id, s]) => (
                      <span key={id}>
                        {id} #{s.rank} <span className="muted">{s.score.toFixed(3)}</span>
                      </span>
                    ))}
                </p>
                {r.context_before.length || r.context_after.length ? (
                  <details className="qres__ctx">
                    <summary>Neighbouring chunks ({r.context_before.length + r.context_after.length})</summary>
                    {r.context_before.map((t, i) => (
                      <p key={`b${i}`} className="qres__ctxtext">
                        <span className="muted">before</span> {t}
                      </p>
                    ))}
                    {r.context_after.map((t, i) => (
                      <p key={`a${i}`} className="qres__ctxtext">
                        <span className="muted">after</span> {t}
                      </p>
                    ))}
                  </details>
                ) : null}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
