import { Layers } from "lucide-react";
import { useChunks } from "../../api/hooks";
import type { Chunk, Document, Pipeline } from "../../api/types";
import { Field, Select } from "../../components/Field";
import { Fingerprint } from "../../components/Fingerprint";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { StatusIndicator } from "../../components/Status";
import { count } from "../../lib/format";

function chunkNode(p: Pipeline | undefined) {
  return p?.nodes.find((n) => n.type === "chunk") ?? null;
}

function pages(c: Chunk): string | null {
  if (c.page_start === null) return null;
  return c.page_end === null || c.page_end === c.page_start ? `p.${c.page_start}` : `pp.${c.page_start}–${c.page_end}`;
}

function ChunkColumn({ doc, pipeline }: { doc: Document; pipeline: Pipeline }) {
  const node = chunkNode(pipeline);
  const chunks = useChunks(doc.id, pipeline.id, node?.node_id ?? null);
  const run = doc.ingestion.find((i) => i.pipeline_id === pipeline.id)?.nodes.find((n) => n.node_id === node?.node_id);
  const total = (chunks.data ?? []).reduce((s, c) => s + c.char_count, 0);
  return (
    <section className="chunk-col" aria-label={`Chunks from ${pipeline.name}`}>
      <header className="chunk-col__head">
        <span className="mono strong">{pipeline.name}</span>
        {node ? <Fingerprint value={node.node_fp} length={6} label={`${pipeline.name} chunk step`} /> : null}
        <span className="page-head__spacer" />
        {chunks.data?.length ? (
          <span className="muted num">
            {count(chunks.data.length)} chunks · avg {count(Math.round(total / chunks.data.length))} chars
          </span>
        ) : null}
      </header>
      {chunks.isError ? (
        <ErrorState error={chunks.error} onRetry={() => void chunks.refetch()} />
      ) : !chunks.data ? (
        [0, 1, 2].map((i) => <Skeleton key={i} width="100%" height={72} />)
      ) : chunks.data.length === 0 ? (
        <p className="chunk-col__empty">
          No chunks yet. The chunk step is <StatusIndicator state={run?.status ?? "pending"} />
          {run?.status === "succeeded" ? "; it produced no chunks for this document." : "."}
        </p>
      ) : (
        <ol className="chunk-list">
          {chunks.data.map((c) => (
            <li key={c.id} className="chunk">
              <div className="chunk__meta">
                <span className="mono">#{c.ordinal}</span>
                {pages(c) ? <span className="num">{pages(c)}</span> : null}
                <span className="num">{count(c.char_count)} chars</span>
                {c.heading_path.length ? <span className="chunk__path">{c.heading_path.join(" › ")}</span> : null}
              </div>
              {c.context ? (
                <p className="chunk__context">
                  <span className="chunk__context-label">context</span>
                  {c.context}
                </p>
              ) : null}
              {c.enrichment ? (
                <p className="chunk__context chunk__enrichment">
                  <span className="chunk__context-label">enrichment</span>
                  {c.enrichment}
                </p>
              ) : null}
              <p className="chunk__text">{c.text}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** One pipeline's chunks, optionally beside another's, to compare two chunkers on this document. */
export function ChunksTab({
  doc,
  pipelines,
  pipelineId,
  compareId,
  onChange,
}: {
  doc: Document;
  pipelines: Pipeline[];
  pipelineId: string | null;
  compareId: string | null;
  onChange: (a: string, b: string | null) => void;
}) {
  const a = pipelines.find((p) => p.id === pipelineId);
  const b = pipelines.find((p) => p.id === compareId && p.id !== pipelineId);
  if (!pipelines.length) return <EmptyState icon={Layers} title="No pipelines yet"><p>Chunks appear here once a pipeline processes this document.</p></EmptyState>;
  const same = a && b && chunkNode(a)?.node_fp === chunkNode(b)?.node_fp;
  return (
    <div className="chunks">
      <div className="tab-toolbar">
        <Field label="Chunks from">
          {(a11y) => (
            <Select {...a11y} value={pipelineId ?? ""} onChange={(e) => onChange(e.target.value, compareId === e.target.value ? null : compareId)}>
              {pipelines.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Compare with">
          {(a11y) => (
            <Select {...a11y} value={b?.id ?? ""} onChange={(e) => onChange(pipelineId ?? "", e.target.value || null)}>
              <option value="">Nothing</option>
              {pipelines
                .filter((x) => x.id !== pipelineId)
                .map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.name}
                  </option>
                ))}
            </Select>
          )}
        </Field>
      </div>
      {same ? (
        <p className="chunks__same" role="note">
          Both pipelines use the same chunk step, so their chunks are identical: they were computed once and shared.
        </p>
      ) : null}
      <div className={`chunk-cols ${b ? "chunk-cols--two" : ""}`}>
        {a ? <ChunkColumn key={a.id} doc={doc} pipeline={a} /> : null}
        {b ? <ChunkColumn key={b.id} doc={doc} pipeline={b} /> : null}
      </div>
    </div>
  );
}
