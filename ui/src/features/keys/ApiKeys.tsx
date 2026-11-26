import { Check, Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { useApiKeys, useCreateKey, useRevokeKey } from "../../api/hooks";
import type { ApiKey, ApiKeyCreated } from "../../api/types";
import { Button } from "../../components/Button";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { Field, Segmented, TextInput } from "../../components/Field";
import { Badge } from "../../components/Status";
import { Time } from "../../components/Time";
import { EmptyState } from "../../components/States";

/** One-time copy with a short confirmation. */
function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable; the value is still visible to select */
    }
  };
  return (
    <Button variant="secondary" icon={copied ? Check : Copy} onClick={copy}>
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

function NewKeyDialog({ onClose }: { onClose: () => void }) {
  const create = useCreateKey();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"read" | "full">("read");
  const [secret, setSecret] = useState<ApiKeyCreated | null>(null);
  const error = create.error instanceof ApiError ? create.error : null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate({ name: name.trim(), scope }, { onSuccess: setSecret });
  };

  if (secret) {
    return (
      <Dialog open onClose={onClose} title="API key created" width={520}>
        <div className="key-secret">
          <p className="key-secret__warn">
            Copy this key now. It is shown exactly once and cannot be recovered — RAGCentre stores only a hash of it.
          </p>
          <div className="key-secret__row">
            <code className="key-secret__value mono">{secret.key}</code>
            <CopyButton value={secret.key} />
          </div>
          <div className="key-secret__meta">
            <span className="mono">{secret.name}</span>
            <Badge tone={secret.scope === "full" ? "warn" : "ok"}>{secret.scope}</Badge>
          </div>
        </div>
        <div className="form-actions">
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog open onClose={onClose} title="New API key">
      <form className="stack-form" onSubmit={submit}>
        <Field label="Name" hint="What will use this key — e.g. “claude-mcp” or “deploy-script”.">
          {(a) => <TextInput {...a} mono value={name} onChange={(e) => setName(e.target.value)} autoFocus data-autofocus />}
        </Field>
        <Field
          label="Scope"
          hint="Read keys can only search and read; full keys can create, edit and delete. Key management itself is a browser-session action."
        >
          {() => (
            <Segmented
              label="Scope"
              value={scope}
              onChange={setScope}
              options={[
                { value: "read", label: "Read", title: "Search and read only" },
                { value: "full", label: "Full", title: "Read and change everything" },
              ]}
            />
          )}
        </Field>
        {error ? (
          <div className="form-error" role="alert">
            <strong>{error.message}</strong>
          </div>
        ) : null}
        <div className="form-actions">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" icon={KeyRound} loading={create.isPending} disabled={!name.trim()}>
            Create key
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/** List, create and revoke the API keys that MCP and other non-browser clients use. */
export function ApiKeys() {
  const list = useApiKeys();
  const revoke = useRevokeKey();
  const [creating, setCreating] = useState(false);
  const [toRevoke, setToRevoke] = useState<ApiKey | null>(null);
  const items = list.data?.items ?? [];

  return (
    <section className="form-section" aria-labelledby="keys-title">
      <header className="prov-group__head">
        <h2 id="keys-title">API keys</h2>
        <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
          New API key
        </Button>
      </header>
      <p className="form-section__desc">
        Keys let the CLI, an MCP client or a script talk to RAGCentre without a browser session. A key's secret is shown once; revoke one and
        everything using it stops working immediately.
      </p>
      {items.length === 0 ? (
        <EmptyState icon={KeyRound} title="No API keys yet">
          <p>Create a read key for an MCP client, or a full key for the CLI.</p>
        </EmptyState>
      ) : (
        <div className="panel">
          <table className="rc-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Scope</th>
                <th>Created</th>
                <th>Last used</th>
                <th className="rc-right">Revoke</th>
              </tr>
            </thead>
            <tbody>
              {items.map((k) => (
                <tr key={k.id}>
                  <td className="mono">{k.name}</td>
                  <td>
                    <Badge tone={k.scope === "full" ? "warn" : "ok"}>{k.scope}</Badge>
                  </td>
                  <td className="muted">
                    <Time iso={k.created_at} />
                  </td>
                  <td className="muted">{k.last_used_at ? <Time iso={k.last_used_at} /> : <span className="muted">never</span>}</td>
                  <td className="rc-right">
                    <Button variant="ghost" size="sm" icon={Trash2} onClick={() => setToRevoke(k)}>
                      Revoke
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {creating ? <NewKeyDialog onClose={() => setCreating(false)} /> : null}
      <ConfirmDialog
        open={toRevoke !== null}
        onClose={() => setToRevoke(null)}
        title={`Revoke ${toRevoke?.name ?? ""}?`}
        confirmLabel="Revoke key"
        destructive
        pending={revoke.isPending}
        onConfirm={() =>
          toRevoke && revoke.mutate(toRevoke.id, { onSuccess: () => setToRevoke(null) })
        }
        error={revoke.error instanceof ApiError ? <strong>{revoke.error.message}</strong> : null}
      >
        <p>Anything using this key will stop working immediately. This cannot be undone.</p>
      </ConfirmDialog>
    </section>
  );
}
