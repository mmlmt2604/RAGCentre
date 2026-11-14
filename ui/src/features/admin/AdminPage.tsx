import { Recycle, Trash2 } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { useGcPreview, useGcRun, useHealth } from "../../api/hooks";
import type { GcReport } from "../../api/types";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { ErrorState, Skeleton } from "../../components/States";
import { Badge } from "../../components/Status";
import { bytes, count } from "../../lib/format";
import { useToast } from "../../lib/toast";

/** Server details and garbage collection, which always previews before it deletes. */
export function AdminPage() {
  const health = useHealth();
  const preview = useGcPreview();
  const run = useGcRun();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const [report, setReport] = useState<GcReport | null>(null);
  const previewError = preview.error instanceof ApiError ? preview.error : null;

  return (
    <div className="page-pad admin">
      <header className="page-head">
        <h1>Admin</h1>
      </header>

      <section className="form-section" aria-labelledby="health-title">
        <h2 id="health-title">Server</h2>
        {health.isError ? (
          <ErrorState error={health.error} onRetry={() => void health.refetch()} />
        ) : !health.data ? (
          <Skeleton width="100%" height={64} />
        ) : (
          <dl className="facts-list">
            <div><dt>Version</dt><dd className="mono">{health.data.version}</dd></div>
            <div><dt>Migration</dt><dd className="num mono">{health.data.migration}</dd></div>
            <div><dt>Workers</dt><dd className="num mono">{health.data.workers}</dd></div>
            <div><dt>Access token</dt><dd>{health.data.auth_required ? <Badge tone="accent">required</Badge> : <span className="muted">not required</span>}</dd></div>
            <div><dt>Stored secrets</dt><dd>{health.data.secret_storage_enabled ? <Badge tone="ok">enabled</Badge> : <span className="muted">off — no RAGCENTRE_SECRET_KEY</span>}</dd></div>
          </dl>
        )}
      </section>

      <section className="form-section" aria-labelledby="gc-title">
        <h2 id="gc-title">Garbage collection</h2>
        <p className="form-section__desc">
          Removes indexes, chunks and runs that no non-archived pipeline uses, and blobs nothing references. Preview first; nothing is deleted until you confirm.
        </p>
        <div className="form-actions">
          <Button icon={Recycle} loading={preview.isPending} onClick={() => preview.mutate(undefined, { onSuccess: (r) => { setReport(r); preview.reset(); } })}>
            Preview
          </Button>
          <Button variant="danger" icon={Trash2} disabled={!report || report.indexes.length === 0 && report.node_count === 0 && report.blob_count === 0} onClick={() => { run.reset(); setConfirm(true); }}>
            Collect
          </Button>
        </div>
        {previewError ? <ErrorState error={previewError} onRetry={() => preview.mutate()} /> : null}
        {report ? (
          <dl className="gc-report">
            <div><dt>Indexes to drop</dt><dd className="num">{count(report.indexes.length)}</dd></div>
            <div><dt>Node work to remove</dt><dd className="num">{count(report.node_count)}</dd></div>
            <div><dt>Blobs to remove</dt><dd className="num">{count(report.blob_count)} ({bytes(report.blob_bytes)})</dd></div>
          </dl>
        ) : null}
        {report && report.indexes.length ? (
          <ul className="gc-indexes">
            {report.indexes.map((i) => (
              <li key={i.id} className="mono">
                <Badge tone={i.kind === "vector" ? "accent" : "muted"}>{i.kind}</Badge> {i.physical_name}
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <ConfirmDialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Collect garbage?"
        confirmLabel="Delete unused work"
        destructive
        pending={run.isPending}
        onConfirm={() =>
          run.mutate(undefined, {
            onSuccess: (r) => {
              setConfirm(false);
              setReport(null);
              toast({ tone: "ok", title: "Garbage collected", body: `${count(r.indexes.length)} indexes, ${count(r.node_count)} node-work sets, ${count(r.blob_count)} blobs removed.` });
            },
          })
        }
        error={run.error instanceof ApiError ? <><strong>{run.error.message}</strong></> : null}
      >
        <p>This removes {report ? count(report.indexes.length) : 0} indexes and {report ? count(report.node_count) : 0} node-work sets. Pipelines that still use any of it are untouched; archived pipelines keep only their configuration.</p>
      </ConfirmDialog>
    </div>
  );
}
