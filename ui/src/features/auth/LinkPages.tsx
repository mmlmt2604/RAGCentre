import { useEffect, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { ApiError, configureTransport, request } from "../../api/client";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { ErrorState } from "../../components/States";
import { AuthFrame } from "./AuthForms";
import type { CurrentUser, TeamRole } from "./types";

export interface InvitationContext {
  username: string;
  display_name: string;
  team_id: string;
  team_name: string;
  role: TeamRole;
  expires_at: string;
  mode: "activate" | "sign_in";
}
interface Acceptance { current: CurrentUser; team_id: string }
export const ROLE_DESCRIPTION: Record<TeamRole, string> = {
  viewer: "Read documents and run searches.",
  editor: "Also manage documents, pipelines, and evaluations.",
  owner: "Also manage services, sources, members, and team API tokens.",
};

function useLinkToken() {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => new URLSearchParams(location.hash.slice(1)).get("token") ?? "");
  useEffect(() => {
    if (location.hash) void navigate({ pathname: location.pathname, search: location.search }, { replace: true });
  }, [location.hash, location.pathname, location.search, navigate]);
  return token;
}
function finishAccount(result: Acceptance, navigate: ReturnType<typeof useNavigate>) {
  if (result.current.user.is_platform_admin || result.current.memberships.some((m) => m.team_id === result.team_id)) {
    try { sessionStorage.setItem("ragcentre.selected-team", result.team_id); } catch { /* Revalidated by AuthGate. */ }
  }
  configureTransport({ mode: "local", csrf: result.current.csrf_token });
  void navigate("/", { replace: true });
}

export function NewPasswordFields({ password, onChange, confirm, onConfirm }: {
  password: string; onChange: (value: string) => void; confirm: string; onConfirm: (value: string) => void;
}) {
  const [visible, setVisible] = useState(false);
  return <>
    <p className="muted">Use 15–128 characters. Spaces and Unicode are welcome. You can paste a password from your password manager.</p>
    <Field label="New password">{(a) => <TextInput {...a} type={visible ? "text" : "password"} autoComplete="new-password" minLength={15} maxLength={128} required value={password} onChange={(e) => onChange(e.target.value)} />}</Field>
    <Field label="Confirm password" error={confirm && confirm !== password ? "The passwords do not match." : undefined}>{(a) => <TextInput {...a} type={visible ? "text" : "password"} autoComplete="new-password" minLength={15} maxLength={128} required value={confirm} onChange={(e) => onConfirm(e.target.value)} />}</Field>
    <Button variant="ghost" aria-pressed={visible} onClick={() => setVisible(!visible)}>{visible ? "Hide passwords" : "Show passwords"}</Button>
  </>;
}
function UnusableLink({ purpose }: { purpose: string }) {
  return <><p>This {purpose} link is missing, expired, or no longer available. Ask the person who shared it for a new link.</p><Link to="/">Return to sign in</Link></>;
}

