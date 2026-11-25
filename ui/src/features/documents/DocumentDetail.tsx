import { useCan, useTransport } from "../auth/context";
import { Can } from "../auth/Access";
import { ArrowLeft, Download, Pencil, Replace, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { ApiError } from "../../api/client";
import { useCorpus, useDeleteDocument, useDocument, useInvalidateDocuments, usePatchDocument, usePipelines } from "../../api/hooks";
import type { Corpus, Document } from "../../api/types";
import { Button } from "../../components/Button";
import { CodeBlock, CodeEditor } from "../../components/Code";
import { ConfirmDialog } from "../../components/Dialog";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, Skeleton } from "../../components/States";
import { Badge } from "../../components/Status";
import { Tabs } from "../../components/Tabs";
import { Time } from "../../components/Time";
import { nextStep } from "../../lib/errors";
import { bytes } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { ChunksTab } from "./ChunksTab";
import { IngestionTab } from "./IngestionTab";
import { MetadataFields } from "./MetadataFields";
import { buildMetadata, metadataView, metaDraft, mimeLabel, shortName } from "./model";
import { ParsedTab } from "./ParsedTab";

type Tab = "ingestion" | "parsed" | "chunks";

export function DocumentDetail() {
  const { corpusId = "", documentId = "" } = useParams();
  const doc = useDocument(documentId);
  const corpus = useCorpus(corpusId);
  const back = (
    <Link to={`/corpora/${corpusId}/documents`} className="back-link">
      <ArrowLeft size={14} aria-hidden /> Documents
    </Link>
  );
  if (doc.isError)
    return (
      <div className="page-pad">
        {back}
        <ErrorState error={doc.error} onRetry={() => void doc.refetch()} />
      </div>
    );
  if (!doc.data || !corpus.data)
    return (
      <div className="page-pad">
        {back}
        <Skeleton width={320} height={22} />
        <Skeleton width="50%" height={14} />
        <Skeleton width="100%" height={220} />
      </div>
    );
  return <Detail key={doc.data.id} doc={doc.data} corpus={corpus.data} back={back} />;
}

function Detail({ doc, corpus, back }: { doc: Document; corpus: Corpus; back: React.ReactNode }) {
  const { download, upload, isCurrent } = useTransport();
  const canEdit = useCan("edit");
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const invalidate = useInvalidateDocuments();
  const del = useDeleteDocument();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const replacer = useRef<HTMLInputElement>(null);
  const [replacing, setReplacing] = useState(false);
  const pipelinesQ = usePipelines(corpus.id);
  const pipelines = [...(pipelinesQ.data?.items ?? [])].sort((a, b) => Number(b.is_active) - Number(a.is_active) || a.name.localeCompare(b.name));
  const tab = (params.get("tab") as Tab | null) ?? "ingestion";
  const editing = canEdit && params.get("edit") === "1";
  const pipelineId = params.get("pipeline") ?? pipelines[0]?.id ?? null;

  const set = (updates: Record<string, string | null>) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(updates)) {
          if (v === null) next.delete(k);
          else next.set(k, v);
        }
        return next;
      },
      { replace: true },
    );

  const replace = async (file: File) => {
    setReplacing(true);
    const form = new FormData();
    form.set("file", file, file.name);
    try {
      const res = await upload<Document>(`/documents/${doc.id}/content`, form, { method: "PUT" });
      invalidate(res.body);
      toast({
        tone: "ok",
        title: res.body.revision > doc.revision ? `Replaced with ${file.name}` : "Same content, nothing to redo",
        body: res.body.revision > doc.revision ? `Revision ${res.body.revision}. Every pipeline processes the new content.` : undefined,
      });
    } catch (e) {
      if (!isCurrent()) return;
      toast({ tone: "danger", title: "Could not replace the file", body: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setReplacing(false);
    }
  };

  return (
    <div className="page-pad doc-detail">
      <input
        ref={replacer}
        type="file"
        hidden
        data-testid="replace-input"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) void replace(f);
        }}
      />
      <header className="prov-detail__head">
        {back}
        <div className="prov-detail__title">
          <h1 className="mono doc-title">{doc.filename}</h1>
          <Badge>{mimeLabel(doc.mime_type)}</Badge>
          {doc.status === "deleting" ? <Badge tone="danger">being deleted</Badge> : null}
        </div>
        <dl className="prov-facts">
          <div>
            <dt>Size</dt>
            <dd className="num">{bytes(doc.size_bytes)}</dd>
          </div>
          <div>
            <dt>Revision</dt>
            <dd className="num">{doc.revision}</dd>
          </div>
          <div>
            <dt>External id</dt>
            <dd className="mono">{doc.external_id ?? <span className="muted">none</span>}</dd>
          </div>
          <div>
            <dt>Content hash</dt>
            <dd>
              <Fingerprint value={doc.content_sha256} length={12} label="Content SHA-256" />
            </dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>
              <Time iso={doc.updated_at} />
            </dd>
          </div>
        </dl>
        <div className="prov-detail__actions">
          <Button
            icon={Download}
            onClick={() => download(`/documents/${doc.id}/content`, shortName(doc.filename)).catch((e) => toast({ tone: "danger", title: "Download failed", body: e instanceof ApiError ? e.message : String(e) }))}
          >
            Download
          </Button>
          <Can capability="edit"><Button icon={Replace} loading={replacing} disabled={doc.status !== "active"} onClick={() => replacer.current?.click()}>
            Replace file
          </Button></Can>
          <span className="page-head__spacer" />
          <Can capability="edit"><Button variant="danger" icon={Trash2} disabled={doc.status !== "active"} onClick={() => { del.reset(); setConfirmDelete(true); }}>
            Delete
          </Button></Can>
        </div>
      </header>

      <MetadataSection doc={doc} corpus={corpus} editing={editing} setEditing={(on) => set({ edit: on ? "1" : null })} />

      <section className="doc-tabs" aria-label="Processing">
        <Tabs<Tab>
          label="Document views"
          value={tab}
          onChange={(v) => set({ tab: v === "ingestion" ? null : v })}
          items={[
            { value: "ingestion", label: "Ingestion" },
            { value: "parsed", label: "Parsed" },
            { value: "chunks", label: "Chunks" },
          ]}
        />
        <div className="doc-tabs__panel" role="tabpanel">
          {pipelinesQ.isError ? (
            <ErrorState error={pipelinesQ.error} onRetry={() => void pipelinesQ.refetch()} />
          ) : !pipelinesQ.data ? (
            <Skeleton width="100%" height={160} />
          ) : tab === "ingestion" ? (
            <IngestionTab doc={doc} corpus={corpus} pipelines={pipelines} />
          ) : tab === "parsed" ? (
            <ParsedTab doc={doc} pipelines={pipelines} pipelineId={pipelineId} onPipeline={(id) => set({ pipeline: id })} />
          ) : (
            <ChunksTab doc={doc} pipelines={pipelines} pipelineId={pipelineId} compareId={params.get("compare")} onChange={(a, b) => set({ pipeline: a, compare: b })} />
          )}
        </div>
      </section>

      <ConfirmDialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={`Delete ${shortName(doc.filename)}?`}
        confirmLabel="Delete document"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          del.mutate(doc.id, {
            onSuccess: () => {
              toast({ tone: "ok", title: `Deleting ${shortName(doc.filename)}`, body: "Its chunks and index records are removed in the background." });
              navigate(`/corpora/${corpus.id}/documents`);
            },
          })
        }
        error={del.error instanceof ApiError ? <><strong>{del.error.message}</strong>{nextStep(del.error) ? <p>{nextStep(del.error)}</p> : null}</> : null}
      >
        <p>
          <span className="mono">{doc.filename}</span> is removed from {corpus.name}, with its chunks and index records in every pipeline.
        </p>
      </ConfirmDialog>
    </div>
  );
}

