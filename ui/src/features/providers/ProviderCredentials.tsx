import { useState } from "react";
import { useHealth, useTestProvider } from "../../api/hooks";
import type { Provider } from "../../api/types";
import { Button } from "../../components/Button";
import { ErrorState } from "../../components/States";
import { StatusIndicator } from "../../components/Status";
import { SecretInput, type SecretDraft } from "./SecretInput";

export function initialSecretDrafts(keys: string[], storageEnabled: boolean, preferStored = true): Record<string, SecretDraft> {
  return Object.fromEntries(keys.map((key) => [key, { mode: preferStored && storageEnabled ? "value" : "env", value: "" }]));
}

/** Shared by guided setup and provider creation. Values stay only in the form's memory. */
export function ProviderCredentials({ secretKeys, drafts, onChange, storageEnabled, errors = {}, disabled, allowSkip = true, onRecheck }: {
  secretKeys: string[]; drafts: Record<string, SecretDraft>; onChange: (key: string, draft: SecretDraft) => void;
  storageEnabled: boolean; errors?: Record<string, string>; disabled?: boolean; allowSkip?: boolean; onRecheck?: () => void;
}) {
  return <>
    <p className="prov-step__desc">Credentials are write-only. Saved values are encrypted with the server's persistent key and cannot be read back.</p>
    {secretKeys.map((key) => <SecretInput key={key} secretKey={key} draft={drafts[key]!} onChange={(draft) => onChange(key, draft)} storageEnabled={storageEnabled} allowSkip={allowSkip} error={errors[`secret:${key}`]} disabled={disabled} />)}
    {!storageEnabled && onRecheck ? <Button size="sm" onClick={onRecheck}>Recheck credential storage</Button> : null}
  </>;
}

/** Checks a saved provider only; never claims a connector scope or pipeline has been tested. */
export function SavedProviderCheck({ provider }: { provider: Provider }) {
  // Remount the check after a credential/config change so stale success cannot survive edits.
  return <SavedCheck key={`${provider.id}:${provider.updated_at}`} provider={provider} />;
}

function SavedCheck({ provider }: { provider: Provider }) {
  const test = useTestProvider(provider.id);
  const [checked, setChecked] = useState(false);
  const missing = Object.entries(provider.secrets).filter(([, state]) => !state.set).map(([key]) => key);
  const verified = checked && test.data?.ok;
  const failed = test.isError || (checked && test.data && !test.data.ok);
  return <section className="prov-section" aria-label="Saved connection verification">
    <h2>Saved connection</h2>
    <StatusIndicator state={verified ? "succeeded" : failed ? "failed" : "pending"} label={verified ? "Connection verified" : failed ? missing.length ? "Credentials needed" : "Connection failed" : "Saved"} />
    {missing.length ? <p className="rc-field__hint">Not set: {missing.join(", ")}. Some services support anonymous access; a connection check determines whether these credentials are needed.</p> : null}
    <Button loading={test.isPending} onClick={() => { setChecked(false); test.mutate(undefined, { onSuccess: () => setChecked(true) }); }}>Verify saved connection</Button>
    {test.error ? <ErrorState error={test.error} /> : null}
    {checked && test.data ? <p role="status" className={test.data.ok ? "rc-field__hint" : "rc-field__error"}>{test.data.ok ? "The saved provider responded successfully." : test.data.message ?? "The connection failed. Check the saved settings and credentials, then retry."}</p> : null}
    {provider.kind === "connector" ? <p className="rc-field__hint">This check does not verify access to a particular bucket, drive, folder, space or page. Run a source sync to check its actual scope.</p> : <p className="rc-field__hint">Ingestion readiness also depends on the pipeline and a successful document run.</p>}
  </section>;
}

/** A health recheck suitable for setup flows that keep their form mounted. */
export function CredentialStorageRecheck() {
  const health = useHealth();
  return <Button size="sm" loading={health.isFetching} onClick={() => void health.refetch()}>Recheck credential storage</Button>;
}
