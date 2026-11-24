import { History, Search, SlidersHorizontal, X } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { useParams, useSearchParams } from "react-router";
import { useCorpus, usePipelines, useQueryResults } from "../../api/hooks";
import type { QueryRequest, QueryResponse } from "../../api/types";
import { Button, ButtonLink } from "../../components/Button";
import { Checkbox, Field, Select } from "../../components/Field";
import { NumberInput } from "../../components/NumberInput";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { FilterBuilder, type FilterState } from "./FilterBuilder";
import { forgetQueries, recentQueries, rememberQuery } from "./recent";
import { ResultColumn, type Twin } from "./ResultColumn";

function parseFilter(raw: string | null): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as unknown;
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function twinsFrom(other: QueryResponse | undefined): Map<string, Twin> | null {
  if (!other) return null;
  const m = new Map<string, Twin>();
  for (const r of other.results) {
    m.set(r.chunk_id, { rank: r.rank, sameDocRank: r.rank });
    if (!m.has(`doc:${r.document_id}`)) m.set(`doc:${r.document_id}`, { rank: null, sameDocRank: r.rank });
  }
  return m;
}

/** Single or side-by-side querying. Everything a user would share lives in the URL (?q=&a=&b=). */
export function QueryPlayground() {
  const { corpusId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const corpus = useCorpus(corpusId);
  const pipelines = usePipelines(corpusId);
  const items = pipelines.data?.items ?? [];
  const activeId = corpus.data?.active_pipeline_id ?? null;

  // What was submitted (from the URL)
  const sq = params.get("q") ?? "";
  const sa = params.get("a") ?? "";
  const sb = params.get("b") ?? "";
  const sk = params.get("k") ? Number(params.get("k")) : undefined;
  const sf = parseFilter(params.get("f"));
  const spartial = params.get("partial") === "1";
  const sskip = params.get("nt") === "1";
  const sdebug = params.get("dbg") === "1";

  // The form (starts from the URL)
  const [text, setText] = useState(sq);
  const [a, setA] = useState(sa);
  const [b, setB] = useState(sb);
  const [k, setK] = useState<number | undefined>(sk);
  const [filter, setFilter] = useState<FilterState>({ filter: sf, error: null });
  const [showFilter, setShowFilter] = useState(Boolean(sf));
  const [partial, setPartial] = useState(spartial);
  const [skip, setSkip] = useState(sskip);
  const [debug, setDebug] = useState(sdebug);
  const [recent, setRecent] = useState(() => recentQueries(corpusId));
  const [hovered, setHovered] = useState<string | null>(null);

  const effA = sa || activeId || items[0]?.id || "";
  const base = (pipelineId: string): QueryRequest | null =>
    sq.trim() && pipelineId
      ? {
          query: sq,
          pipeline_id: pipelineId,
          ...(sk ? { top_k: sk } : {}),
          ...(sf ? { filter: sf } : {}),
          allow_partial: spartial,
          ...(sskip ? { transform: false } : {}),
          ...(sdebug ? { debug: true } : {}),
        }
      : null;
  const resA = useQueryResults(corpusId, base(effA));
  const resB = useQueryResults(corpusId, sb ? base(sb) : null);
  const twinsA = useMemo(() => (sb ? twinsFrom(resB.data) : null), [sb, resB.data]);
  const twinsB = useMemo(() => (sb ? twinsFrom(resA.data) : null), [sb, resA.data]);

  const submit = (e?: FormEvent, override?: Partial<{ q: string; partial: boolean }>) => {
    e?.preventDefault();
    const q = (override?.q ?? text).trim();
    if (!q || filter.error) return;
    const next = new URLSearchParams();
    next.set("q", q);
    if (a) next.set("a", a);
    if (b) next.set("b", b);
    if (k) next.set("k", String(k));
    if (filter.filter) next.set("f", JSON.stringify(filter.filter));
    if (override?.partial ?? partial) next.set("partial", "1");
    if (skip) next.set("nt", "1");
    if (debug) next.set("dbg", "1");
    if (next.toString() === params.toString()) {
      void resA.refetch();
      if (sb) void resB.refetch();
    } else setParams(next);
    setRecent(rememberQuery(corpusId, q));
  };

  if (corpus.isError) return <div className="page-pad"><ErrorState error={corpus.error} onRetry={() => void corpus.refetch()} /></div>;
  if (pipelines.isError) return <div className="page-pad"><ErrorState error={pipelines.error} onRetry={() => void pipelines.refetch()} /></div>;
  if (pipelines.data && !items.length) {
    return (
      <div className="page-pad">
        <h1>Query</h1>
        <EmptyState icon={Search} title="No pipelines to query" action={<ButtonLink to={`/corpora/${corpusId}/pipelines/new`} variant="primary">New pipeline</ButtonLink>}>
          <p>Queries run through a pipeline's indexes. Build one first; it can share work with any later pipeline.</p>
        </EmptyState>
      </div>
    );
  }
  const name = (id: string) => items.find((p) => p.id === id);
  const transforms = [effA, sb].some((id) => Boolean(name(id)?.config.retrieval.transform));
  const pipelineOptions = items.map((p) => (
    <option key={p.id} value={p.id}>
      {p.name}
      {p.id === activeId ? " (active)" : ""}
      {p.status !== "ready" ? ` · ${p.status}` : ""}
    </option>
  ));

  return (
    <div className="page-pad qp">
      <form className="qform" onSubmit={submit} role="search" aria-label="Query">
        <div className="qform__main">
          <label className="sr-only" htmlFor="q">
            Query
          </label>
          <input id="q" className="rc-input qform__q" value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask the corpus, e.g. how long do refunds take?" autoComplete="off" />
          <Button type="submit" variant="primary" icon={Search} disabled={!text.trim() || Boolean(filter.error)}>
            Search
          </Button>
        </div>
        <div className="qform__opts">
          <Field label="Pipeline">
            {(a11y) => (
              <Select {...a11y} value={a || effA} onChange={(e) => setA(e.target.value)}>
                {!pipelines.data ? <option>Loading…</option> : pipelineOptions}
              </Select>
            )}
          </Field>
          <Field label="Side by side with">
            {(a11y) => (
              <Select {...a11y} value={b} onChange={(e) => setB(e.target.value)}>
                <option value="">Nothing</option>
                {pipelineOptions}
              </Select>
            )}
          </Field>
          <div className="qform__k">
            <NumberInput label="Results" min={1} max={1000} placeholder="pipeline's top_k" value={k} onChange={setK} />
          </div>
          <Button variant="ghost" icon={SlidersHorizontal} aria-expanded={showFilter} onClick={() => setShowFilter((x) => !x)}>
            Filter{filter.filter ? " (on)" : ""}
          </Button>
          <Checkbox checked={partial} onChange={(e) => setPartial(e.target.checked)} label="Allow partial indexes" />
          {transforms ? <Checkbox checked={skip} onChange={(e) => setSkip(e.target.checked)} label="Skip query transform" /> : null}
          {transforms ? <Checkbox checked={debug} onChange={(e) => setDebug(e.target.checked)} label="Show which query found each result" /> : null}
        </div>
        {showFilter && corpus.data ? (
          <div className="qform__filter">
            <FilterBuilder schema={corpus.data.metadata_schema} initial={sf} onChange={setFilter} />
          </div>
        ) : null}
        {recent.length ? (
          <div className="qform__recent">
            <History size={13} aria-hidden className="muted" />
            <span className="sr-only">Recent queries:</span>
            {recent.map((q) => (
              <button
                key={q}
                type="button"
                className="qchip"
                onClick={() => {
                  setText(q);
                  submit(undefined, { q });
                }}
              >
                {q}
              </button>
            ))}
            <button
              type="button"
              className="link-btn qform__clear"
              onClick={() => {
                forgetQueries(corpusId);
                setRecent([]);
              }}
            >
              <X size={12} aria-hidden /> Clear
            </button>
          </div>
        ) : null}
      </form>

      {!sq ? (
        <EmptyState icon={Search} title="Try a query">
          <p>Results show each chunk's rank, score and where it came from. Choose a second pipeline to see both result lists side by side, with shared chunks linked.</p>
        </EmptyState>
      ) : !pipelines.data ? (
        <Skeleton width="100%" height={300} />
      ) : (
        <div className={`qcols ${sb ? "qcols--two" : ""}`}>
          <ResultColumn
            label={sb ? "a" : null}
            pipeline={name(effA)}
            data={resA.data}
            error={resA.error}
            loading={resA.isFetching}
            corpusId={corpusId}
            twins={twinsA}
            otherLabel="b"
            hovered={hovered}
            onHover={setHovered}
            onQueryAnyway={() => {
              setPartial(true);
              submit(undefined, { q: sq, partial: true });
            }}
          />
          {sb ? (
            <ResultColumn
              label="b"
              pipeline={name(sb)}
              data={resB.data}
              error={resB.error}
              loading={resB.isFetching}
              corpusId={corpusId}
              twins={twinsB}
              otherLabel="a"
              hovered={hovered}
              onHover={setHovered}
              onQueryAnyway={() => {
                setPartial(true);
                submit(undefined, { q: sq, partial: true });
              }}
            />
          ) : null}
        </div>
      )}
    </div>
  );
}