function MetadataSection({ doc, corpus, editing, setEditing }: { doc: Document; corpus: Corpus; editing: boolean; setEditing: (on: boolean) => void }) {
  const schema = corpus.metadata_schema;
  const patch = usePatchDocument(doc.id);
  const toast = useToast();
  const [draft, setDraft] = useState(() => metaDraft(doc, schema));
  const [attrs, setAttrs] = useState(() => JSON.stringify(doc.attributes, null, 2));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [attrError, setAttrError] = useState<string | null>(null);
  const view = metadataView(doc, schema);

  const start = () => {
    setDraft(metaDraft(doc, schema));
    setAttrs(JSON.stringify(doc.attributes, null, 2));
    setErrors({});
    setAttrError(null);
    patch.reset();
    setEditing(true);
  };

  const save = () => {
    const { metadata, errors: errs } = buildMetadata(draft, schema);
    setErrors(errs);
    let attributes: unknown;
    try {
      attributes = JSON.parse(attrs || "{}");
      if (!attributes || typeof attributes !== "object" || Array.isArray(attributes)) throw new Error("not an object");
      setAttrError(null);
    } catch {
      setAttrError("Attributes must be a JSON object, such as {\"owner\": \"docs-team\"}.");
      return;
    }
    if (Object.keys(errs).length) return;
    patch.mutate(
      { metadata, attributes: attributes as Record<string, unknown> },
      {
        onSuccess: () => {
          setEditing(false);
          toast({ tone: "ok", title: "Metadata saved", body: "Index records are updated with the new values." });
        },
      },
    );
  };

  return (
    <section className="prov-section" aria-labelledby="meta-title">
      <div className="section-head">
        <h2 id="meta-title">Metadata</h2>
        <span className="page-head__spacer" />
        {!editing ? (
          <Can capability="edit"><Button size="sm" icon={Pencil} onClick={start} disabled={doc.status !== "active"}>
            Edit metadata
          </Button></Can>
        ) : null}
      </div>
      {editing ? (
        <div className="meta-edit">
          <p className="prov-section__desc">Metadata fields come from the corpus schema and can be used in query filters. Attributes are free JSON returned with results, not filterable.</p>
          <MetadataFields schema={schema} draft={draft} onChange={setDraft} errors={errors} disabled={patch.isPending} />
          <div className="rc-field">
            <span className="rc-field__label">Attributes</span>
            <CodeEditor value={attrs} onChange={setAttrs} label="Attributes (JSON)" language="json" rows={6} />
            {attrError ? <p className="rc-field__error">{attrError}</p> : null}
          </div>
          {patch.error instanceof ApiError ? (
            <div className="form-error" role="alert">
              <strong>{patch.error.message}</strong>
              <code>{patch.error.code}</code>
            </div>
          ) : null}
          <div className="form-actions">
            <Button variant="primary" loading={patch.isPending} onClick={save}>
              Save metadata
            </Button>
            <Button variant="ghost" onClick={() => setEditing(false)} disabled={patch.isPending}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="meta-view">
          {view.length ? (
            <dl className="facts-list">
              {view.map((m) => (
                <div key={m.key}>
                  <dt className="mono">{m.key}</dt>
                  <dd>
                    <span className="mono">{JSON.stringify(m.value)}</span> {m.isDefault ? <span className="muted">default</span> : null}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="muted">This corpus has no metadata fields.</p>
          )}
          {Object.keys(doc.attributes).length ? <CodeBlock code={JSON.stringify(doc.attributes, null, 2)} label="Attributes" /> : null}
        </div>
      )}
    </section>
  );
}
