import { useState } from "react";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { Field, Select, TextInput } from "../../components/Field";
import { ErrorState } from "../../components/States";
import { useAuth } from "../auth/context";
import { useDirectory, useManagementAction } from "./data";
import { OneTimeValue } from "./OneTimeValue";
interface TeamToken { id: string; name: string; kind: "read" | "full"; created_at: string; last_used_at: string | null }
export function TeamTokensPage() {
  const { teamId } = useAuth();
  const base = `/teams/${teamId}/tokens`;
  const tokens = useDirectory<TeamToken>(base, Boolean(teamId));
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"read" | "full">("read");
  const [secret, setSecret] = useState<string>();
  const [removing, setRemoving] = useState<TeamToken | null>(null);
  const create = useManagementAction<{ name: string; kind: "read" | "full" }, TeamToken & { token: string }>((body, { request }) => request(base, { method: "POST", body }));
  const revoke = useManagementAction<string>((id, { request }) => request(`${base}/${id}`, { method: "DELETE" }));
  return <div className="page-pad membership-page"><header className="page-head"><h1>Team service tokens</h1></header><p className="muted">Service tokens work independently of accounts. Removing a member does not revoke their copies. Rotate tokens separately when access changes.</p>
    {!teamId ? <p>Select a team using a local session to manage its service tokens.</p> : <>
      <form className="member-add" onSubmit={(e) => { e.preventDefault(); create.mutate({ name: name.trim(), kind }, { onSuccess: (result) => { setSecret(result.token); setName(""); } }); }}>
        <Field label="Token name">{(a) => <TextInput {...a} required maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />}</Field><Field label="Access">{(a) => <Select {...a} value={kind} onChange={(e) => setKind(e.target.value as "read" | "full")}><option value="read">Read · automation read allowlist</option><option value="full">Full · team resource management</option></Select>}</Field><Button type="submit" variant="primary" loading={create.isPending}>Create token</Button>
      </form>
      {secret ? <OneTimeValue title="One-time service token" value={secret} onClose={() => setSecret(undefined)} /> : null}
      {tokens.error || create.error ? <ErrorState error={tokens.error ?? create.error} /> : null}
      <div className="panel"><table className="rc-table"><thead><tr><th>Name</th><th>Access</th><th>Last used</th><th>Actions</th></tr></thead><tbody>{tokens.data?.map((token) => <tr key={token.id}><td>{token.name}</td><td>{token.kind}</td><td className="mono">{token.last_used_at ?? "Never"}</td><td><Button variant="danger" size="sm" onClick={() => { revoke.reset(); setRemoving(token); }}>Revoke {token.name}</Button></td></tr>)}</tbody></table></div>
    </>}
    <ConfirmDialog open={Boolean(removing)} onClose={() => setRemoving(null)} title={`Revoke ${removing?.name ?? "token"}?`} confirmLabel="Revoke token" destructive pending={revoke.isPending} onConfirm={() => removing && revoke.mutate(removing.id, { onSuccess: () => setRemoving(null) })} error={revoke.error instanceof Error ? revoke.error.message : null}><p>Automation using this token will stop working on its next request.</p></ConfirmDialog>
  </div>;
}