export function JoinPage() {
  const token = useLinkToken();
  const navigate = useNavigate();
  const [invitation, setInvitation] = useState<InvitationContext>();
  const [current, setCurrent] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(Boolean(token));
  const [loadError, setLoadError] = useState<unknown>();
  const [error, setError] = useState<unknown>();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!token) return;
    let active = true;
    void Promise.all([
      request<InvitationContext>("/auth/invitations/inspect", { method: "POST", body: { token }, auth: "public" }),
      request<CurrentUser>("/auth/me", { auth: "public" }).catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }),
    ]).then(([info, session]) => {
      if (active) { setInvitation(info); setCurrent(session); }
    }).catch((e: unknown) => { if (active) setLoadError(e); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [token]);
  const wrongAccount = Boolean(current && invitation && current.user.username !== invitation.username);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!invitation || wrongAccount) return;
    setError(undefined); setBusy(true);
    try {
      if (!current && invitation.mode === "sign_in") {
        const user = await request<CurrentUser>("/auth/login", { method: "POST", auth: "public", body: { username: invitation.username, password } });
        setCurrent(user); setPassword("");
      } else if (!current) {
        if (password !== confirm) throw new Error("The passwords do not match.");
        const result = await request<Acceptance>("/auth/invitations/activate", { method: "POST", auth: "public", body: { token, password } });
        setPassword(""); setConfirm(""); finishAccount(result, navigate);
      } else {
        configureTransport({ mode: "local", csrf: current.csrf_token });
        const result = await request<Acceptance>("/auth/invitations/accept", { method: "POST", auth: "session", body: { token } });
        finishAccount(result, navigate);
      }
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const signOut = async () => {
    if (!current) return;
    setBusy(true); setError(undefined);
    try {
      configureTransport({ mode: "local", csrf: current.csrf_token });
      await request("/auth/logout", { method: "POST", auth: "session" });
      setCurrent(null);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <AuthFrame title={invitation ? `Join ${invitation.team_name}` : "Join your team"}>
    {loading ? <p role="status">Checking your invitation and account…</p> : !token || loadError || !invitation ? <>{loadError instanceof ApiError && loadError.isNetwork ? <ErrorState error={loadError} /> : <UnusableLink purpose="invitation" />}</> : <>
      <dl className="auth-context"><div><dt>Account</dt><dd><span className="mono">{invitation.username}</span>{invitation.display_name && invitation.display_name !== invitation.username ? ` · ${invitation.display_name}` : ""}</dd></div><div><dt>Team role</dt><dd>{invitation.role.charAt(0).toUpperCase() + invitation.role.slice(1)}<span className="muted"> — {ROLE_DESCRIPTION[invitation.role]}</span></dd></div></dl>
      <p className="muted">All members of this team can read its documents.</p>
      {wrongAccount ? <><p>You are signed in as <strong>{current?.user.username}</strong>. Sign out, then continue as <strong>{invitation.username}</strong> to accept this invitation.</p><Button loading={busy} onClick={() => void signOut()}>Sign out of {current?.user.username}</Button></> : <form className="auth-fields" onSubmit={(e) => void submit(e)}>
        {!current && invitation.mode === "activate" ? <NewPasswordFields password={password} onChange={setPassword} confirm={confirm} onConfirm={setConfirm} /> : !current ? <>
          <Field label="Username">{(a) => <TextInput {...a} value={invitation.username} readOnly autoComplete="username" />}</Field>
          <Field label="Password">{(a) => <TextInput {...a} type="password" autoComplete="current-password" required maxLength={128} value={password} onChange={(e) => setPassword(e.target.value)} />}</Field>
        </> : <p>Signed in as <strong>{current.user.username}</strong>. Accepting adds this team to your account.</p>}
        <Button variant="primary" type="submit" loading={busy}>{current ? "Accept invitation" : invitation.mode === "activate" ? "Create account and join team" : "Sign in to accept"}</Button>
      </form>}
      {error ? <ErrorState title="Could not continue" error={error} /> : null}
      <p className="muted">If account creation succeeded but the connection was interrupted, sign in normally to continue.</p>
    </>}
  </AuthFrame>;
}

export function RecoveryPage() {
  const token = useLinkToken();
  const [info, setInfo] = useState<{ username: string; expires_at: string }>();
  const [loading, setLoading] = useState(Boolean(token));
  const [loadError, setLoadError] = useState<unknown>();
  const [error, setError] = useState<unknown>();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!token) return;
    let active = true;
    void request<{ username: string; expires_at: string }>("/auth/recovery/inspect", { method: "POST", auth: "public", body: { token } })
      .then((value) => { if (active) setInfo(value); }).catch((e: unknown) => { if (active) setLoadError(e); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [token]);
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setError(undefined);
    if (password !== confirm) { setError(new Error("The passwords do not match.")); return; }
    setBusy(true);
    try { await request("/auth/recovery/complete", { method: "POST", auth: "public", body: { token, password } }); setPassword(""); setConfirm(""); setDone(true); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <AuthFrame title={done ? "Password reset" : "Reset your password"}>
    {done ? <><p>Your password is set and previous sessions have been signed out. Sign in with your new password.</p><Link to="/" className="link-btn">Go to sign in</Link></> : loading ? <p role="status">Checking your recovery link…</p> : !token || loadError || !info ? <>{loadError instanceof ApiError && loadError.isNetwork ? <ErrorState error={loadError} /> : <UnusableLink purpose="recovery" />}</> : <>
      <p>Choose a new password for <strong>{info.username}</strong>. This signs out all previous sessions.</p>
      <form className="auth-fields" onSubmit={(e) => void submit(e)}><NewPasswordFields password={password} onChange={setPassword} confirm={confirm} onConfirm={setConfirm} />{error ? <ErrorState error={error} title="Password reset failed" /> : null}<Button type="submit" variant="primary" loading={busy}>Reset password</Button></form>
    </>}
  </AuthFrame>;
}

export function InitializePage() {
  const token = useLinkToken();
  const navigate = useNavigate();
  const [teamName, setTeamName] = useState("Default");
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(Boolean(token));
  const [loadError, setLoadError] = useState<unknown>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!token) return;
    let active = true;
    void request<{ team_name: string }>("/auth/bootstrap/inspect", { method: "POST", auth: "public", body: { token } })
      .then((value) => { if (active) { setTeamName(value.team_name); setReady(true); } }).catch((e: unknown) => { if (active) setLoadError(e); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [token]);
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setError(undefined);
    if (password !== confirm) { setError(new Error("The passwords do not match.")); return; }
    setBusy(true);
    try {
      const result = await request<Acceptance>("/auth/bootstrap/complete", { method: "POST", auth: "public", body: { token, username: username.trim().toLowerCase(), display_name: name.trim() || username.trim(), password, team_name: teamName.trim() } });
      setPassword(""); setConfirm(""); finishAccount(result, navigate);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <AuthFrame title="Set up this RAGCentre server">
    {loading ? <p role="status">Checking installation authorization…</p> : !token || loadError || !ready ? <>{loadError instanceof ApiError && loadError.isNetwork ? <ErrorState error={loadError} /> : <><p>This installation link is missing, expired, or no longer available. A server operator can issue a new one:</p><pre className="mono">ragcentre user setup-link</pre><p className="muted">The server must use local authentication with its browser origin configured.</p></>}</> : <>
      <p>Create the first administrator account. You will own the existing default team; its documents and pipelines stay in place.</p>
      <form className="auth-fields" onSubmit={(e) => void submit(e)}>
        <Field label="Username" hint="3–64 lowercase letters, digits, dots, underscores or hyphens.">{(a) => <TextInput {...a} required autoComplete="username" minLength={3} maxLength={64} pattern="[a-z0-9][a-z0-9_.\-]{2,63}" value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} />}</Field>
        <Field label="Display name (optional)">{(a) => <TextInput {...a} autoComplete="name" maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />}</Field>
        <Field label="Team name">{(a) => <TextInput {...a} required maxLength={128} value={teamName} onChange={(e) => setTeamName(e.target.value)} />}</Field>
        <NewPasswordFields password={password} onChange={setPassword} confirm={confirm} onConfirm={setConfirm} />
        {error ? <ErrorState error={error} title="Administrator setup failed" /> : null}<Button type="submit" variant="primary" loading={busy}>Create administrator and continue</Button>
      </form>
      <p className="muted">If setup completed before a connection error, return to sign in with this account.</p>
    </>}
  </AuthFrame>;
}
