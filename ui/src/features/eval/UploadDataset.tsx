import { FileUp } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { useImportDataset } from "../../api/hooks";
import type { EvalDataset } from "../../api/types";
import { Button } from "../../components/Button";
import { CodeBlock } from "../../components/Code";
import { Field, TextInput } from "../../components/Field";
import { bytes } from "../../lib/format";

const EXAMPLE = `{"question": "How long do refunds take?", "targets": [{"document": "billing-refund-policy", "text_contains": "five to ten business days"}]}
{"question": "Which queue handles SSO escalations?", "targets": [{"document": "sso-saml-setup", "pages": [2]}], "filter": {"lang": "en"}}`;

/** Upload a JSON Lines dataset. Import errors are listed with their line numbers. */
export function UploadDataset({ corpusId, onDone, onCancel }: { corpusId: string; onDone: (d: EvalDataset) => void; onCancel: () => void }) {
  const importDs = useImportDataset(corpusId);
  const input = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const err = importDs.error instanceof ApiError ? importDs.error : null;
  const lineErrors = (err?.details?.errors as { line: number; message: string }[] | undefined) ?? [];

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!file) return setProblem("Choose a JSON Lines file.");
    const n = name.trim() || file.name.replace(/\.jsonl?$/i, "");
    setProblem(null);
    importDs.mutate({ name: n, file }, { onSuccess: onDone });
  };

  return (
    <form className="eval-panel" onSubmit={submit} aria-labelledby="upload-title" noValidate>
      <h2 id="upload-title">Upload a dataset</h2>
      <p className="form-section__desc">
        One question per line, as JSON. Each names its targets by document (external id or id), optionally narrowed to pages or a text snippet, so the same
        dataset scores any chunking.
      </p>
      <CodeBlock code={EXAMPLE} label="questions.jsonl" />
      <div className="eval-panel__fields">
        <Field label="Name" hint="Defaults to the file name.">
          {(a11y) => <TextInput {...a11y} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. support-golden" autoComplete="off" />}
        </Field>
        <div className="rc-field">
          <span className="rc-field__label">File *</span>
          <span className="eval-file">
            <Button icon={FileUp} onClick={() => input.current?.click()}>
              {file ? "Choose another file" : "Choose file"}
            </Button>
            <span className="mono muted">{file ? `${file.name} · ${bytes(file.size)}` : "No file chosen"}</span>
          </span>
          <input
            ref={input}
            type="file"
            accept=".jsonl,.json,application/x-ndjson,application/json,text/plain"
            hidden
            data-testid="dataset-input"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              e.target.value = "";
              importDs.reset();
            }}
          />
          {problem ? <p className="rc-field__error">{problem}</p> : null}
        </div>
      </div>
      {err ? (
        <div className="form-error" role="alert">
          <strong>{lineErrors.length ? `The file has ${lineErrors.length === 1 ? "a problem" : `${lineErrors.length} problems`}; nothing was imported.` : err.message}</strong>
          {lineErrors.length ? (
            <ol className="eval-lines">
              {lineErrors.map((l, i) => (
                <li key={i}>
                  <span className="mono">{l.line ? `line ${l.line}` : "file"}</span> {l.message}
                </li>
              ))}
            </ol>
          ) : null}
          <code>{err.code}</code>
        </div>
      ) : null}
      <div className="form-actions">
        <Button type="submit" variant="primary" loading={importDs.isPending}>
          Import dataset
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
