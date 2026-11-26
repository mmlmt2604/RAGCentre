import { useState, type FormEvent } from "react";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { Field, Select, TextInput } from "../../components/Field";
import { ErrorState, SkeletonRows } from "../../components/States";
import { Badge } from "../../components/Status";
import { Time } from "../../components/Time";
import { ROLE_DESCRIPTION } from "../auth/LinkPages";
import { useAuth } from "../auth/context";
import type { TeamRole } from "../auth/types";
import { useDirectory, useManagementAction } from "./data";
import { OneTimeValue } from "./OneTimeValue";

export interface TeamInvitation {
  id: string;
  team_id: string;
  user_id: string;
  username: string;
  display_name: string;
  role: TeamRole;
  kind: "new_account" | "existing_account";
  status: "pending" | "accepted" | "expired" | "revoked";
  expires_at: string;
}
interface InvitationResult { invitation: TeamInvitation; token: string; expires_at: string; link: string; path: string }

export function InvitationsPanel() {
  const auth = useAuth();
  const base = `/teams/${auth.teamId}/invitations`;
  const invitations = useDirectory<TeamInvitation>(base, Boolean(auth.teamId));
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<TeamRole>("viewer");
  const [issued, setIssued] = useState<InvitationResult>();
  const [action, setAction] = useState<{ invitation: TeamInvitation; kind: "reissue" | "revoke" }>();
  const create = useManagementAction<{ username: string; display_name: string; role: TeamRole }, InvitationResult>((body, { request }) => request(base, { method: "POST", body }));
  const reissue = useManagementAction<string, InvitationResult>((id, { request }) => request(`${base}/${id}/reissue`, { method: "POST" }));
  const revoke = useManagementAction<string>((id, { request }) => request(`${base}/${id}`, { method: "DELETE" }));
  const submit = (event: FormEvent) => {
    event.preventDefault(); setIssued(undefined);
    create.mutate({ username: username.trim().toLowerCase(), display_name: displayName.trim(), role }, { onSuccess: (result) => {
      setIssued(result); setUsername(""); setDisplayName(""); setRole("viewer");
    } });
  };
  const confirm = () => {
    if (!action) return;
    if (action.kind === "reissue") reissue.mutate(action.invitation.id, { onSuccess: (result) => { setIssued(result); setAction(undefined); } });
    else revoke.mutate(action.invitation.id, { onSuccess: () => { if (issued?.invitation.id === action.invitation.id) setIssued(undefined); setAction(undefined); } });
  };
  return <section className="invitations-section" aria-labelledby="invitations-title">
    <h2 id="invitations-title">Invite a teammate</h2>
    <p className="muted">Choose their account and role, then share a one-time link privately. They join this team when they accept. No email service is needed.</p>
    <form className="member-add" onSubmit={submit}>
      <Field label="Username" hint="An existing username, or a new one with 3–64 lowercase letters, digits, dots, underscores or hyphens.">{(a) => <TextInput {...a} required minLength={3} maxLength={64} pattern="[a-z0-9][a-z0-9_.\-]{2,63}" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} />}</Field>
      <Field label="Display name (optional)" hint="Used when creating a new account.">{(a) => <TextInput {...a} maxLength={128} autoComplete="off" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />}</Field>
      <Field label="Team role" hint={ROLE_DESCRIPTION[role]}>{(a) => <Select {...a} value={role} onChange={(e) => setRole(e.target.value as TeamRole)}><option value="viewer">Viewer</option><option value="editor">Editor</option><option value="owner">Owner</option></Select>}</Field>
      <Button variant="primary" type="submit" loading={create.isPending}>Create invitation</Button>
    </form>
    {create.error ? <ErrorState title="Could not create invitation" error={create.error} /> : null}
    {issued ? <><OneTimeValue title={`Invitation for ${issued.invitation.username}`} value={issued.link} onClose={() => setIssued(undefined)} /><p role="status">Share privately with <strong>{issued.invitation.username}</strong>. Expires <Time iso={issued.expires_at} />. Reissuing replaces this link.</p></> : null}
    <h3>Invitations</h3>
    {invitations.error ? <ErrorState error={invitations.error} onRetry={() => void invitations.refetch()} /> : <div className="panel"><table className="rc-table"><caption className="sr-only">Invitations for the selected team</caption><thead><tr><th>Account</th><th>Role</th><th>Status</th><th>Expires</th><th>Actions</th></tr></thead><tbody>
      {!invitations.data ? <SkeletonRows rows={2} cols={[160, 80, 80, 140, 160]} /> : invitations.data.length === 0 ? <tr><td colSpan={5}>No invitations yet. Create one above to add a teammate.</td></tr> : invitations.data.map((invitation) => <tr key={invitation.id}>
        <td><span className="mono">{invitation.username}</span>{invitation.display_name !== invitation.username ? <span className="muted"> · {invitation.display_name}</span> : null}</td><td>{invitation.role}</td><td><Badge>{invitation.status}</Badge></td><td><Time iso={invitation.expires_at} /></td><td><div className="inline-gap">
          {invitation.status === "pending" || invitation.status === "expired" ? <Button size="sm" onClick={() => { reissue.reset(); revoke.reset(); setAction({ invitation, kind: "reissue" }); }}>Reissue link</Button> : null}
          {invitation.status === "pending" ? <Button size="sm" variant="danger" onClick={() => { reissue.reset(); revoke.reset(); setAction({ invitation, kind: "revoke" }); }}>Revoke</Button> : null}
        </div></td>
      </tr>)}
    </tbody></table></div>}
    <ConfirmDialog open={Boolean(action)} onClose={() => setAction(undefined)} title={`${action?.kind === "revoke" ? "Revoke invitation for" : "Replace invitation link for"} ${action?.invitation.username ?? ""}?`} confirmLabel={action?.kind === "revoke" ? "Revoke invitation" : "Create replacement link"} destructive={action?.kind === "revoke"} pending={reissue.isPending || revoke.isPending} onConfirm={confirm} error={(reissue.error ?? revoke.error) instanceof Error ? (reissue.error ?? revoke.error)?.message : undefined}>
      <p>{action?.kind === "revoke" ? "The current link will stop working. This does not disable the account or remove an existing membership." : "The old link will stop working. Copy and share the replacement link after it is created."}</p>
    </ConfirmDialog>
  </section>;
}
