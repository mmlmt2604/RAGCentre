import { ArrowLeft, RefreshCw, Save, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "../../api/client";
import { useDeleteSource, usePatchSource, useProviders, useSource, useSourceItems, useSyncSource } from "../../api/hooks";
import type { Source } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { Field, TextInput } from "../../components/Field";
import { ErrorState, Skeleton } from "../../components/States";
import { Badge } from "../../components/Status";
import { Time } from "../../components/Time";
import { nextStep } from "../../lib/errors";
import { bytes } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { parseJsonObject, providerOf, scheduleLabel, STATUS_META } from "./model";

export function SourceDetail() {
  const { corpusId = "", sourceId = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const source = useSource(sourceId);
  const providers = useProviders();
  const items = useSourceItems(sourceId);
  const sync = useSyncSource(corpusId);
  const del = useDeleteSource(corpusId);
  const [confirming, setConfirming] = useState(false);

  const s = source.data;
  const provider = s ? providerOf(providers.data?.items, s) : undefined;
  const meta = s ? STATUS_META[s.status] : null;

  const syncNow = async () => {
    if (!s) return;
    try {
      await sync.mutateAsync(s.id);
      toast({ tone: "ok", title: `Syncing ${s.name}`, body: "The sync runs in the background." });
    } catch (e) {
      toast({ tone: "danger", title: `Could not sync ${s.name}`, body: e instanceof ApiError ? e.message : String(e) });
    }
  };

  return (
    <div className="page-pad">
      <header className="page-head">
        <div>
          <Link to={`/corpora/${corpusId}/sources`} className="back-link">
            <ArrowLeft size={14} aria-hidden /> Sources
          </Link>
          <h1>{s ? s.name : <Skeleton width={180} />}</h1>
          {s && meta ? (
            <p className="page-head__sub">
              <Badge tone={meta.tone}>{meta.label}</Badge>{" "}
              {s.last_error ? <span className="muted" title={s.last_error}>Last run failed: {s.last_error}</span> : s.last_run_at ? <>Last ran <Time iso={s.last_run_at} /></> : "Never run yet"}
            </p>
          ) : null}
        </div>
        <span className="page-head__spacer" />
        {s ? (
          <>
            <Button icon={RefreshCw} loading={sync.isPending} onClick={() => void syncNow()} disabled={s.status === "syncing"}>
              Sync now
            </Button>
            <IconButton icon={Trash2} label={`Delete ${s.name}`} onClick={() => { del.reset(); setConfirming(true); }} />
          </>
        ) : null}
      </header>

      {source.isError && !source.data ? (
        <ErrorState error={source.error} onRetry={() => void source.refetch()} />
      ) : s ? (
        <>
          <SourceSettings key={s.id} source={s} corpusId={corpusId} providerName={provider?.name} />

          <section className="prov-section" aria-labelledby="status-title">
            <h2 id="status-title">Schedule & status</h2>
            <dl className="prov-facts">
              <div>
                <dt>Schedule</dt>
                <dd>{scheduleLabel(s.schedule_seconds)}</dd>
              </div>
              <div>
                <dt>Next run</dt>
                <dd>{s.next_run_at ? <Time iso={s.next_run_at} /> : "—"}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd><Badge tone={meta!.tone}>{meta!.label}</Badge></dd>
              </div>
              <div>
                <dt>Last run</dt>
                <dd>{s.last_run_at ? <Time iso={s.last_run_at} /> : "—"}</dd>
              </div>
            </dl>
          </section>
        </>
      ) : null}

      <section className="prov-section" aria-labelledby="items-title">
        <h2 id="items-title">Items</h2>
        {items.isError && !items.data ? (
          <ErrorState error={items.error} onRetry={() => void items.refetch()} />
        ) : !items.data ? (
          <Skeleton width="100%" height={160} />
        ) : items.data.items.length === 0 ? (
          <p className="muted">No items yet — run a sync.</p>
        ) : (
          <div className="pl-tablewrap">
            <table className="rc-table">
              <caption className="sr-only">Remote items this source tracks</caption>
              <thead>
                <tr>
                  <th>Remote item</th>
                  <th>Type</th>
                  <th className="rc-right">Size</th>
                  <th>Version</th>
                  <th>Last seen</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {items.data.items.map((it) => (
                  <tr key={it.remote_id}>
                    <td className="mono">
                      {it.document_id ? (
                        <Link to={`/corpora/${corpusId}/documents/${it.document_id}`} className="strong-link" title={it.remote_id}>
                          {it.remote_path ?? it.remote_id}
                        </Link>
                      ) : (
                        <span title={it.remote_id}>{it.remote_path ?? it.remote_id}</span>
                      )}
                    </td>
                    <td className="muted">{it.remote_mime ?? "—"}</td>
                    <td className="rc-right num mono">{it.remote_size != null ? bytes(it.remote_size) : "—"}</td>
                    <td className="mono muted" title={it.remote_version}>{it.remote_version.length > 12 ? `${it.remote_version.slice(0, 12)}…` : it.remote_version}</td>
                    <td className="muted nowrap"><Time iso={it.last_seen_at} compact /></td>
                    <td>{it.skipped_reason ? <span title={it.skipped_reason}><Badge tone="warn">Skipped</Badge></span> : it.document_id ? <Badge tone="ok">Synced</Badge> : <Badge tone="muted">Pending</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ConfirmDialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Delete ${s?.name ?? ""}?`}
        confirmLabel="Delete source"
        destructive
        pending={del.isPending}
        onConfirm={() =>
          s &&
          del.mutate(s.id, {
            onSuccess: () => {
              toast({ tone: "ok", title: `Deleted ${s.name}`, body: "Documents it already ingested stay in the corpus." });
              navigate(`/corpora/${corpusId}/sources`);
            },
          })
        }
        error={del.error instanceof ApiError ? <><strong>{del.error.message}</strong>{nextStep(del.error) ? <p>{nextStep(del.error)}</p> : null}</> : null}
      >
        <p><span className="mono">{s?.name}</span> stops pulling new documents into this corpus. Documents it already ingested stay.</p>
      </ConfirmDialog>
    </div>
  );
}

function SourceSettings({ source, corpusId, providerName }: { source: Source; corpusId: string; providerName?: string }) {
  const toast = useToast();
  const patch = usePatchSource(source.id, corpusId);
  const [scope, setScope] = useState(() => JSON.stringify(source.scope, null, 2));
  const [schedule, setSchedule] = useState(String(source.schedule_seconds));
  const [metadata, setMetadata] = useState(() => JSON.stringify(source.metadata_template, null, 2));
  const [attributes, setAttributes] = useState(() => JSON.stringify(source.attributes_template, null, 2));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [apiError, setApiError] = useState<ApiError | null>(null);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setApiError(null);
    const scopeObj = parseJsonObject(scope);
    const metaObj = parseJsonObject(metadata);
    const attrObj = parseJsonObject(attributes);
    const scheduleNum = Number(schedule);
    const errs: Record<string, string> = {};
    if (scopeObj.error) errs.scope = scopeObj.error;
    if (metaObj.error) errs.metadata = metaObj.error;
    if (attrObj.error) errs.attributes = attrObj.error;
    if (!Number.isInteger(scheduleNum) || scheduleNum < 0) errs.schedule = "Seconds must be a whole number ≥ 0 (0 = manual only).";
    setErrors(errs);
    if (Object.keys(errs).length) return;
    try {
      await patch.mutateAsync({ scope: scopeObj.value ?? {}, schedule_seconds: scheduleNum, metadata_template: metaObj.value ?? {}, attributes_template: attrObj.value ?? {} });
      toast({ tone: "ok", title: `Saved ${source.name}` });
    } catch (e) {
      if (e instanceof ApiError) setApiError(e);
    }
  };

  return (
    <form className="prov-section" onSubmit={save} aria-labelledby="settings-title" noValidate>
      <h2 id="settings-title">Settings</h2>
      <p className="prov-section__desc">
        Reads from{" "}
        {providerName ? (
          <Link to={`/providers/${source.provider_id}`} className="inline-link mono">{providerName}</Link>
        ) : (
          <span className="mono">{source.provider_id}</span>
        )}
        . To read from a different connector, create a new source.
      </p>
      <Field label="Scope" error={errors.scope} hint="Connector-specific JSON: a bucket, drive, space or database.">
        {(a11y) => <textarea {...a11y} className="rc-input mono" rows={4} value={scope} spellCheck={false} onChange={(e) => setScope(e.target.value)} />}
      </Field>
      <Field label="Schedule (seconds)" error={errors.schedule} hint={`0 = manual only. ${scheduleLabel(Number(schedule) || 0)}`}>
        {(a11y) => <TextInput {...a11y} mono inputMode="numeric" value={schedule} onChange={(e) => setSchedule(e.target.value)} />}
      </Field>
      <Field label="Metadata template" error={errors.metadata} hint="Merged into every document this source ingests.">
        {(a11y) => <textarea {...a11y} className="rc-input mono" rows={3} value={metadata} spellCheck={false} onChange={(e) => setMetadata(e.target.value)} />}
      </Field>
      <Field label="Attributes template" error={errors.attributes} hint="Merged into every document's attributes.">
        {(a11y) => <textarea {...a11y} className="rc-input mono" rows={3} value={attributes} spellCheck={false} onChange={(e) => setAttributes(e.target.value)} />}
      </Field>
      {apiError ? (
        <div className="form-error" role="alert">
          <strong>{apiError.message}</strong>
          {nextStep(apiError) ? <p>{nextStep(apiError)}</p> : null}
          <code>{apiError.code}</code>
        </div>
      ) : null}
      <div className="form-actions">
        <Button type="submit" variant="primary" icon={Save} loading={patch.isPending}>
          Save
        </Button>
      </div>
    </form>
  );
}
