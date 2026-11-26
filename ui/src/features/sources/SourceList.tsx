import { Plug, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";
import { ApiError } from "../../api/client";
import { useCorpus, useCreateSource, useDeleteSource, useProviders, useProviderTypes, useSources, useSyncSource } from "../../api/hooks";
import type { Provider, Source } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { Field, Select, TextInput } from "../../components/Field";
import { EmptyState, ErrorState, SkeletonRows } from "../../components/States";
import { Badge } from "../../components/Status";
import { Time } from "../../components/Time";
import { nextStep } from "../../lib/errors";
import { count } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { connectorProviders, parseJsonObject, providerOf, scheduleLabel, scopeText, STATUS_META } from "./model";
import { useAuth, useTransport } from "../auth/context";
import { SavedProviderCheck } from "../providers/ProviderCredentials";
import { InlineConnectorCreate } from "./InlineConnectorCreate";
import { ScheduleFields, ScopeFields } from "./SourceFields";
import { broadScopeMessage, scheduleProblem, scopeProblem, scopeSchemaFor } from "./scopeModel";

export function SourceList() {
  const { corpusId = "" } = useParams();
  const toast = useToast();
  const { generation, teamId } = useAuth();
  const { isCurrent } = useTransport();
  const corpus = useCorpus(corpusId);
  const providers = useProviders();
  const sources = useSources(corpusId);
  const create = useCreateSource(corpusId);
  const sync = useSyncSource(corpusId);
  const del = useDeleteSource(corpusId);
  const [creating, setCreating] = useState(false);
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Source | null>(null);

  const items = sources.data?.items ?? [];
  const sourceTeamId = corpus.data && "team_id" in corpus.data && typeof corpus.data.team_id === "string" ? corpus.data.team_id : teamId ?? undefined;
  const connectors = connectorProviders(providers.data?.items ?? []).filter((provider) => !sourceTeamId || !("team_id" in provider) || provider.team_id === sourceTeamId);

  const syncNow = async (source: Source) => {
    setSyncingId(source.id);
    try {
      await sync.mutateAsync(source.id);
      toast({ tone: "ok", title: `Syncing ${source.name}`, body: "The sync runs in the background; new and changed documents appear as they are ingested." });
    } catch (e) {
      toast({ tone: "danger", title: `Could not sync ${source.name}`, body: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setSyncingId(null);
    }
  };

  return (
    <div className="sources">
      <header className="pl-head">
        <div className="pl-head__title">
          <h1>Sources</h1>
          {corpus.data ? <p className="pl-head__summary">{count(items.length)} sync source{items.length === 1 ? "" : "s"} pull documents into {corpus.data.name}.</p> : null}
        </div>
        <span className="pl-head__spacer" />
        <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
          New source
        </Button>
      </header>

      {sources.isError && !sources.data ? (
        <ErrorState error={sources.error} onRetry={() => void sources.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Plug}
          title="No sources yet"
          action={
            connectors.length ? (
              <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
                New source
              </Button>
            ) : (
              <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>Add a data connector</Button>
            )
          }
        >
          <p>A source connects a data connector to this corpus and pulls its documents in on a schedule.</p>
          <p>Connectors are providers of kind “Data connector” — S3, SharePoint, Confluence, Google Drive and Notion.</p>
        </EmptyState>
      ) : (
        <div className="pl-tablewrap">
          <table className="rc-table sources-table">
            <caption className="sr-only">Sync sources, with their connector, scope, schedule and status</caption>
            <thead>
              <tr>
                <th>Source</th>
                <th>Connector</th>
                <th>Scope</th>
                <th>Schedule</th>
                <th>Status</th>
                <th className="sources-col-time">Last run</th>
                <th className="rc-col-tight">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {!sources.data ? (
                <SkeletonRows rows={4} cols={[180, 120, 160, 90, 70, 80, 60]} />
              ) : (
                items.map((s) => {
                  const provider = providerOf(providers.data?.items, s);
                  const meta = STATUS_META[s.status];
                  return (
                    <tr key={s.id}>
                      <td className="sources-name">
                        <Link to={`/corpora/${corpusId}/sources/${s.id}`} className="strong-link mono" title={s.name}>
                          {s.name}
                        </Link>
                        {s.last_error ? <span className="sources-error muted" title={s.last_error}>{s.last_error}</span> : null}
                      </td>
                      <td className="muted">{provider ? provider.name : "…"}</td>
                      <td className="mono sources-scope" title={JSON.stringify(s.scope)}>{scopeText(s.scope)}</td>
                      <td className="muted nowrap">{scheduleLabel(s.schedule_seconds)}</td>
                      <td>
                        <Badge tone={meta.tone}>{meta.label}</Badge>
                      </td>
                      <td className="muted nowrap sources-col-time">{s.last_run_at ? <Time iso={s.last_run_at} compact /> : "—"}</td>
                      <td className="rc-col-tight">
                        <span className="sources-actions">
                          <IconButton icon={RefreshCw} label={`Sync ${s.name} now`} onClick={() => void syncNow(s)} disabled={syncingId === s.id || s.status === "syncing"} />
                          <IconButton icon={Trash2} label={`Delete ${s.name}`} onClick={() => { del.reset(); setDeleting(s); }} />
                        </span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {creating ? (
        <SourceCreateDialog
          key={`${generation}:${sourceTeamId}:${corpusId}`}
          teamId={sourceTeamId}
          corpusName={corpus.data?.name ?? "this corpus"}
          connectors={connectors}
          pending={create.isPending}
          onClose={() => setCreating(false)}
          onSubmit={async (body) => {
            const created = await create.mutateAsync(body);
            if (!isCurrent()) return;
            setCreating(false);
            toast({ tone: "ok", title: `Created ${created.name}`, body: scheduleLabel(created.schedule_seconds) === "Manual only" ? "Sync it manually, or edit it to add a schedule." : undefined });
          }}
        />
      ) : null}

      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title={`Delete ${deleting ? deleting.name : ""}?`}
        confirmLabel="Delete source"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          deleting &&
          del.mutate(deleting.id, {
            onSuccess: () => {
              toast({ tone: "ok", title: `Deleted ${deleting.name}`, body: "Documents it already ingested stay in the corpus." });
              setDeleting(null);
            },
          })
        }
        error={del.error instanceof ApiError ? <><strong>{del.error.message}</strong>{nextStep(del.error) ? <p>{nextStep(del.error)}</p> : null}</> : null}
      >
        <p>
          <span className="mono">{deleting?.name}</span> stops pulling new documents into this corpus. Documents it already ingested stay, and are removed only if you delete them.
        </p>
      </ConfirmDialog>
    </div>
  );
}

function SourceCreateDialog({
  teamId,
  corpusName,
  connectors,
  pending,
  onClose,
  onSubmit,
}: {
  teamId?: string;
  corpusName: string;
  connectors: Provider[];
  pending: boolean;
  onClose: () => void;
  onSubmit: (body: { name: string; provider_id: string; scope: Record<string, unknown>; schedule_seconds: number }) => Promise<void>;
}) {
  const types = useProviderTypes();
  const { isCurrent } = useTransport();
  const [name, setName] = useState("");
  const [providerId, setProviderId] = useState("");
  const [scopeTextValue, setScopeTextValue] = useState("{}");
  const [schedule, setSchedule] = useState("3600");
  const [confirmed, setConfirmed] = useState(false);
  const [addingConnector, setAddingConnector] = useState(false);
  const [createdProvider, setCreatedProvider] = useState<Provider | null>(null);
  const [failedSecrets, setFailedSecrets] = useState<string[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [apiError, setApiError] = useState<ApiError | null>(null);
  const allConnectors = createdProvider && !connectors.some((p) => p.id === createdProvider.id) ? [...connectors, createdProvider] : connectors;
  const selected = allConnectors.find((p) => p.id === providerId);
  const schema = scopeSchemaFor(types.data?.items, selected?.type);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setApiError(null);
    const scope = parseJsonObject(scopeTextValue);
    const errs: Record<string, string> = {};
    if (!name.trim()) errs.name = "Give the source a name.";
    if (!providerId) errs.provider = "Choose a data connector.";
    const scopeError = scopeProblem(scopeTextValue, schema);
    if (scopeError) errs.scope = scopeError;
    if (!scopeError && broadScopeMessage(scopeTextValue, schema) && !confirmed) errs.scope = "Confirm the broad import scope before creating this source.";
    const scheduleError = scheduleProblem(schedule);
    if (scheduleError) errs.schedule = scheduleError;
    setErrors(errs);
    if (Object.keys(errs).length || !isCurrent()) return;
    try {
      await onSubmit({ name: name.trim(), provider_id: providerId, scope: scope.value ?? {}, schedule_seconds: Number(schedule) });
    } catch (e) {
      if (!isCurrent()) return;
      if (e instanceof ApiError) {
        setApiError(e);
        if (e.code === "conflict") setErrors((x) => ({ ...x, name: e.message }));
      }
    }
  };

  return (
    <Dialog open onClose={onClose} title={addingConnector ? "Connect a data source" : "New source"} width={640} footer={addingConnector ? undefined : <><Button onClick={onClose}>Cancel</Button><Button variant="primary" loading={pending} disabled={!types.data || !selected} onClick={submit}>Create source</Button></>}>
      {addingConnector ? <InlineConnectorCreate teamId={teamId} onCancel={() => setAddingConnector(false)} onCreated={(provider, missing) => {
        if (!isCurrent()) return;
        setCreatedProvider(provider); setProviderId(provider.id); setFailedSecrets(missing); setConfirmed(false); setAddingConnector(false);
      }} /> : <form className="prov-form" onSubmit={submit} noValidate>
        <Field label="Name *" error={errors.name}>
          {(a11y) => <TextInput {...a11y} mono value={name} onChange={(e) => setName(e.target.value)} placeholder={`e.g. ${corpusName}-manuals`} autoComplete="off" required autoFocus />}
        </Field>
        <Field label="Data connector *" error={errors.provider} hint="The connector provider whose service this source reads from.">
          {(a11y) => <Select {...a11y} value={providerId} onChange={(e) => { setProviderId(e.target.value); setConfirmed(false); }} required>
            <option value="">Choose a connector…</option>
            {allConnectors.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.type})</option>)}
          </Select>}
        </Field>
        <Button icon={Plus} onClick={() => setAddingConnector(true)}>Create a connector here</Button>
        {failedSecrets.length && createdProvider?.id === providerId ? <p className="rc-field__error" role="alert">The connector was created, but {failedSecrets.join(", ")} could not be saved. <Link className="inline-link" to={`/providers/${providerId}`}>Set the missing credentials</Link> before syncing.</p> : null}
        {selected ? <SavedProviderCheck key={selected.id} provider={selected} /> : null}
        {types.isError ? <ErrorState error={types.error} onRetry={() => void types.refetch()} /> : null}
        {selected ? <ScopeFields key={selected.type} schema={schema} value={scopeTextValue} onChange={setScopeTextValue} error={errors.scope} confirmed={confirmed} onConfirm={setConfirmed} /> : null}
        <ScheduleFields value={schedule} onChange={setSchedule} error={errors.schedule} />
        {apiError && apiError.code !== "conflict" ? <div className="form-error" role="alert"><strong>{apiError.message}</strong>{nextStep(apiError) ? <p>{nextStep(apiError)}</p> : null}<code>{apiError.code}</code></div> : null}
      </form>}
    </Dialog>
  );
}
