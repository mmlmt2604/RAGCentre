import { ArrowLeft, Play, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "../../api/client";
import { useDataset, useDatasetItems, useDeleteDataset } from "../../api/hooks";
import { Button, ButtonLink } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { ErrorState, SkeletonRows } from "../../components/States";
import { Time } from "../../components/Time";
import { nextStep } from "../../lib/errors";
import { count } from "../../lib/format";
import { useToast } from "../../lib/toast";

/** A dataset's questions and their targets. Read-only; it can be deleted. */
export function DatasetDetail() {
  const { corpusId = "", datasetId = "" } = useParams();
  const ds = useDataset(datasetId);
  const items = useDatasetItems(datasetId);
  const del = useDeleteDataset();
  const navigate = useNavigate();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const base = `/corpora/${corpusId}/eval`;
  const err = del.error instanceof ApiError ? del.error : null;

  return (
    <div className="page-pad eval">
      <header className="prov-detail__head">
        <Link to={base} className="back-link">
          <ArrowLeft size={14} aria-hidden /> Evaluation
        </Link>
        {ds.isError ? (
          <ErrorState error={ds.error} onRetry={() => void ds.refetch()} />
        ) : (
          <>
            <div className="prov-detail__title">
              <h1 className="mono">{ds.data?.name ?? "…"}</h1>
              {ds.data ? (
                <span className="muted">
                  {count(ds.data.item_count)} questions · imported <Time iso={ds.data.created_at} />
                </span>
              ) : null}
            </div>
            <div className="prov-detail__actions">
              <ButtonLink to={`${base}?panel=run&dataset=${datasetId}`} variant="primary" icon={Play}>
                Run on this dataset
              </ButtonLink>
              <span className="page-head__spacer" />
              <Button variant="danger" icon={Trash2} onClick={() => { del.reset(); setConfirm(true); }} disabled={!ds.data}>
                Delete dataset
              </Button>
            </div>
          </>
        )}
      </header>

      {items.isError ? (
        <ErrorState error={items.error} onRetry={() => void items.refetch()} />
      ) : (
        <div className="panel">
          <table className="rc-table ds-table">
            <thead>
              <tr>
                <th className="rc-right ds-col-n">#</th>
                <th>Question</th>
                <th>Targets</th>
                <th className="ds-col-filter">Filter</th>
              </tr>
            </thead>
            <tbody>
              {!items.data ? (
                <SkeletonRows rows={6} cols={[20, 280, 260, 80]} />
              ) : (
                items.data.items.map((it, i) => (
                  <tr key={it.id}>
                    <td className="rc-right num mono muted ds-col-n">{i + 1}</td>
                    <td>{it.question}</td>
                    <td>
                      <ul className="ds-targets">
                        {it.targets.map((t, j) => (
                          <li key={j}>
                            <Link to={`/corpora/${corpusId}/documents/${t.document_id}`} className="mono strong-link">
                              {t.document_label}
                            </Link>
                            {t.pages ? <span className="muted num"> p.{t.pages.join(", ")}</span> : null}
                            {t.text_contains ? <span className="ds-snippet">“{t.text_contains}”</span> : null}
                          </li>
                        ))}
                      </ul>
                    </td>
                    <td className="mono ds-col-filter">{it.filter ? JSON.stringify(it.filter) : <span className="muted">—</span>}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      <ConfirmDialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title={`Delete ${ds.data?.name ?? "dataset"}?`}
        confirmLabel="Delete dataset"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          del.mutate(datasetId, {
            onSuccess: () => {
              toast({ tone: "ok", title: `Deleted ${ds.data?.name ?? "dataset"}` });
              navigate(base);
            },
          })
        }
        error={err ? <><strong>{err.message}</strong>{nextStep(err) ? <p>{nextStep(err)}</p> : null}</> : null}
      >
        <p>Its {count(ds.data?.item_count ?? 0)} questions and every finished run on it are deleted. Runs in progress must finish first.</p>
      </ConfirmDialog>
    </div>
  );
}
