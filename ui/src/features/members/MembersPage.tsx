import { useState, type FormEvent } from "react";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { Field, Select, TextInput } from "../../components/Field";
import { EmptyState, ErrorState, SkeletonRows } from "../../components/States";
import { Badge } from "../../components/Status";
import { useAuth } from "../auth/context";
import type { TeamMember, TeamRole } from "../auth/types";
import { useDirectory, useManagementAction } from "./data";
import { InvitationsPanel } from "./InvitationsPanel";

function RoleSelect({ value, onChange, label, disabled }: { value: TeamRole; onChange: (role: TeamRole) => void; label: string; disabled?: boolean }) {
  return <Select aria-label={label} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value as TeamRole)}><option value="viewer">Viewer</option><option value="editor">Editor</option><option value="owner">Owner</option></Select>;
}
export function MembersPage() {
  const auth = useAuth();
  const teamId = auth.teamId;
  const base = `/teams/${teamId}/members`;
  const members = useDirectory<TeamMember>(base, Boolean(teamId));
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<TeamRole>("viewer");
  const [removing, setRemoving] = useState<TeamMember | null>(null);
  const put = useManagementAction<{ username: string; role: TeamRole }, TeamMember>((value, { request }) => request(`${base}/${encodeURIComponent(value.username)}`, { method: "PUT", body: { role: value.role } }));
  const remove = useManagementAction<string>((name, { request }) => request(`${base}/${encodeURIComponent(name)}`, { method: "DELETE" }));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    put.mutate({ username: username.trim().toLowerCase(), role }, { onSuccess: () => setUsername("") });
  };
  if (!teamId) return <div className="page-pad"><EmptyState title="Select a team"><p>Select a team before managing its members.</p></EmptyState></div>;
  return <div className="page-pad membership-page"><header className="page-head"><h1>Team members</h1><span className="muted">{auth.teams.find((t) => t.team_id === teamId)?.team_name}</span></header>
    <p className="muted">Viewers read and query. Editors also manage corpus content, pipelines and evaluations. Owners also manage providers, sources, members and service tokens.</p>
    <InvitationsPanel />
    <details><summary>Add an existing account directly</summary><p className="muted">This grants access immediately. Use an invitation when the person should confirm joining first.</p>
    <form className="member-add" onSubmit={submit}>
      <Field label="Existing username" hint="Enter the exact username. A platform administrator creates accounts.">{(a) => <TextInput {...a} required maxLength={64} autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} />}</Field>
      <Field label="Role">{(a) => <Select {...a} value={role} onChange={(e) => setRole(e.target.value as TeamRole)}><option value="viewer">Viewer</option><option value="editor">Editor</option><option value="owner">Owner</option></Select>}</Field>
      <Button type="submit" variant="primary" loading={put.isPending}>Add member</Button>
    </form></details>
    <h2>Current members</h2>
    {put.error ? <ErrorState error={put.error} title="Membership change failed" /> : null}
    {members.error ? <ErrorState error={members.error} onRetry={() => void members.refetch()} /> : <div className="panel"><table className="rc-table"><thead><tr><th>Username</th><th>Display name</th><th>Status</th><th>Role</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>
      {!members.data ? <SkeletonRows rows={3} cols={[140, 180, 80, 120, 80]} /> : !members.data.length ? <tr><td colSpan={5}>No members yet. Assign an active owner to initialize this team.</td></tr> : members.data.map((m) => <tr key={m.user_id}>
        <td className="mono">{m.username}</td><td>{m.display_name}</td><td><Badge>{m.status}</Badge></td>
        <td><RoleSelect label={`Role for ${m.username}`} value={m.role} disabled={put.isPending} onChange={(next) => put.mutate({ username: m.username, role: next }, { onSuccess: () => { if (m.user_id === auth.current?.user.id) void auth.refresh(); } })} /></td>
        <td><Button variant="danger" size="sm" onClick={() => { remove.reset(); setRemoving(m); }}>Remove {m.username}</Button></td>
      </tr>)}
    </tbody></table></div>}
    <p className="muted">Every team must keep an active owner once ownership is assigned. Add another active owner before removing or demoting the final owner. Removing a member does not revoke independently issued service tokens.</p>
    <ConfirmDialog open={Boolean(removing)} onClose={() => setRemoving(null)} title={`Remove ${removing?.username ?? "member"}?`} confirmLabel="Remove member" destructive pending={remove.isPending} onConfirm={() => removing && remove.mutate(removing.username, { onSuccess: () => { setRemoving(null); if (removing.user_id === auth.current?.user.id) void auth.refresh(); } })} error={remove.error instanceof Error ? remove.error.message : null}><p>They lose access to this team's content on their next request. Their other team memberships stay active.</p></ConfirmDialog>
  </div>;
}
