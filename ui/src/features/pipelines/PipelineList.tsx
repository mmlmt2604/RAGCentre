import { Can } from "../auth/Access";
import { CircleX, GitCompareArrows, Plus, Workflow } from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { useCorpus, usePipelines } from "../../api/hooks";
import type { PipelineState } from "../../api/types";
import { Button, ButtonLink } from "../../components/Button";
import { Checkbox, SearchInput, Segmented } from "../../components/Field";
import { Fingerprint } from "../../components/Fingerprint";
import { EmptyState, ErrorState, SkeletonRows } from "../../components/States";
import { Badge, Meter, StatusIndicator } from "../../components/Status";
import { Tabs } from "../../components/Tabs";
import { Time } from "../../components/Time";
import { count, percent } from "../../lib/format";
import { NODE_LABEL, sortPipelines } from "./model";
import { PipelinePanel } from "./PipelinePanel";

type Filter = "all" | PipelineState;

export function PipelineList() {
  const { corpusId = "" } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const archived = params.get("view") === "archived";
  const filter = (params.get("status") as Filter | null) ?? "all";
  const q = params.get("q") ?? "";
  const [checked, setChecked] = useState<string[]>([]);

  const corpus = useCorpus(corpusId);
  const pipelines = usePipelines(corpusId, archived);
  const archivedList = usePipelines(corpusId, true);

  const all = useMemo(() => sortPipelines(pipelines.data?.items ?? []), [pipelines.data]);
  const counts = useMemo(() => {
    const c = { degraded: 0, building: 0, ready: 0 };
    for (const p of all) c[p.status] += 1;
    return c;
  }, [all]);
  const visible = all.filter((p) => (filter === "all" || p.status === filter) && (!q || p.name.toLowerCase().includes(q.toLowerCase())));

  const selectedId = params.get("selected") ?? visible[0]?.id ?? null;
  const selected = all.find((p) => p.id === selectedId) ?? visible[0] ?? null;
  const docCount = corpus.data?.document_count ?? 0;

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null || value === "" || value === "all") next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const toggleCheck = (id: string) =>
    setChecked((xs) => (xs.includes(id) ? xs.filter((x) => x !== id) : [...xs.slice(-1), id]));

  const onRowKey = (e: KeyboardEvent<HTMLTableRowElement>, index: number) => {
    if (e.target !== e.currentTarget) return;
    const dir = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (dir) {
      e.preventDefault();
      const next = visible[Math.min(visible.length - 1, Math.max(0, index + dir))];
      if (next) {
        setParam("selected", next.id);
        document.getElementById(`pl-row-${next.id}`)?.focus();
      }
    } else if (e.key === "Enter") {
      navigate(`/corpora/${corpusId}/pipelines/${visible[index]!.id}`);
    }
  };

  // Which node fingerprints appear in more than one listed pipeline.
  const fpUse = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const p of all) for (const n of p.nodes) m.set(n.node_fp, [...(m.get(n.node_fp) ?? []), p.name]);
    return m;
  }, [all]);

  const summary = [
    counts.degraded ? `${counts.degraded} degraded` : null,
    counts.building ? `${counts.building} building` : null,
    counts.ready ? `${counts.ready} ready` : null,
  ].filter(Boolean);

  return (
    <div className="pl">
      <section className="pl-main" aria-label="Pipelines">
        <header className="pl-head">
          <div className="pl-head__title">
            <h1>Pipelines</h1>
            {!archived && summary.length ? <p className="pl-head__summary">{summary.join(" · ")}</p> : null}
          </div>
          <span className="pl-head__spacer" />
          <Button
            icon={GitCompareArrows}
            disabled={checked.length !== 2}
            title={checked.length === 2 ? undefined : "Tick two pipelines to compare them"}
            onClick={() => navigate(`/corpora/${corpusId}/pipelines/compare?a=${checked[0]}&b=${checked[1]}`)}
          >
            Compare{checked.length ? ` ${checked.length}/2` : ""}
          </Button>
          <Can capability="edit"><ButtonLink to={`/corpora/${corpusId}/pipelines/new`} variant="primary" icon={Plus}>
            New pipeline
          </ButtonLink></Can>
        </header>

        <div className="pl-toolbar">
          <Tabs
            label="Pipeline list"
            value={archived ? "archived" : "active"}
            onChange={(v) => {
              setChecked([]);
              const next = new URLSearchParams();
              if (v === "archived") next.set("view", "archived");
              setParams(next, { replace: true });
            }}
            items={[
              { value: "active", label: "Active", count: archived ? undefined : all.length },
              { value: "archived", label: "Archived", count: archivedList.data?.items.length },
            ]}
          />
          <span className="pl-toolbar__spacer" />
          <SearchInput aria-label="Filter pipelines by name" placeholder="Filter by name" value={q} onChange={(e) => setParam("q", e.target.value)} className="pl-search" />
          {!archived ? (
            <Segmented<Filter>
              label="Status"
              value={filter}
              onChange={(v) => setParam("status", v)}
              options={[
                { value: "all", label: "All" },
                { value: "degraded", label: "Degraded", count: counts.degraded },
                { value: "building", label: "Building", count: counts.building },
                { value: "ready", label: "Ready", count: counts.ready },
              ]}
            />
          ) : null}
        </div>

        {pipelines.isError && !pipelines.data ? (
          <ErrorState error={pipelines.error} onRetry={() => void pipelines.refetch()} />
        ) : pipelines.data && all.length === 0 ? (
          archived ? (
            <EmptyState icon={Workflow} title="No archived pipelines">
              <p>Archiving a pipeline stops new ingestion for steps no other pipeline uses. Archived pipelines appear here.</p>
            </EmptyState>
          ) : (
            <EmptyState
              icon={Workflow}
              title="No pipelines yet"
              action={
                <Can capability="edit"><ButtonLink to={`/corpora/${corpusId}/pipelines/new`} variant="primary" icon={Plus}>
                  New pipeline
                </ButtonLink></Can>
              }
            >
              <p>A pipeline turns this corpus's documents into searchable indexes: parse, chunk, embed and index, then a retrieval recipe on top.</p>
              <p>Every step is fingerprinted. When two pipelines share a step, it runs once and both use it.</p>
            </EmptyState>
          )
        ) : (
          <div className="pl-tablewrap">
            <table className="rc-table pl-table">
              <caption className="sr-only">Pipelines in this corpus. Use arrow keys to move between rows and Enter to open one.</caption>
              <thead>
                <tr>
                  <th className="pl-gutter" aria-label="Problems" />
                  <th className="rc-col-tight" aria-label="Select for comparison" />
                  <th>Pipeline</th>
                  <th>Status</th>
                  <th>Coverage</th>
                  <th>
                    Steps <span className="pl-steps-hint">parse · chunk · embed · index</span>
                  </th>
                  <th className="pl-col-created">Created</th>
                </tr>
              </thead>
              <tbody>
                {!pipelines.data ? (
                  <SkeletonRows rows={4} cols={[14, 16, 180, 80, 130, 320, 70]} />
                ) : visible.length === 0 ? (
                  <tr>
                    <td colSpan={7}>
                      <div className="pl-nomatch">
                        No pipelines match these filters.{" "}
                        <button type="button" className="link-btn" onClick={() => setParams(new URLSearchParams(archived ? { view: "archived" } : {}), { replace: true })}>
                          Clear filters
                        </button>
                      </div>
                    </td>
                  </tr>
                ) : (
                  visible.map((p, i) => {
                    const done = Math.round(p.coverage * docCount);
                    const isSel = selected?.id === p.id;
                    return (
                      <tr
                        key={p.id}
                        id={`pl-row-${p.id}`}
                        tabIndex={isSel ? 0 : -1}
                        aria-selected={isSel}
                        onClick={() => setParam("selected", p.id)}
                        onKeyDown={(e) => onRowKey(e, i)}
                        className="pl-row"
                      >
                        <td className="pl-gutter">
                          {p.status === "degraded" ? (
                            <CircleX size={14} strokeWidth={2} className="pl-gutter--danger" aria-label="Has failed runs" />
                          ) : null}
                        </td>
                        <td className="rc-col-tight" onClick={(e) => e.stopPropagation()}>
                          <Checkbox aria-label={`Select ${p.name} for comparison`} checked={checked.includes(p.id)} onChange={() => toggleCheck(p.id)} />
                        </td>
                        <td>
                          <div className="pl-name">
                            <span className="pl-name__text">{p.name}</span>
                            {p.is_active ? <Badge tone="accent">active</Badge> : null}
                          </div>
                        </td>
                        <td>
                          <StatusIndicator state={p.status} />
                        </td>
                        <td>
                          <Meter
                            value={p.coverage}
                            tone={p.status === "degraded" ? "danger" : p.status === "building" ? "warn" : "neutral"}
                            label={
                              <>
                                <strong>{percent(p.coverage)}</strong> {count(done)}/{count(docCount)}
                              </>
                            }
                          />
                        </td>
                        <td>
                          <span className="pl-steps">
                            {p.nodes.map((n) => {
                              const users = fpUse.get(n.node_fp) ?? [];
                              const others = users.filter((u) => u !== p.name);
                              return (
                                <Fingerprint
                                  key={n.node_id}
                                  value={n.node_fp}
                                  length={4}
                                  label={`${NODE_LABEL[n.type] ?? n.type} · ${n.node_id}`}
                                  unique={others.length === 0}
                                  note={others.length ? `Shared with ${others.join(", ")}` : "Used only by this pipeline"}
                                />
                              );
                            })}
                          </span>
                        </td>
                        <td className="muted nowrap pl-col-created">
                          <Time iso={p.created_at} compact />
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
            {pipelines.data && visible.length ? (
              <p className="pl-legend">
                <span className="rc-fp" data-slot="3" aria-hidden>
                  7d10
                </span>
                Same token, same step: computed once and shared.
                <span className="rc-fp" data-slot="3" data-unique aria-hidden>
                  4c2e
                </span>
                Dashed: used only by this pipeline.
              </p>
            ) : null}
          </div>
        )}
      </section>

      <PipelinePanel corpusId={corpusId} pipeline={selected} documentCount={docCount} pipelines={all} />
    </div>
  );
}
