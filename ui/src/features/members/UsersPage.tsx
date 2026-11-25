import { useState, type FormEvent } from "react";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { Field, Select, TextInput } from "../../components/Field";
import { ErrorState, SkeletonRows } from "../../components/States";
import { Badge } from "../../components/Status";
import { useAuth } from "../auth/context";
import type { SetupResult, Team, User } from "../auth/types";
import { useDirectory, useManagementAction } from "./data";

export function OneTimeValue({ value, title, onClose }: { value: string; title: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  return <section className="form-section" aria-label={title}><h2>{title}</h2><p>Copy and share this value now. It is shown only once and is not saved in this browser.</p><TextInput aria-label={title} readOnly mono value={value} onFocus={(e) => e.target.select()} /><div className="form-actions"><Button onClick={() => { void Promise.resolve().then(() => navigator.clipboard.writeText(value)).then(() => setCopied(true)).catch(() => setFailed(true)); }}>{copied ? "Copied" : "Copy"}</Button><Button variant="ghost" onClick={onClose}>Dismiss</Button>{failed ? <span role="status">Select the value and copy it manually.</span> : null}</div></section>;
}
const setupLink = (token: string) => new URL(`/setup#token=${encodeURIComponent(token)}`, window.location.origin).toString();
export function UsersPage() {
  const auth = useAuth();
  const users = useDirectory<User>("/users", auth.mode === "local");
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [oneTime, setOneTime] = useState<string>();
  const create = useManagementAction<{ username: string; display_name: string }, SetupResult>((body, { request }) => request("/users", { method: "POST", body }));
  const reissue = useManagementAction<string, SetupResult>((id, { request }) => request(`/users/${id}/setup-token`, { method: "POST" }));
  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate({ username: username.trim().toLowerCase(), display_name: name.trim() }, { onSuccess: (result) => { setOneTime(setupLink(result.setup_token)); setUsername(""); setName(""); } }); };
  if (auth.mode !== "local") return <div className="page-pad"><h1>Users</h1><p>Account management requires a local administrator session.</p></div>;
  return <div className="page-pad membership-page"><header className="page-head"><h1>Users</h1></header>
    <p className="muted">Create a pending account and share its setup link privately. Add team membership separately. Passwords are chosen by each account holder.</p>
    <form className="member-add" onSubmit={submit}>
      <Field label="Username" hint="3–64 lowercase letters, digits, dots, underscores or hyphens.">{(a) => <TextInput {...a} required minLength={3} maxLength={64} pattern={"[a-z0-9][a-z0-9_.\\-]{2,63}"} autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} />}</Field>
      <Field label="Display name">{(a) => <TextInput {...a} required maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />}</Field>
      <Button type="submit" variant="primary" loading={create.isPending}>Create account</Button>
    </form>
    {oneTime ? <OneTimeValue value={oneTime} title="One-time setup link" onClose={() => setOneTime(undefined)} /> : null}
    {create.error || reissue.error ? <ErrorState error={create.error ?? reissue.error} title="Account change failed" /> : null}
    {users.error ? <ErrorState error={users.error} onRetry={() => void users.refetch()} /> : <div className="panel"><table className="rc-table"><thead><tr><th>Username</th><th>Display name</th><th>Status</th><th>Platform role</th><th>Actions</th></tr></thead><tbody>
      {!users.data ? <SkeletonRows rows={3} cols={[140, 180, 80, 120, 200]} /> : users.data.map((user) => <UserRow key={`${user.id}-${user.updated_at}`} user={user} onSetup={() => reissue.mutate(user.id, { onSuccess: (result) => setOneTime(setupLink(result.setup_token)) })} />)}
    </tbody></table></div>}
    <p className="muted">Disabling an account revokes every session. The final active platform administrator and final active team owners are protected by the server. Password recovery is available to the local server operator.</p>
  </div>;
}
function UserRow({ user, onSetup }: { user: User; onSetup: () => void }) {
  const auth = useAuth();
  const [name, setName] = useState(user.display_name);
  const [confirm, setConfirm] = useState<"status" | "admin" | null>(null);
  type Patch = Partial<Pick<User, "display_name" | "status" | "is_platform_admin">>;
  const patch = useManagementAction<Patch, User>((body, { request }) => request(`/users/${user.id}`, { method: "PATCH", body }));
  const disabled = user.status === "disabled";
  const update = (body: Patch) => patch.mutate(body, { onSuccess: () => { setConfirm(null); if (user.id === auth.current?.user.id && (body.status || body.is_platform_admin !== undefined)) void auth.refresh(); } });
  return <tr><td className="mono">{user.username}</td><td><div className="inline-gap"><TextInput aria-label={`Display name for ${user.username}`} value={name} maxLength={128} onChange={(e) => setName(e.target.value)} /><Button size="sm" disabled={!name.trim() || name === user.display_name} loading={patch.isPending} onClick={() => update({ display_name: name.trim() })}>Save</Button></div></td><td><Badge>{user.status}</Badge></td><td>{user.is_platform_admin ? "Administrator" : "Member"}</td><td>
    <div className="inline-gap">{user.status === "pending" ? <Button size="sm" onClick={onSetup}>Reissue setup link</Button> : null}<Button size="sm" variant={disabled ? "secondary" : "danger"} onClick={() => { patch.reset(); setConfirm("status"); }}>{disabled ? "Re-enable" : "Disable"}</Button><Button size="sm" onClick={() => { patch.reset(); setConfirm("admin"); }}>{user.is_platform_admin ? "Remove admin" : "Grant admin"}</Button></div>
    {patch.error && !confirm ? <ErrorState error={patch.error} title="Account change failed" /> : null}
    <ConfirmDialog open={Boolean(confirm)} onClose={() => setConfirm(null)} title={`${confirm === "admin" ? user.is_platform_admin ? "Remove administrator access from" : "Grant administrator access to" : disabled ? "Re-enable" : "Disable"} ${user.username}?`} confirmLabel="Confirm account change" destructive pending={patch.isPending} onConfirm={() => update(confirm === "admin" ? { is_platform_admin: !user.is_platform_admin } : { status: disabled ? "active" : "disabled" })} error={patch.error instanceof Error ? patch.error.message : null}><p>{confirm === "admin" ? "Platform administrators can manage all users and teams. Changing this privilege signs the account out everywhere." : disabled ? "They can sign in again with their established password. Old sessions stay revoked." : "They will be signed out everywhere and cannot sign in until re-enabled. Their memberships are retained."}</p></ConfirmDialog>
  </td></tr>;
}

