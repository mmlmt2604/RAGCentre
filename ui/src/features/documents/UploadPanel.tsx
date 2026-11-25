import { useTransport } from "../auth/context";
import { CircleCheck, CircleX, FileUp, LoaderCircle, Upload, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../../api/client";
import { useInvalidateDocuments } from "../../api/hooks";
import type { Corpus, Document } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { Checkbox, Field, TextInput } from "../../components/Field";
import { nextStep } from "../../lib/errors";
import { bytes } from "../../lib/format";
import { buildMetadata, metaDraft, type Staged } from "./model";
import { MetadataFields } from "./MetadataFields";

const PARALLEL = 3;

/** Staged uploads with optional batch metadata and external ids; each file reports its own progress and result. */
export function UploadPanel({
  corpus,
  items,
  setItems,
  onAddMore,
}: {
  corpus: Corpus;
  items: Staged[];
  setItems: (fn: (xs: Staged[]) => Staged[]) => void;
  onAddMore: () => void;
}) {
  const { upload, isCurrent } = useTransport();
  const invalidate = useInvalidateDocuments();
  const schema = corpus.metadata_schema;
  const [draft, setDraft] = useState<Record<string, string>>(() => metaDraft(null, schema));
  const [metaErrors, setMetaErrors] = useState<Record<string, string>>({});
  const [useNames, setUseNames] = useState(true);
  const [externalId, setExternalId] = useState("");
  const [running, setRunning] = useState(false);

  const staged = items.filter((i) => i.status === "staged" || i.status === "failed");
  const single = items.length === 1;
  const finished = items.length > 0 && items.every((i) => i.status === "added" || i.status === "exists" || i.status === "failed");
  const patch = (key: string, p: Partial<Staged>) => setItems((xs) => xs.map((x) => (x.key === key ? { ...x, ...p } : x)));

  const start = async () => {
    const { metadata, errors } = buildMetadata(draft, schema);
    setMetaErrors(errors);
    if (Object.keys(errors).length) return;
    setRunning(true);
    const queue = [...staged];
    const worker = async () => {
      for (let item = queue.shift(); item && isCurrent(); item = queue.shift()) {
        patch(item.key, { status: "uploading", progress: 0, message: undefined });
        const form = new FormData();
        form.set("file", item.file, item.file.name);
        if (Object.keys(metadata).length) form.set("metadata", JSON.stringify(metadata));
        const ext = single && externalId.trim() ? externalId.trim() : useNames ? item.file.name : "";
        if (ext) form.set("external_id", ext);
        try {
          const res = await upload<Document>(`/corpora/${corpus.id}/documents`, form, { onProgress: (f) => patch(item!.key, { progress: f }) });
          patch(item.key, {
            status: res.status === 201 ? "added" : "exists",
            progress: 1,
            documentId: res.body.id,
            message: res.status === 201 ? "Added. Ingestion has started." : `Already in the corpus as ${ext}; ${res.body.revision > 1 ? `now revision ${res.body.revision}` : "unchanged, no work needed"}.`,
          });
        } catch (e) {
          if (!isCurrent()) return;
          const err = e instanceof ApiError ? e : null;
          patch(item.key, { status: "failed", message: err ? `${err.message}${nextStep(err) ? ` ${nextStep(err)}` : ""}` : String(e) });
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL, queue.length) }, worker));
    if (!isCurrent()) return;
    setRunning(false);
    invalidate();
  };

  return (
    <section className="upload" aria-labelledby="upload-title">
      <header className="upload__head">
        <h2 id="upload-title">
          <Upload size={14} strokeWidth={2} aria-hidden /> Upload {items.length === 1 ? "1 file" : `${items.length} files`}
        </h2>
        <span className="page-head__spacer" />
        {!running ? (
          <Button size="sm" variant="ghost" icon={FileUp} onClick={onAddMore}>
            Add files
          </Button>
        ) : null}
      </header>

      <ul className="upload__list" aria-label="Files to upload">
        {items.map((i) => (
          <li key={i.key} className={`upload__item upload__item--${i.status}`}>
            <span className="upload__icon" aria-hidden>
              {i.status === "uploading" ? <LoaderCircle size={14} className="rc-spin" /> : i.status === "added" || i.status === "exists" ? <CircleCheck size={14} /> : i.status === "failed" ? <CircleX size={14} /> : <FileUp size={14} />}
            </span>
            <span className="upload__name mono" title={i.file.name}>
              {i.file.name}
            </span>
            <span className="upload__size num muted">{bytes(i.file.size)}</span>
            <span className="upload__state">
              {i.status === "uploading" ? (
                <span className="upload__bar" role="progressbar" aria-label={`Uploading ${i.file.name}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(i.progress * 100)}>
                  <span style={{ width: `${Math.max(4, i.progress * 100)}%` }} />
                </span>
              ) : i.message ? (
                <span className="upload__msg">
                  {i.message}
                  {i.documentId ? (
                    <>
                      {" "}
                      <Link to={`/corpora/${corpus.id}/documents/${i.documentId}`} className="inline-link">
                        Open
                      </Link>
                    </>
                  ) : null}
                </span>
              ) : (
                <span className="muted">Ready</span>
              )}
            </span>
            {i.status === "staged" || i.status === "failed" ? (
              <IconButton icon={X} label={`Remove ${i.file.name}`} onClick={() => setItems((xs) => xs.filter((x) => x.key !== i.key))} disabled={running} />
            ) : (
              <span className="upload__spacer" />
            )}
          </li>
        ))}
      </ul>

      {!finished ? (
        <div className="upload__options">
          <details className="upload__meta" open={Object.keys(metaErrors).length > 0 || undefined}>
            <summary>Metadata for {single ? "this file" : "these files"} (optional)</summary>
            <MetadataFields schema={schema} draft={draft} onChange={setDraft} errors={metaErrors} disabled={running} />
          </details>
          {single ? (
            <div className="field-name">
              <Field label="External id" hint="Your own id for the document. Uploading again with the same id replaces its content, or does nothing if unchanged.">
                {(a11y) => <TextInput {...a11y} mono value={externalId} onChange={(e) => setExternalId(e.target.value)} placeholder={items[0]?.file.name} disabled={running} />}
              </Field>
            </div>
          ) : (
            <Checkbox
              checked={useNames}
              onChange={(e) => setUseNames(e.target.checked)}
              disabled={running}
              label="Use each file name as its external id, so uploading the same file again does no work"
            />
          )}
        </div>
      ) : null}

      <div className="form-actions">
        {finished ? (
          <>
            <Button variant="primary" onClick={() => setItems(() => [])}>
              Done
            </Button>
            {items.some((i) => i.status === "failed") ? (
              <Button onClick={() => setItems((xs) => xs.filter((x) => x.status === "failed").map((x) => ({ ...x, status: "staged", message: undefined })))}>
                Keep the failed files to retry
              </Button>
            ) : null}
          </>
        ) : (
          <>
            <Button variant="primary" icon={Upload} loading={running} disabled={!staged.length} onClick={() => void start()}>
              {staged.length === 1 ? "Upload 1 file" : `Upload ${staged.length} files`}
            </Button>
            {!running ? (
              <Button variant="ghost" onClick={() => setItems(() => [])}>
                Cancel
              </Button>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
