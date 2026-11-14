import { ArrowLeft, Lock } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { ApiError, request } from "../../api/client";
import { keys, useCreateProvider, useHealth, useProvider, useProviders, useProviderTypes } from "../../api/hooks";
import type { Provider, ProviderKind, ProviderType } from "../../api/types";
import { Button } from "../../components/Button";
import { ConfigFields } from "../../components/ConfigForm";
import { Field, TextInput } from "../../components/Field";
import { ErrorState, Skeleton } from "../../components/States";
import { configFields, initialValues, PROVIDER_NAME_HINT, PROVIDER_NAME_RE, toConfig, type FormValues } from "../../lib/configSchema";
import { nextStep } from "../../lib/errors";
import { useToast } from "../../lib/toast";
import { KIND_LABEL, KINDS } from "./model";
import { SecretInput, type SecretDraft } from "./SecretInput";

/** Suggest an unused name for a copy: "bge-m3" -> "bge-m3-2". */
function copyName(name: string, taken: Set<string>): string {
  const base = name.replace(/-\d+$/, "");
  for (let i = 2; i < 100; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
  return `${base}-copy`;
}

export function ProviderNew() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const types = useProviderTypes();
  const providers = useProviders();
  const health = useHealth();
  const from = useProvider(params.get("from") ?? undefined);
  const create = useCreateProvider();
  const qc = useQueryClient();

  const source = from.data;
  const kind = (source?.kind ?? params.get("kind")) as ProviderKind | null;
  const typeName = source?.type ?? params.get("type");
  const typesOfKind = (types.data?.items ?? []).filter((t) => t.kind === kind);
  const type = typesOfKind.find((t) => t.type === typeName) ?? null;

  const setKind = (k: ProviderKind) => setParams(new URLSearchParams({ kind: k }), { replace: true });
  const setType = (t: ProviderType) => setParams(new URLSearchParams({ kind: t.kind, type: t.type }), { replace: true });

  const failed = [types, from, providers].find((query) => query.isError);
  if (failed) return <div className="page-pad"><ErrorState error={failed.error} onRetry={() => void failed.refetch()} /></div>;
  if (params.get("from") && !source) return <div className="page-pad"><Skeleton width="100%" height={240} /></div>;

  return (
    <div className="page-pad prov-new">
      <header className="page-head">
        <div>
          <Link to="/providers" className="back-link">
            <ArrowLeft size={14} aria-hidden /> Providers
          </Link>
          <h1>{source ? `New provider from ${source.name}` : "New provider"}</h1>
        </div>
      </header>

      {source ? (
        <p className="prov-new__note">
          Settings are copied from <span className="mono">{source.name}</span>. Change any of them, including the permanent ones; the copy is a separate
          provider with its own fingerprint, and existing pipelines keep using <span className="mono">{source.name}</span>.
        </p>
      ) : (
        <>
          <section className="prov-step" aria-labelledby="step-kind">
            <h2 id="step-kind" className="prov-step__title">What kind of service?</h2>
            <div className="choice-list" role="radiogroup" aria-labelledby="step-kind">
              {KINDS.map((k) => (
                <label key={k} className="choice" data-on={k === kind || undefined}>
                  <input type="radio" name="kind" value={k} checked={k === kind} onChange={() => setKind(k)} />
                  <span className="choice__title">{KIND_LABEL[k].one}</span>
                  <span className="choice__desc">{KIND_LABEL[k].blurb}</span>
                </label>
              ))}
            </div>
          </section>

          {kind ? (
            <section className="prov-step" aria-labelledby="step-type">
              <h2 id="step-type" className="prov-step__title">Which type?</h2>
              {!types.data ? (
                <Skeleton width={320} height={40} />
              ) : (
                <div className="choice-list" role="radiogroup" aria-labelledby="step-type">
                  {typesOfKind.map((t) => (
                    <label key={t.type} className="choice" data-on={t.type === type?.type || undefined}>
                      <input type="radio" name="type" value={t.type} checked={t.type === type?.type} onChange={() => setType(t)} />
                      <span className="choice__title">
                        {t.label} <span className="cfg-key mono">{t.type}</span>
                      </span>
                      <span className="choice__desc">{t.description}</span>
                    </label>
                  ))}
                </div>
              )}
            </section>
          ) : null}
        </>
      )}

      {type && (!params.get("from") || source) ? (
        <ProviderForm
          key={`${type.kind}/${type.type}/${source?.id ?? ""}`}
          type={type}
          source={source ?? null}
          takenNames={new Set((providers.data?.items ?? []).map((p) => p.name))}
          storageEnabled={health.data?.secret_storage_enabled ?? true}
          pending={create.isPending}
          onSubmit={async (name, config, secrets) => {
            const created = await create.mutateAsync({ name, kind: type.kind, type: type.type, config });
            const failed: string[] = [];
            for (const [key, body] of secrets) {
              try {
                await request<Provider>(`/providers/${created.id}/secrets/${encodeURIComponent(key)}`, { method: "PUT", body });
              } catch {
                failed.push(key);
              }
            }
            void qc.invalidateQueries({ queryKey: keys.providers });
            toast({
              tone: failed.length ? "danger" : "ok",
              title: `Created ${created.name}`,
              body: failed.length ? `Could not set ${failed.join(", ")}; set it on the provider page.` : undefined,
            });
            navigate(`/providers/${created.id}`);
          }}
        />
      ) : null}
    </div>
  );
}

