import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { keys, useCreateProvider, useHealth, useProviders, useProviderTypes } from "../../api/hooks";
import type { Provider } from "../../api/types";
import { Button } from "../../components/Button";
import { Field, Select } from "../../components/Field";
import { ErrorState, Skeleton } from "../../components/States";
import { useTransport } from "../auth/context";
import { ProviderForm } from "../providers/ProviderNew";

/** The enclosing source form owns the return context; no draft or credential enters a URL. */
export function InlineConnectorCreate({ teamId, onCreated, onCancel }: { teamId?: string; onCreated: (provider: Provider, missing: string[]) => void; onCancel: () => void }) {
  const types = useProviderTypes();
  const providers = useProviders();
  const health = useHealth();
  const create = useCreateProvider();
  const qc = useQueryClient();
  const { request, isCurrent } = useTransport();
  const [typeName, setTypeName] = useState("");
  const [saving, setSaving] = useState(false);
  const type = types.data?.items.find((t) => t.kind === "connector" && t.type === typeName);
  if (types.isError || health.isError) return <ErrorState error={types.error ?? health.error} onRetry={() => { void types.refetch(); void health.refetch(); }} />;
  return <div className="inline-connector">
    <p className="prov-section__desc">Create a connector for this source's team. Your source settings stay here while you connect the service.</p>
    <Field label="Connector type">
      {(a11y) => <Select {...a11y} value={typeName} disabled={saving} onChange={(e) => setTypeName(e.target.value)}>
        <option value="">Choose a connector type…</option>
        {(types.data?.items ?? []).filter((t) => t.kind === "connector").map((t) => <option key={t.type} value={t.type}>{t.label}</option>)}
      </Select>}
    </Field>
    {type ? !health.data ? <Skeleton width="100%" height={120} /> : <ProviderForm key={type.type} type={type} source={null} takenNames={new Set((providers.data?.items ?? []).map((p) => p.name))} storageEnabled={health.data.secret_storage_enabled} preferStored pending={saving} onCancel={onCancel} onRecheckStorage={() => void health.refetch()} onSubmit={async (name, config, secrets) => {
      setSaving(true);
      try {
        const body = { name, kind: "connector" as const, type: type.type, config, ...(teamId ? { team_id: teamId } : {}) };
        const created = await create.mutateAsync(body);
        if (!isCurrent()) return;
        const missing: string[] = [];
        let saved = created;
        for (const [key, body] of secrets) {
          if (!isCurrent()) return;
          try { saved = await request<Provider>(`/providers/${created.id}/secrets/${encodeURIComponent(key)}`, { method: "PUT", body }); }
          catch { missing.push(key); }
        }
        if (!isCurrent()) return;
        void qc.invalidateQueries({ queryKey: keys.providers });
        onCreated(saved, missing);
      } finally { if (isCurrent()) setSaving(false); }
    }} /> : <Button onClick={onCancel}>Back to source</Button>}
  </div>;
}
