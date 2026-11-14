import { FileSearch } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { useParsed } from "../../api/hooks";
import type { Document, Pipeline } from "../../api/types";
import { CodeBlock } from "../../components/Code";
import { Field, Segmented, Select } from "../../components/Field";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { Badge, StatusIndicator } from "../../components/Status";

interface IrBlock {
  id: string;
  type: string;
  level?: number | null;
  text: string;
  page?: number | null;
  heading_path?: string[];
  metadata?: Record<string, unknown>;
}

/** The Document IR a pipeline's parse step produced, block by block in reading order. */
export function ParsedTab({ doc, pipelines, pipelineId, onPipeline }: { doc: Document; pipelines: Pipeline[]; pipelineId: string | null; onPipeline: (id: string) => void }) {
  const [view, setView] = useState<"blocks" | "json">("blocks");
  const parsed = useParsed(doc.id, pipelineId);
  const p = pipelines.find((x) => x.id === pipelineId);
  const parseNode = doc.ingestion.find((i) => i.pipeline_id === pipelineId)?.nodes.find((n) => p?.nodes.find((pn) => pn.node_id === n.node_id)?.type === "parse");
  const blocks = (parsed.data?.blocks as IrBlock[] | undefined) ?? [];
  const notYet = parsed.error instanceof ApiError && parsed.error.status === 404;

  return (
    <div className="parsed">
      <div className="tab-toolbar">
        <Field label="Parsed by">
          {(a11y) => (
            <Select {...a11y} value={pipelineId ?? ""} onChange={(e) => onPipeline(e.target.value)}>
              {pipelines.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <span className="page-head__spacer" />
        {parsed.data ? (
          <Segmented
            label="View"
            value={view}
            onChange={setView}
            options={[
              { value: "blocks", label: "Blocks", count: blocks.length },
              { value: "json", label: "Raw JSON" },
            ]}
          />
        ) : null}
      </div>
      {notYet ? (
        <EmptyState icon={FileSearch} title="Not parsed yet">
          <p>
            This pipeline's parse step has not succeeded for this document
            {parseNode ? (
              <>
                {" "}
                (it is <StatusIndicator state={parseNode.status} />)
              </>
            ) : null}
            . The parsed blocks appear here once it does.
          </p>
        </EmptyState>
      ) : parsed.isError ? (
        <ErrorState error={parsed.error} onRetry={() => void parsed.refetch()} />
      ) : !parsed.data ? (
        <div className="parsed-blocks">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} width="100%" height={36} />
          ))}
        </div>
      ) : view === "json" ? (
        <CodeBlock code={JSON.stringify(parsed.data, null, 2)} label="Document IR" maxHeight={640} />
      ) : (
        <ol className="parsed-blocks" aria-label="Blocks in reading order">
          {blocks.map((b) => (
            <li key={b.id} className={`ir-block ir-block--${b.type}`}>
              <div className="ir-block__meta">
                <span className="mono ir-block__id">{b.id}</span>
                <Badge>{b.type === "heading" ? `heading ${b.level}` : b.type}</Badge>
                {b.page ? <span className="muted num">p.{b.page}</span> : null}
                {b.metadata?.original_type ? <span className="muted">was {String(b.metadata.original_type)}</span> : null}
              </div>
              <div className="ir-block__body">
                {b.heading_path?.length && b.type !== "heading" ? <p className="ir-block__path">{b.heading_path.join(" › ")}</p> : null}
                <p className="ir-block__text">{b.text || <span className="muted">(no text)</span>}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
