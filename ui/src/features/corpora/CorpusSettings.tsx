import { useState, type FormEvent } from "react";
import { useParams } from "react-router";
import { ApiError } from "../../api/client";
import { useCorpus, useRenameCorpus, useUpdateSchema } from "../../api/hooks";
import type { Corpus } from "../../api/types";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { ErrorState, Skeleton } from "../../components/States";
import { Time } from "../../components/Time";
import { nextStep } from "../../lib/errors";
import { useToast } from "../../lib/toast";
import { SchemaEditor } from "./SchemaEditor";
import { buildSchema, type DraftField } from "./schemaDraft";

export function CorpusSettings() {
  const { corpusId = "" } = useParams();
  const corpus = useCorpus(corpusId);
  if (corpus.isError) return <div className="page-pad"><ErrorState error={corpus.error} onRetry={() => void corpus.refetch()} /></div>;
  const c = corpus.data;
  return (
    <div className="page-pad narrow-page">
      <header className="page-head">
        <h1>Settings</h1>
      </header>
      {!c ? (
        <>
          <Skeleton width={260} height={28} />
          <Skeleton width="100%" height={160} />
        </>
      ) : (
        <>
          <RenameForm key={`name-${c.id}`} c={c} />
          <SchemaForm key={`schema-${c.id}-${Object.keys(c.metadata_schema.fields).join(",")}`} c={c} />
          <section className="form-section" aria-labelledby="about-title">
            <h2 id="about-title">About</h2>
            <dl className="facts-list">
              <div>
                <dt>Corpus id</dt>
                <dd className="mono">{c.id}</dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>
                  <Time iso={c.created_at} />
                </dd>
              </div>
            </dl>
            <p className="form-section__desc">Deleting a corpus is not available in this version. Archive its pipelines to stop their work.</p>
          </section>
        </>
      )}
    </div>
  );
}

function ErrorBox({ error }: { error: unknown }) {
  const api = error instanceof ApiError ? error : null;
  return (
    <div className="form-error" role="alert">
      <strong>{api?.message ?? String(error)}</strong>
      {nextStep(error) ? <p>{nextStep(error)}</p> : null}
      {api ? <code>{api.code}</code> : null}
    </div>
  );
}

function RenameForm({ c }: { c: Corpus }) {
  const rename = useRenameCorpus(c.id);
  const toast = useToast();
  const [name, setName] = useState(c.name);
  const [error, setError] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return setError("Give the corpus a name.");
    setError(null);
    rename.mutate(name.trim(), {
      onSuccess: (u) => toast({ tone: "ok", title: `Renamed to ${u.name}` }),
      onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
    });
  };
  return (
    <form className="form-section" onSubmit={submit} aria-labelledby="name-title" noValidate>
      <h2 id="name-title">Name</h2>
      <div className="inline-form">
        <Field label="Corpus name" error={error}>
          {(a11y) => <TextInput {...a11y} value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />}
        </Field>
        <Button type="submit" loading={rename.isPending} disabled={name.trim() === c.name}>
          Rename
        </Button>
      </div>
    </form>
  );
}

function SchemaForm({ c }: { c: Corpus }) {
  const update = useUpdateSchema(c.id);
  const toast = useToast();
  const [drafts, setDrafts] = useState<DraftField[]>([]);
  const [errors, setErrors] = useState<Record<number, string>>({});
  const pending = drafts.filter((d) => d.name.trim() || d.default !== "");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const { schema, errors: errs } = buildSchema(c.metadata_schema, drafts);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const added = Object.keys(schema.fields).filter((k) => !(k in c.metadata_schema.fields));
    if (!added.length) return;
    update.mutate(schema, {
      onSuccess: () =>
        toast({
          tone: "ok",
          title: `Added ${added.join(", ")}`,
          body: "Vector index records are being rewritten with the new field. Vectors come from the cache, so this is quick.",
        }),
    });
  };

  return (
    <form className="form-section" onSubmit={submit} aria-labelledby="schema-title" noValidate>
      <h2 id="schema-title">Metadata fields</h2>
      <p className="form-section__desc">
        Fields every document carries and queries can filter on. Existing fields cannot be changed or removed, because indexes already hold their values.
        Adding a field rewrites each pipeline's vector index records with its default; the vectors themselves are reused, so it is cheap.
      </p>
      <SchemaEditor existing={c.metadata_schema} drafts={drafts} onChange={setDrafts} errors={errors} disabled={update.isPending} />
      {update.error ? <ErrorBox error={update.error} /> : null}
      <div className="form-actions">
        <Button type="submit" variant="primary" loading={update.isPending} disabled={!pending.length}>
          Save {pending.length > 1 ? `${pending.length} new fields` : "new field"}
        </Button>
      </div>
    </form>
  );
}