function ProviderForm({
  type,
  source,
  takenNames,
  storageEnabled,
  pending,
  onSubmit,
}: {
  type: ProviderType;
  source: Provider | null;
  takenNames: Set<string>;
  storageEnabled: boolean;
  pending: boolean;
  onSubmit: (name: string, config: Record<string, unknown>, secrets: [string, { env: string } | { value: string }][]) => Promise<void>;
}) {
  const fields = useMemo(() => configFields(type.config_schema), [type]);
  const permanent = fields.filter((f) => type.fingerprint_fields.includes(f.key));
  const operational = fields.filter((f) => !type.fingerprint_fields.includes(f.key));
  const [name, setName] = useState(() => (source ? copyName(source.name, takenNames) : ""));
  const [values, setValues] = useState<FormValues>(() => initialValues(fields, source?.config));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [nameError, setNameError] = useState<string | null>(null);
  const [secrets, setSecrets] = useState<Record<string, SecretDraft>>(() => Object.fromEntries(type.secret_keys.map((k) => [k, { mode: "env", value: "" }])));
  const [apiError, setApiError] = useState<ApiError | null>(null);
  const capturesInfo = type.fingerprint_fields.includes("service_version");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setApiError(null);
    const { config, errors: errs } = toConfig(fields, values);
    const nErr = !name ? "Give the provider a name." : !PROVIDER_NAME_RE.test(name) ? PROVIDER_NAME_HINT : takenNames.has(name) ? `A provider named ${name} already exists.` : null;
    const secretErrs = Object.entries(secrets).filter(([, s]) => s.mode !== "skip" && !s.value.trim());
    setErrors(errs);
    setNameError(nErr);
    if (nErr || Object.keys(errs).length || secretErrs.length) {
      if (secretErrs.length) setErrors((x) => ({ ...x, ...Object.fromEntries(secretErrs.map(([k]) => [`secret:${k}`, "Enter a value, or choose Skip."])) }));
      return;
    }
    const secretBodies = Object.entries(secrets)
      .filter(([, s]) => s.mode !== "skip")
      .map(([k, s]): [string, { env: string } | { value: string }] => [k, s.mode === "env" ? { env: s.value.trim() } : { value: s.value }]);
    try {
      await onSubmit(name, config, secretBodies);
    } catch (err) {
      if (err instanceof ApiError) {
        setApiError(err);
        const problems = (err.details?.problems as { path: string; message: string }[] | undefined) ?? [];
        const byField = Object.fromEntries(problems.filter((p) => p.path.startsWith("config.")).map((p) => [p.path.slice(7), p.message]));
        if (Object.keys(byField).length) setErrors(byField);
        if (err.code === "conflict") setNameError(err.message);
      }
    }
  };

  return (
    <form className="prov-form" onSubmit={submit} noValidate>
      <section className="prov-step" aria-labelledby="step-settings">
        <h2 id="step-settings" className="prov-step__title">Settings</h2>
<div className="field-name">
        <Field label="Name *" hint={PROVIDER_NAME_HINT} error={nameError}>
          {(a11y) => <TextInput {...a11y} mono value={name} onChange={(e) => setName(e.target.value.toLowerCase())} placeholder="e.g. bge-m3" autoComplete="off" required />}
        </Field>
        </div>
        {operational.length ? (
          <ConfigFields fields={operational} values={values} errors={errors} onChange={(k, v) => setValues((x) => ({ ...x, [k]: v }))} disabled={pending} />
        ) : null}
      </section>

      {permanent.length || capturesInfo ? (
        <section className="prov-step prov-perm" aria-labelledby="step-perm">
          <h2 id="step-perm" className="prov-step__title">
            <Lock size={14} strokeWidth={2} aria-hidden /> Permanent once created
          </h2>
          <p className="prov-perm__why">
            These settings change what the provider produces, so they are part of its fingerprint. To change them later you create a new provider, and pipelines
            built on the old one keep their results.
          </p>
          {permanent.length ? (
            <ConfigFields fields={permanent} values={values} errors={errors} onChange={(k, v) => setValues((x) => ({ ...x, [k]: v }))} disabled={pending} />
          ) : null}
          {capturesInfo ? (
            <p className="prov-perm__info">The service's name and version are read from its <span className="mono">/info</span> endpoint when you create the provider and pinned.</p>
          ) : null}
        </section>
      ) : null}

      {type.secret_keys.length ? (
        <section className="prov-step" aria-labelledby="step-secrets">
          <h2 id="step-secrets" className="prov-step__title">Secrets</h2>
          <p className="prov-step__desc">Secrets are write-only: after saving, RAGCentre shows whether one is set and where it comes from, never its value.</p>
          {type.secret_keys.map((k) => (
            <SecretInput
              key={k}
              secretKey={k}
              draft={secrets[k]!}
              onChange={(d) => setSecrets((x) => ({ ...x, [k]: d }))}
              storageEnabled={storageEnabled}
              allowSkip
              error={errors[`secret:${k}`]}
              disabled={pending}
            />
          ))}
        </section>
      ) : null}

      {apiError && apiError.code !== "conflict" ? (
        <div className="form-error" role="alert">
          <strong>{apiError.message}</strong>
          {nextStep(apiError) ? <p>{nextStep(apiError)}</p> : null}
          <code>{apiError.code}</code>
        </div>
      ) : null}

      <div className="form-actions">
        <Button type="submit" variant="primary" loading={pending}>
          {capturesInfo ? "Connect and create" : "Create provider"}
        </Button>
        <Link to="/providers" className="link-btn">
          Cancel
        </Link>
      </div>
    </form>
  );
}