export function TeamsPage() {
  const auth = useAuth();
  const users = useDirectory<User>("/users", auth.mode === "local");
  const [name, setName] = useState("");
  const [owner, setOwner] = useState("");
  const create = useManagementAction<{ name: string; owner_user_id: string }, Team>((body, { request }) => request("/teams", { method: "POST", body }));
  if (auth.mode !== "local") return <div className="page-pad"><h1>Teams</h1><p>Use a local administrator session to manage teams and initial owners.</p></div>;
  return <div className="page-pad membership-page"><header className="page-head"><h1>Teams</h1></header><p className="muted">New teams require an active initial owner. For an existing team, select it and open Members to assign or change owners.</p>
    <form className="member-add" onSubmit={(e) => { e.preventDefault(); create.mutate({ name: name.trim(), owner_user_id: owner }, { onSuccess: () => { void auth.refresh(); } }); }}>
      <Field label="Team name">{(a) => <TextInput {...a} required maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />}</Field>
      <Field label="Initial owner">{(a) => <Select {...a} required value={owner} onChange={(e) => setOwner(e.target.value)}><option value="">Choose an active account</option>{users.data?.filter((u) => u.status === "active").map((u) => <option key={u.id} value={u.id}>{u.username} · {u.display_name}</option>)}</Select>}</Field>
      <Button type="submit" variant="primary" loading={create.isPending} disabled={!owner}>Create team</Button>
    </form>
    {create.error || users.error ? <ErrorState error={create.error ?? users.error} /> : null}
    <div className="panel"><table className="rc-table"><thead><tr><th>Team</th><th>Slug</th><th>Actions</th></tr></thead><tbody>{auth.teams.map((team) => <tr key={team.team_id}><td>{team.team_name}</td><td className="mono">{team.team_slug}</td><td><Button size="sm" disabled={team.team_id === auth.teamId} onClick={() => auth.switchTeam(team.team_id)}>{team.team_id === auth.teamId ? "Selected" : "Select team"}</Button></td></tr>)}</tbody></table></div>
  </div>;
}
