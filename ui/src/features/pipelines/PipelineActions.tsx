import { useCan } from "../auth/context";
import { Archive, RotateCw, Star } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { useArchivePipeline, useRetryFailed, useSetActivePipeline } from "../../api/hooks";
import type { Pipeline } from "../../api/types";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { nextStep } from "../../lib/errors";
import { percent, plural } from "../../lib/format";
import { useToast } from "../../lib/toast";

function message(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

/** Set active (confirming when not ready), retry failed runs, archive (confirming; 409 explained). */
export function PipelineActions({ corpusId, pipeline, failedRuns, activeName }: { corpusId: string; pipeline: Pipeline; failedRuns: number; activeName: string | null }) {
  const canEdit = useCan("edit");
  const retry = useRetryFailed();
  const archive = useArchivePipeline();
  const setActive = useSetActivePipeline();
  const toast = useToast();
  const [confirm, setConfirm] = useState<"archive" | "activate" | null>(null);

  const activate = (force: boolean) =>
    setActive.mutate(
      { corpusId, pipelineId: pipeline.id, force },
      {
        onSuccess: () => {
          setConfirm(null);
          toast({ tone: "ok", title: `${pipeline.name} is now the active pipeline`, body: "Queries that name no pipeline use it from now on." });
        },
        onError: (e) => toast({ tone: "danger", title: "Could not make it active", body: message(e) }),
      },
    );

  if (!canEdit) return null;
  const archiveErr = archive.error instanceof ApiError ? archive.error : null;
  return (
    <>
      {!pipeline.is_active && !pipeline.archived ? (
        <Button icon={Star} loading={setActive.isPending && confirm === null} onClick={() => (pipeline.status === "ready" ? activate(false) : setConfirm("activate"))}>
          Set active
        </Button>
      ) : null}
      {failedRuns > 0 ? (
        <Button
          icon={RotateCw}
          loading={retry.isPending}
          onClick={() =>
            retry.mutate(pipeline.id, {
              onSuccess: (r) => toast({ tone: "ok", title: `Retrying ${plural(r.retried, "run")}`, body: "Blocked steps continue once these succeed." }),
              onError: (e) => toast({ tone: "danger", title: "Retry failed", body: message(e) }),
            })
          }
        >
          Retry {plural(failedRuns, "failed run")}
        </Button>
      ) : null}
      <span className="page-head__spacer" />
      {!pipeline.archived ? (
        <Button variant="danger" icon={Archive} onClick={() => { archive.reset(); setConfirm("archive"); }}>
          Archive
        </Button>
      ) : null}

      <ConfirmDialog
        open={confirm === "activate"}
        onClose={() => setConfirm(null)}
        title={`Make ${pipeline.name} active while it is ${pipeline.status}?`}
        confirmLabel="Make it active anyway"
        pending={setActive.isPending}
        onConfirm={() => activate(true)}
      >
        <p>
          Only <strong>{percent(pipeline.coverage)}</strong> of documents are searchable through it. Queries that name no pipeline would use it and{" "}
          {pipeline.status === "degraded" ? "miss the documents whose ingestion failed" : "miss documents that are still being processed"}
          {activeName ? <>, instead of {activeName}</> : null}.
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirm === "archive"}
        onClose={() => setConfirm(null)}
        title={`Archive ${pipeline.name}?`}
        confirmLabel="Archive pipeline"
        destructive
        pending={archive.isPending}
        onConfirm={() =>
          archive.mutate(pipeline.id, {
            onSuccess: () => {
              setConfirm(null);
              toast({ tone: "ok", title: `Archived ${pipeline.name}` });
            },
          })
        }
        error={archiveErr ? <><strong>{archiveErr.message}</strong>{nextStep(archiveErr) ? <p>{nextStep(archiveErr)}</p> : null}</> : null}
      >
        <p>It can no longer be queried or evaluated, and work no other pipeline needs stops. Its indexes stay until garbage collection, and it stays listed under Archived.</p>
        {pipeline.is_active ? <p className="dialog-warn">It is the active pipeline, so the server will refuse until another one is made active.</p> : null}
      </ConfirmDialog>
    </>
  );
}
