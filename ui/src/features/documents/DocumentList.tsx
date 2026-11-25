import { useCan, useTransport } from "../auth/context";
import { Can } from "../auth/Access";
import { FileText, Pencil, Replace, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState, type DragEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { ApiError } from "../../api/client";
import { useCorpus, useDeleteDocument, useDocuments, useInvalidateDocuments } from "../../api/hooks";
import type { Document, RunState } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { SearchInput, Segmented, Select } from "../../components/Field";
import { EmptyState, ErrorState, SkeletonRows } from "../../components/States";
import { Badge, StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { nextStep } from "../../lib/errors";
import { bytes, count } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { mimeLabel, orderedPipelines, shortName, stage, type Staged } from "./model";
import { UploadPanel } from "./UploadPanel";

type StateFilter = "all" | "failed" | "running" | "pending" | "succeeded";
const PAGE = 50;

export function DocumentList() {
  const canEdit = useCan("edit");
  const { upload, isCurrent } = useTransport();
  const { corpusId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const corpus = useCorpus(corpusId);
  const invalidate = useInvalidateDocuments();

  const state = (params.get("state") as StateFilter | null) ?? "all";
  const pipelineScope = params.get("pipeline") ?? "";
  const cursor = Number(params.get("cursor") ?? 0) || 0;
  const [q, setQ] = useState(params.get("q") ?? "");
  const [staged, setStaged] = useState<Staged[]>([]);
  const [dragging, setDragging] = useState(false);
  const [deleting, setDeleting] = useState<Document | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const replacer = useRef<HTMLInputElement>(null);
  const replaceTarget = useRef<Document | null>(null);
  const del = useDeleteDocument();

  // The filename text is component state and re-applied on every URL change: the search params of
  // the last render can lag behind a just-finished navigation (React Router renders them in a
  // transition), and a filter change must never drop the text the user typed.
  const setParam = (updates: Record<string, string | null>) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (q) next.set("q", q);
        else next.delete("q");
        for (const [k, v] of Object.entries(updates)) {
          if (v === null || v === "" || v === "all") next.delete(k);
          else next.set(k, v);
        }
        if (!("cursor" in updates)) next.delete("cursor");
        return next;
      },
      { replace: true },
    );

  // Debounce the filename filter into the URL.
  useEffect(() => {
    const t = window.setTimeout(() => setParams((prev) => {
      if ((prev.get("q") ?? "") === q) return prev;
      const next = new URLSearchParams(prev);
      if (q) next.set("q", q);
      else next.delete("q");
      next.delete("cursor");
      return next;
    }, { replace: true }), 250);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const docs = useDocuments(corpusId, {
    q: params.get("q") ?? undefined,
    state: state === "all" ? undefined : state,
    pipeline_id: pipelineScope || undefined,
    cursor: cursor ? String(cursor) : undefined,
  });
  const pipelines = orderedPipelines(corpus.data);
  const items = docs.data?.items ?? [];
  const filtered = Boolean(params.get("q") || state !== "all" || pipelineScope);
  const empty = docs.data && items.length === 0 && !filtered && cursor === 0;

  const addFiles = (files: FileList | File[] | null) => {
    if (!canEdit || !files || !files.length) return;
    // Copy now: a FileList is live, and the input is cleared before React runs the updater.
    const added = stage(Array.from(files));
    setStaged((xs) => [...xs.filter((x) => x.status === "staged" || x.status === "failed" || x.status === "uploading"), ...added]);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  };

  const replaceFile = async (file: File) => {
    const doc = replaceTarget.current;
    if (!doc) return;
    const form = new FormData();
    form.set("file", file, file.name);
    try {
      const res = await upload<Document>(`/documents/${doc.id}/content`, form, { method: "PUT" });
      invalidate(res.body);
      toast({
        tone: "ok",
        title: res.body.revision > doc.revision ? `Replaced ${shortName(doc.filename)}` : `${shortName(doc.filename)} is unchanged`,
        body: res.body.revision > doc.revision ? `Now revision ${res.body.revision}; only this document is processed again.` : "The new file has identical content, so nothing was redone.",
      });
    } catch (e) {
      if (!isCurrent()) return;
      toast({ tone: "danger", title: `Could not replace ${shortName(doc.filename)}`, body: e instanceof ApiError ? e.message : String(e) });
    }
  };

  return (
    <div
      className="docs"
      onDragOver={(e) => {
        if (!canEdit || !e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target || !e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={onDrop}
    >
      <input ref={picker} type="file" multiple hidden data-testid="upload-input" onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
      <input
        ref={replacer}
        type="file"
        hidden
        data-testid="replace-input"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) void replaceFile(f);
        }}
      />
      {dragging ? (
        <div className="docs__drop" aria-hidden>
          <Upload size={20} strokeWidth={1.75} />
          Drop files to upload to {corpus.data?.name ?? "this corpus"}
        </div>
      ) : null}

      <header className="pl-head">
        <div className="pl-head__title">
          <h1>Documents</h1>
          {corpus.data ? <p className="pl-head__summary">{count(corpus.data.document_count)} in {corpus.data.name}.{canEdit ? " Drop files anywhere on this page to upload." : ""}</p> : null}
        </div>
        <span className="pl-head__spacer" />
        <Can capability="edit"><Button variant="primary" icon={Upload} onClick={() => picker.current?.click()}>
          Upload files
        </Button></Can>
      </header>

      {staged.length && corpus.data ? <UploadPanel corpus={corpus.data} items={staged} setItems={setStaged} onAddMore={() => picker.current?.click()} /> : null}

      {!empty ? (
        <div className="pl-toolbar">
          <SearchInput aria-label="Filter by file name or external id" placeholder="File name or external id" value={q} onChange={(e) => setQ(e.target.value)} className="pl-search" />
          <Segmented<StateFilter>
            label="Ingestion state"
            value={state}
            onChange={(v) => setParam({ state: v })}
            options={[
              { value: "all", label: "All" },
              { value: "failed", label: "Failed" },
              { value: "running", label: "Running" },
              { value: "pending", label: "Pending" },
              { value: "succeeded", label: "Done" },
            ]}
          />
          <Select aria-label="Pipelines the state refers to" value={pipelineScope} onChange={(e) => setParam({ pipeline: e.target.value })}>
            <option value="">In any pipeline</option>
            {pipelines.map((p) => (
              <option key={p.id} value={p.id}>
                In {p.name}
              </option>
            ))}
          </Select>
        </div>
      ) : null}

      {docs.isError && !docs.data ? (
        <ErrorState error={docs.error} onRetry={() => void docs.refetch()} />
      ) : empty ? (
        <EmptyState
          icon={FileText}
          title="No documents yet"
          action={
            <Can capability="edit"><Button variant="primary" icon={Upload} onClick={() => picker.current?.click()}>
              Upload files
            </Button></Can>
          }
        >
          <p>Upload files here, or drag them onto this page. Each document is processed by every pipeline in the corpus.</p>
          <p>Plain text and Markdown work with the built-in parser; PDFs and other formats need a parser service.</p>
        </EmptyState>
      ) : (
        <div className="pl-tablewrap">
          <table className="rc-table docs-table">
            <caption className="sr-only">Documents, with their ingestion state in each pipeline</caption>
            <thead>
              <tr>
                <th>Document</th>
                <th className="docs-col-type">Type</th>
                <th className="rc-right docs-col-size">Size</th>
                <th className="docs-col-meta">Metadata</th>
                <th className="docs-col-time">Updated</th>
                {pipelines.map((p) => (
                  <th key={p.id} className="docs-col-pl" title={p.name}>
                    <span className="docs-pl-head">
                      <span className="mono">{p.name}</span>
                      {p.id === corpus.data?.active_pipeline_id ? <Badge tone="accent">active</Badge> : null}
                    </span>
                  </th>
                ))}
                <th className="rc-col-tight">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {!docs.data ? (
                <SkeletonRows rows={8} cols={[220, 60, 50, 140, 60, ...pipelines.map(() => 20), 60]} />
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={6 + pipelines.length}>
                    <div className="pl-nomatch">
                      No documents match these filters.{" "}
                      <button type="button" className="link-btn" onClick={() => { setQ(""); setParams(new URLSearchParams(), { replace: true }); }}>
                        Clear filters
                      </button>
                    </div>
                  </td>
                </tr>
              ) : (
                items.map((d) => (
                  <tr key={d.id}>
                    <td className="docs-name">
                      <Link to={`/corpora/${corpusId}/documents/${d.id}`} className="strong-link mono" title={d.filename}>
                        {d.filename}
                      </Link>
                      {d.external_id && d.external_id !== d.filename ? <span className="docs-ext mono">{d.external_id}</span> : null}
                    </td>
                    <td className="docs-col-type muted">{mimeLabel(d.mime_type)}</td>
                    <td className="rc-right num mono docs-col-size">{bytes(d.size_bytes)}</td>
                    <td className="docs-col-meta">
                      <span className="docs-meta">
                        {Object.entries(d.metadata).map(([k, v]) => (
                          <span key={k} className="mono">
                            <span className="muted">{k}=</span>
                            {String(v)}
                          </span>
                        ))}
                      </span>
                    </td>
                    <td className="muted nowrap docs-col-time">
                      <Time iso={d.updated_at} compact />
                    </td>
                    {pipelines.map((p) => {
                      const ing = d.ingestion.find((i) => i.pipeline_id === p.id);
                      const st: RunState = ing?.state ?? "pending";
                      const failedNode = ing?.nodes.find((n) => n.status === "failed");
                      return (
                        <td key={p.id} className="docs-col-pl" title={`${p.name}: ${st}${failedNode ? ` at ${failedNode.node_id} (${failedNode.error_code})` : ""}`}>
                          <StatusIndicator state={st} compact label={`${p.name}: ${st}${failedNode ? ` at ${failedNode.node_id}` : ""}`} />
                        </td>
                      );
                    })}
                    <td className="rc-col-tight">
                      <span className="docs-actions">
                        <Can capability="edit"><IconButton icon={Pencil} label={`Edit metadata of ${d.filename}`} onClick={() => navigate(`/corpora/${corpusId}/documents/${d.id}?edit=1`)} /></Can>
                        <Can capability="edit"><IconButton icon={Replace} label={`Replace the file of ${d.filename}`} onClick={() => { replaceTarget.current = d; replacer.current?.click(); }} /></Can>
                        <Can capability="edit"><IconButton icon={Trash2} label={`Delete ${d.filename}`} onClick={() => { del.reset(); setDeleting(d); }} /></Can>
                      </span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          {docs.data && (cursor > 0 || docs.data.next_cursor) ? (
            <nav className="docs-pager" aria-label="Pages">
              <span className="muted num">
                {count(cursor + 1)}–{count(cursor + items.length)}
              </span>
              <Button size="sm" disabled={cursor === 0} onClick={() => setParam({ cursor: String(Math.max(0, cursor - PAGE)) })}>
                Previous
              </Button>
              <Button size="sm" disabled={!docs.data.next_cursor} onClick={() => setParam({ cursor: docs.data!.next_cursor })}>
                Next
              </Button>
            </nav>
          ) : null}
        </div>
      )}

      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title={`Delete ${deleting ? shortName(deleting.filename) : ""}?`}
        confirmLabel="Delete document"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          deleting &&
          del.mutate(deleting.id, {
            onSuccess: () => {
              toast({ tone: "ok", title: `Deleting ${shortName(deleting.filename)}`, body: "Its chunks and index records are removed in the background." });
              setDeleting(null);
            },
          })
        }
        error={del.error instanceof ApiError ? <><strong>{del.error.message}</strong>{nextStep(del.error) ? <p>{nextStep(del.error)}</p> : null}</> : null}
      >
        <p>
          <span className="mono">{deleting?.filename}</span> is removed from this corpus, with its chunks and index records in every pipeline. Evaluation
          targets that name it keep their label but can no longer match.
        </p>
      </ConfirmDialog>
    </div>
  );
}
