import { useCan } from "../auth/context";
import { Can } from "../auth/Access";
import { ArrowLeft, CopyPlus, FileSearch, KeyRound, Lock, PlugZap, Trash2, TriangleAlert } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "../../api/client";
import {
  useDeleteProvider,
  useDeleteSecret,
  useHealth,
  usePatchProvider,
  useProvider,
  useProviderTypes,
  useSetSecret,
  useTestProvider,
} from "../../api/hooks";
import type { ParserCheck, Provider, ProviderTestResult, ProviderType, StoreCapabilities, StoreCheck } from "../../api/types";
import { Button, ButtonLink } from "../../components/Button";
import { ConfigFields, LockedSettings } from "../../components/ConfigForm";
import { ConfirmDialog } from "../../components/Dialog";
import { Fingerprint } from "../../components/Fingerprint";
import { ErrorState, Skeleton } from "../../components/States";
import { Badge, StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { configFields, humanize, initialValues, SERVER_FIELDS, toConfig, type FormValues } from "../../lib/configSchema";
import { nextStep } from "../../lib/errors";
import { duration, plural } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { OcrPanel } from "../ocr/OcrPanel";
import { KIND_LABEL } from "./model";
import { SecretInput, type SecretDraft } from "./SecretInput";
import { StoreCapabilitiesPanel } from "./StoreCapabilities";

export function ProviderDetail() {
  const { providerId = "" } = useParams();
  const provider = useProvider(providerId);
  const types = useProviderTypes();
  const p = provider.data;
  const type = types.data?.items.find((t) => t.kind === p?.kind && t.type === p?.type);

  if (provider.isError) {
    return (
      <div className="page-pad">
        <BackLink />
        <ErrorState error={provider.error} onRetry={() => void provider.refetch()} />
      </div>
    );
  }
  if (!p || !type) {
    return (
      <div className="page-pad">
        <BackLink />
        <Skeleton width={220} height={22} />
        <Skeleton width="60%" height={14} />
        <Skeleton width="100%" height={180} />
      </div>
    );
  }
  return <Detail key={p.id} p={p} type={type} />;
}

function BackLink() {
  return (
    <Link to="/providers" className="back-link">
      <ArrowLeft size={14} aria-hidden /> Providers
    </Link>
  );
}

function Detail({ p, type }: { p: Provider; type: ProviderType }) {
  const manage = useCan("manage");
  const platform = useCan("platform");
  const canManage = manage && (!p.builtin || platform);
  const test = useTestProvider(p.id);
  const capabilities = (p.config.capabilities as StoreCapabilities | undefined) ?? type.capabilities;
  const fields = useMemo(() => configFields(type.config_schema), [type]);
  const permanentKeys = new Set(type.fingerprint_fields);
  const operational = fields.filter((f) => !permanentKeys.has(f.key));
  const locked = [
    ...type.fingerprint_fields.map((k) => ({ key: k, label: fields.find((f) => f.key === k)?.label ?? humanize(k), value: p.config[k] })),
  ].filter((i, idx, all) => all.findIndex((j) => j.key === i.key) === idx);
  const serverSet = [...SERVER_FIELDS].filter((k) => k in p.config && !permanentKeys.has(k)).map((k) => ({ key: k, label: humanize(k), value: p.config[k] }));

  return (
    <div className="page-pad prov-detail">
      <header className="prov-detail__head">
        <BackLink />
        <div className="prov-detail__title">
          <h1 className="mono">{p.name}</h1>
          <Badge>{KIND_LABEL[p.kind].one}</Badge>
          <span className="muted">{type.label}</span>
          {p.builtin ? <Badge tone="accent">built in</Badge> : null}
        </div>
        <dl className="prov-facts">
          <div>
            <dt>Fingerprint</dt>
            <dd>
              <Fingerprint value={p.fingerprint} length={12} label={`${p.name} fingerprint`} />
            </dd>
          </div>
          <div>
            <dt>Used by</dt>
            <dd>{p.used_by_pipelines ? plural(p.used_by_pipelines, "pipeline") : <span className="muted">no pipelines</span>}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>
              <Time iso={p.updated_at} />
            </dd>
          </div>
        </dl>
        <div className="prov-detail__actions">{canManage ? <>
          <Button variant="primary" icon={PlugZap} loading={test.isPending} onClick={() => test.mutate()}>
            Test connection
          </Button>
          {p.kind === "parser" && p.type === "http" ? (
            <Button icon={FileSearch} loading={test.isPending} onClick={() => test.mutate(true)}>
              Test with a sample document
            </Button>
          ) : null}
          {p.kind === "vector_store" ? (
            <Button icon={FileSearch} loading={test.isPending} onClick={() => test.mutate(true)}>
              Test with live data
            </Button>
          ) : null}
          <ButtonLink to={`/providers/new?from=${p.id}`} icon={CopyPlus}>
            New provider from this one
          </ButtonLink>
          <span className="page-head__spacer" />
          <DeleteAction p={p} />
        </> : null}</div>
      </header>

      {p.kind === "parser" && p.type === "http" ? <Can capability="platform"><OcrPanel provider={p} /></Can> : null}

      <TestResultView result={test.data} error={test.error} />
      {p.kind === "connector" ? <p className="prov-section__desc">A connection check verifies the saved connector credentials and service. It does not verify access to a source's bucket, drive, folder, space or page; run a source sync to check that scope.</p> : null}

      {capabilities ? <StoreCapabilitiesPanel caps={capabilities} immutable={[]} /> : null}

      {operational.length ? canManage ? <SettingsForm p={p} fields={operational} /> : <section className="prov-section"><h2>Settings</h2><LockedSettings items={operational.map((f) => ({ key: f.key, label: f.label, value: p.config[f.key] }))} /></section> : null}

      {locked.length || serverSet.length ? (
        <section className="prov-section" aria-labelledby="perm-title">
          <h2 id="perm-title">
            <Lock size={14} strokeWidth={2} aria-hidden /> {p.kind === "vector_store" ? "Set once" : "Permanent settings"}
          </h2>
          <LockedSettings items={[...locked, ...serverSet]}>
            <p className="prov-perm__why">
              {p.kind === "vector_store"
                ? `These settings shape the index ${p.name} builds, so they are set once and cannot change. Changing one would leave existing indexes inconsistent with the store.`
                : `These settings decide what ${p.name} produces, so they are part of its fingerprint and cannot change. Pipelines that used it would otherwise mix results from two different services.`}{" "}
              {p.builtin || !canManage ? null : (
                <Link to={`/providers/new?from=${p.id}`} className="inline-link">
                  Create a new provider from this one
                </Link>
              )}
              {p.builtin || !canManage ? null : " to use different values."}
            </p>
          </LockedSettings>
        </section>
      ) : null}

      {canManage && type.secret_keys.length ? <Secrets p={p} keys={type.secret_keys} /> : null}
    </div>
  );
}

// ---------- test ----------

export function TestResultView({ result, error }: { result: ProviderTestResult | undefined; error: unknown }) {
  if (error) return <ErrorState error={error} title="The test could not run" />;
  if (!result) return null;
  return (
    <section className={`prov-test ${result.ok ? "prov-test--ok" : "prov-test--fail"}`} aria-live="polite" aria-labelledby="test-title">
      <h2 id="test-title" className="prov-test__title">
        <StatusIndicator state={result.ok ? "succeeded" : "failed"} label={result.ok ? "Connection works" : result.version_drift ? "Version changed" : "Connection failed"} />
        <span className="muted num">{duration(result.latency_ms)}</span>
      </h2>
      {result.version_drift ? (
        <div className="prov-drift" role="alert">
          <TriangleAlert size={16} strokeWidth={2} aria-hidden />
          <div>
            <p>
              The service now reports version <span className="mono">{result.version_drift.current}</span>, but this provider was pinned to{" "}
              <span className="mono">{result.version_drift.pinned}</span>. Review the service change and create a new provider to use the new version.
            </p>
          </div>
        </div>
      ) : result.message ? (
        <p className="prov-test__msg mono">{result.message}</p>
      ) : null}
      <dl className="prov-test__facts">
        {result.service ? (
          <>
            <div>
              <dt>Service</dt>
              <dd className="mono">
                {result.service.name} {result.service.version}
              </dd>
            </div>
            <div>
              <dt>Accepts</dt>
              <dd className="mono">{result.service.mime_types.join(", ") || "—"}</dd>
            </div>
          </>
        ) : null}
        {result.dimensions !== null ? (
          <div>
            <dt>Vector size</dt>
            <dd className="num mono">{result.dimensions} dimensions</dd>
          </div>
        ) : null}
      </dl>
      {result.check ? (isStoreCheck(result.check) ? <StoreCheckView check={result.check} /> : <ParserCheckView check={result.check} />) : null}
    </section>
  );
}

const isStoreCheck = (c: ParserCheck | StoreCheck): c is StoreCheck => "store_type" in c;

const LEVEL_LABEL = { error: "Problem", warn: "Warning", info: "OK" } as const;

/** What a sample document showed about a parser, one line per finding. A warning names the
 *  RAGCentre feature that the problem costs. */
function ParserCheckView({ check }: { check: ParserCheck }) {
  const shown = check.findings.filter((f) => f.level !== "info");
  return (
    <div className="prov-check">
      <p className="prov-check__sum">
        Parsed a sample document in <span className="num">{check.seconds.toFixed(1)} s</span>: <span className="num">{check.blocks}</span> blocks on{" "}
        <span className="num">{check.pages}</span> pages. {shown.length ? `${shown.length} finding${shown.length === 1 ? "" : "s"}.` : "Nothing to fix."}
      </p>
      {shown.length ? (
        <ul className="prov-check__list">
          {shown.map((f, i) => (
            <li key={i} className={`prov-check__item prov-check__item--${f.level}`}>
              <strong>{LEVEL_LABEL[f.level]}</strong> <span className="mono">{f.check}</span> {f.message}
              {f.lost ? <span className="muted"> Loses: {f.lost}.</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** What live data showed about a vector store, one line per finding. Each finding names the
 *  capability it proves, with its timing; a warning names the RAGCentre feature it costs. */
function StoreCheckView({ check }: { check: StoreCheck }) {
  const shown = check.findings.filter((f) => f.level !== "info");
  return (
    <div className="prov-check">
      <p className="prov-check__sum">
        Wrote <span className="num">{check.records}</span> records to <span className="mono">{check.collection}</span> and checked{" "}
        <span className="num">{check.metrics.length}</span> metric{check.metrics.length === 1 ? "" : "s"} in{" "}
        <span className="num">{check.seconds.toFixed(1)} s</span>. {shown.length ? `${shown.length} finding${shown.length === 1 ? "" : "s"}.` : "Nothing to fix."}
      </p>
      {shown.length ? (
        <ul className="prov-check__list">
          {shown.map((f, i) => (
            <li key={i} className={`prov-check__item prov-check__item--${f.level}`}>
              <strong>{LEVEL_LABEL[f.level]}</strong> <span className="mono">{f.check}</span> {f.message}{" "}
              <span className="muted num">({f.ms.toFixed(0)} ms)</span>
              {f.lost ? <span className="muted"> Loses: {f.lost}.</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// ---------- operational settings ----------

function SettingsForm({ p, fields }: { p: Provider; fields: ReturnType<typeof configFields> }) {
  const patch = usePatchProvider(p.id);
  const toast = useToast();
  const initial = useMemo(() => initialValues(fields, p.config), [fields, p.config]);
  const [values, setValues] = useState<FormValues>(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const dirty = JSON.stringify(values) !== JSON.stringify(initial);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const { config, errors: errs } = toConfig(fields, values);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    patch.mutate(config, {
      onSuccess: (updated) => {
        setValues(initialValues(fields, updated.config));
        toast({ tone: "ok", title: `Saved ${updated.name}`, body: "These settings take effect on the next call; no work is redone." });
      },
    });
  };

  return (
    <form className="prov-section" onSubmit={submit} aria-labelledby="ops-title" noValidate>
      <h2 id="ops-title">Settings</h2>
      <p className="prov-section__desc">Operational settings: changing them does not change what the provider produces, so no work is redone.</p>
      <ConfigFields fields={fields} values={values} errors={errors} onChange={(k, v) => setValues((x) => ({ ...x, [k]: v }))} disabled={patch.isPending} />
      {patch.error ? <ApiErrorBox error={patch.error} /> : null}
      <div className="form-actions">
        <Button type="submit" variant="primary" disabled={!dirty} loading={patch.isPending}>
          Save settings
        </Button>
        <Button variant="ghost" disabled={!dirty || patch.isPending} onClick={() => { setValues(initial); setErrors({}); }}>
          Discard changes
        </Button>
      </div>
    </form>
  );
}

function ApiErrorBox({ error }: { error: unknown }) {
  const api = error instanceof ApiError ? error : null;
  return (
    <div className="form-error" role="alert">
      <strong>{api?.message ?? String(error)}</strong>
      {nextStep(error) ? <p>{nextStep(error)}</p> : null}
      {api ? <code>{api.code}</code> : null}
    </div>
  );
}

// ---------- secrets ----------

function Secrets({ p, keys }: { p: Provider; keys: string[] }) {
  const health = useHealth();
  const storage = health.data?.secret_storage_enabled ?? true;
  return (
    <section id="secrets" className="prov-section" aria-labelledby="secrets-title">
      <h2 id="secrets-title">
        <KeyRound size={14} strokeWidth={2} aria-hidden /> Secrets
      </h2>
      <p className="prov-section__desc">Write-only. RAGCentre shows whether a secret is set and where it comes from; the value is never sent back.</p>
      <div className="secret-list">
        {keys.map((k) => (
          <SecretRow key={k} p={p} secretKey={k} storageEnabled={storage} />
        ))}
      </div>
    </section>
  );
}

function SecretRow({ p, secretKey, storageEnabled }: { p: Provider; secretKey: string; storageEnabled: boolean }) {
  const state = p.secrets[secretKey] ?? { source: "env" as const, set: false, env_var: null };
  const set = useSetSecret(p.id);
  const remove = useDeleteSecret(p.id);
  const toast = useToast();
  const [editing, setEditing] = useState(!state.set);
  const [draft, setDraft] = useState<SecretDraft>({ mode: "env", value: "" });
  const [error, setError] = useState<string | undefined>();

  const save = () => {
    if (!draft.value.trim()) {
      setError(draft.mode === "env" ? "Enter the variable name." : "Enter the value.");
      return;
    }
    setError(undefined);
    set.mutate(
      { key: secretKey, body: draft.mode === "env" ? { env: draft.value.trim() } : { value: draft.value } },
      {
        onSuccess: () => {
          setDraft({ mode: "env", value: "" });
          setEditing(false);
          toast({ tone: "ok", title: `${secretKey} set on ${p.name}` });
        },
      },
    );
  };

  return (
    <div className="secret-row">
      <div className="secret-row__state">
        <span className="mono secret-row__key">{secretKey}</span>
        {state.set ? (
          <StatusIndicator state="succeeded" label={state.source === "env" ? `Set · from environment variable ${state.env_var ?? ""}` : "Set · stored encrypted"} />
        ) : (
          <StatusIndicator state="pending" label="Not set" />
        )}
        <span className="page-head__spacer" />
        {state.set && !editing ? (
          <>
            <Button size="sm" onClick={() => setEditing(true)}>
              Replace
            </Button>
            <Button size="sm" variant="ghost" loading={remove.isPending} onClick={() => remove.mutate(secretKey, { onSuccess: () => setEditing(true) })}>
              Remove
            </Button>
          </>
        ) : null}
      </div>
      {editing ? (
        <div className="secret-row__edit">
          <SecretInput secretKey={secretKey} draft={draft} onChange={setDraft} storageEnabled={storageEnabled} error={error} disabled={set.isPending} />
          {set.error ? <ApiErrorBox error={set.error} /> : null}
          <div className="form-actions">
            <Button size="sm" variant="primary" loading={set.isPending} onClick={save}>
              Save {secretKey}
            </Button>
            {state.set ? (
              <Button size="sm" variant="ghost" onClick={() => { setEditing(false); setDraft({ mode: "env", value: "" }); setError(undefined); }}>
                Cancel
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ---------- delete ----------

function DeleteAction({ p }: { p: Provider }) {
  const [open, setOpen] = useState(false);
  const del = useDeleteProvider();
  const navigate = useNavigate();
  const toast = useToast();
  if (p.builtin) {
    return (
      <span className="muted prov-builtin-note">
        <Lock size={12} aria-hidden /> Built-in providers cannot be deleted.
      </span>
    );
  }
  const err = del.error instanceof ApiError ? del.error : null;
  return (
    <>
      <Button variant="danger" icon={Trash2} onClick={() => { del.reset(); setOpen(true); }}>
        Delete
      </Button>
      <ConfirmDialog
        open={open}
        onClose={() => setOpen(false)}
        title={`Delete ${p.name}?`}
        confirmLabel="Delete provider"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          del.mutate(p.id, {
            onSuccess: () => {
              setOpen(false);
              toast({ tone: "ok", title: `Deleted ${p.name}` });
              navigate("/providers");
            },
          })
        }
        error={
          err ? (
            <>
              <strong>{err.message}</strong>
              {err.code !== "provider_in_use" && nextStep(err) ? <p>{nextStep(err)}</p> : null}
            </>
          ) : null
        }
      >
        <p>
          <span className="mono">{p.name}</span> and its secrets are removed. Pipelines refer to providers by name, so a new pipeline cannot use it afterwards.
        </p>
        {p.used_by_pipelines ? (
          <p className="dialog-warn">
            It is used by {plural(p.used_by_pipelines, "pipeline")}, so the server will refuse until they are archived.
          </p>
        ) : (
          <p className="muted">No pipeline uses it.</p>
        )}
      </ConfirmDialog>
    </>
  );
}
