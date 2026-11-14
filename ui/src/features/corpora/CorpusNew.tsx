import { ArrowLeft } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError } from "../../api/client";
import { useCreateCorpus } from "../../api/hooks";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { nextStep } from "../../lib/errors";
import { useToast } from "../../lib/toast";
import { SchemaEditor } from "./SchemaEditor";
import { buildSchema, type DraftField } from "./schemaDraft";

export function CorpusNew() {
  const create = useCreateCorpus();
  const navigate = useNavigate();
  const toast = useToast();
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<DraftField[]>([]);
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({});

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const { schema, errors } = buildSchema({ fields: {} }, drafts);
    setRowErrors(errors);
    const nErr = name.trim() ? null : "Give the corpus a name.";
    setNameError(nErr);
    if (nErr || Object.keys(errors).length) return;
    create.mutate(
      { name: name.trim(), metadata_schema: schema },
      {
        onSuccess: (c) => {
          toast({ tone: "ok", title: `Created ${c.name}`, body: "Next: upload documents, then build a pipeline." });
          navigate(`/corpora/${c.id}/documents`);
        },
        onError: (err) => {
          if (err instanceof ApiError && err.code === "conflict") setNameError(err.message);
        },
      },
    );
  };
  const apiErr = create.error instanceof ApiError && create.error.code !== "conflict" ? create.error : null;

  return (
    <div className="page-pad narrow-page">
      <header className="page-head">
        <div>
          <Link to="/" className="back-link">
            <ArrowLeft size={14} aria-hidden /> Corpora
          </Link>
          <h1>New corpus</h1>
        </div>
      </header>
      <form className="stack-form" onSubmit={submit} noValidate>
<div className="field-name">
        <Field label="Name *" hint="Shown everywhere the corpus appears. You can rename it later." error={nameError}>
          {(a11y) => <TextInput {...a11y} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. support-docs" autoComplete="off" required />}
        </Field>
        </div>
        <section className="form-section" aria-labelledby="schema-title">
          <h2 id="schema-title">Metadata fields</h2>
          <p className="form-section__desc">
            Optional. Fields you can set on each document and filter queries by, such as language or product. Every field needs a default, which documents
            without a value get. You can add fields later, but not change or remove them.
          </p>
          <SchemaEditor existing={{ fields: {} }} drafts={drafts} onChange={setDrafts} errors={rowErrors} disabled={create.isPending} />
        </section>
        {apiErr ? (
          <div className="form-error" role="alert">
            <strong>{apiErr.message}</strong>
            {nextStep(apiErr) ? <p>{nextStep(apiErr)}</p> : null}
            <code>{apiErr.code}</code>
          </div>
        ) : null}
        <div className="form-actions">
          <Button type="submit" variant="primary" loading={create.isPending}>
            Create corpus
          </Button>
          <Link to="/" className="link-btn">
            Cancel
          </Link>
        </div>
      </form>
    </div>
  );
}
