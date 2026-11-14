import { Play } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { useCreateRun, usePipelines } from "../../api/hooks";
import type { EvalDataset, EvalRun } from "../../api/types";
import { Button } from "../../components/Button";
import { Checkbox, Field, Select, TextInput } from "../../components/Field";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { StatusIndicator } from "../../components/Status";
import { nextStep } from "../../lib/errors";
import { percent } from "../../lib/format";
import { parseK } from "./model";

/** Start a run: a dataset, two or more pipelines (non-ready ones need "allow partial"), k values. */
export function NewRun({
  corpusId,
  datasets,
  initialPipelines,
  initialDataset,
  onDone,
  onCancel,
}: {
  corpusId: string;
  datasets: EvalDataset[];
  initialPipelines: string[];
  initialDataset: string | null;
  onDone: (r: EvalRun) => void;
  onCancel: () => void;
}) {
  const pipelines = usePipelines(corpusId);
  const create = useCreateRun();
  const [dataset, setDataset] = useState(initialDataset ?? datasets[0]?.id ?? "");
  const [chosen, setChosen] = useState<string[]>(initialPipelines);
  const [partial, setPartial] = useState(false);
  const [kText, setKText] = useState("1, 3, 5, 10");
  const [problem, setProblem] = useState<string | null>(null);
  const items = pipelines.data?.items ?? [];
  const k = parseK(kText);
  const err = create.error instanceof ApiError ? create.error : null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!dataset) return setProblem("Choose a dataset.");
    if (chosen.length < 1) return setProblem("Choose the pipelines to evaluate.");
    if (k.error) return setProblem(k.error);
    setProblem(null);
    // Keep the list order, so a/b in comparisons follow the order shown.
    const ids = items.map((p) => p.id).filter((id) => chosen.includes(id));
    create.mutate({ dataset_id: dataset, pipeline_ids: ids, k_values: k.ks, allow_partial: partial }, { onSuccess: onDone });
  };

  return (
    <form className="eval-panel" onSubmit={submit} aria-labelledby="run-title" noValidate>
      <h2 id="run-title">New evaluation run</h2>
      <p className="form-section__desc">Every question is asked of every chosen pipeline. Two or more pipelines are compared pair by pair, with confidence intervals.</p>
      <div className="eval-panel__fields">
        <Field label="Dataset">
          {(a11y) => (
            <Select {...a11y} value={dataset} onChange={(e) => setDataset(e.target.value)}>
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.item_count} questions)
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="k values" hint="Metrics are reported at each k." error={k.error ?? null}>
          {(a11y) => <TextInput {...a11y} mono value={kText} onChange={(e) => setKText(e.target.value)} />}
        </Field>
      </div>
      <fieldset className="eval-pipes">
        <legend>Pipelines</legend>
        {pipelines.isError ? <ErrorState error={pipelines.error} onRetry={() => void pipelines.refetch()} /> : !pipelines.data ? <Skeleton width="100%" height={80} /> : !items.length ? <EmptyState title="No pipelines to evaluate"><p>Build a pipeline in this corpus, then return to start a run.</p></EmptyState> : null}
        {items.map((p) => {
          const blocked = p.status !== "ready" && !partial;
          return (
            <div key={p.id} className="eval-pipes__row" data-disabled={blocked || undefined}>
              <Checkbox
                checked={chosen.includes(p.id)}
                disabled={blocked}
                onChange={(e) => setChosen((xs) => (e.target.checked ? [...xs, p.id] : xs.filter((x) => x !== p.id)))}
                label={<span className="mono">{p.name}</span>}
              />
              <StatusIndicator state={p.status} />
              {blocked ? (
                <span className="muted eval-pipes__why">
                  {percent(p.coverage)} indexed. Comparing a partly built index is misleading; tick “Allow partial indexes” to include it anyway.
                </span>
              ) : null}
            </div>
          );
        })}
      </fieldset>
      <Checkbox
        checked={partial}
        onChange={(e) => {
          setPartial(e.target.checked);
          if (!e.target.checked) setChosen((xs) => xs.filter((id) => items.find((p) => p.id === id)?.status === "ready"));
        }}
        label="Allow partial indexes (results only cover documents indexed so far)"
      />
      {problem ? (
        <p className="rc-field__error" role="alert">
          {problem}
        </p>
      ) : null}
      {err ? (
        <div className="form-error" role="alert">
          <strong>{err.message}</strong>
          {nextStep(err) ? <p>{nextStep(err)}</p> : null}
          <code>{err.code}</code>
        </div>
      ) : null}
      <div className="form-actions">
        <Button type="submit" variant="primary" icon={Play} loading={create.isPending} disabled={!chosen.length || !pipelines.data || pipelines.isError || !dataset}>
          Start run{chosen.length ? ` (${chosen.length} pipeline${chosen.length > 1 ? "s" : ""})` : ""}
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
