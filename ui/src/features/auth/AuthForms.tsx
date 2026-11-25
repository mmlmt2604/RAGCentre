import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router";
import { request } from "../../api/client";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { ErrorState } from "../../components/States";
import { useAuth, useTransport } from "./context";
import type { CurrentUser } from "./types";

export function AuthFrame({ title, children }: { title: string; children: ReactNode }) {
  return <main id="main" className="auth-page"><div className="auth-form"><h1>{title}</h1>{children}</div></main>;
}
export function Login({ onLogin, onLegacy, notice }: { onLogin: (current: CurrentUser) => Promise<void>; onLegacy: () => void; notice?: string }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(undefined);
    try {
      const current = await request<CurrentUser>("/auth/login", { method: "POST", body: { username: username.trim().toLowerCase(), password }, auth: "public" });
      setPassword(""); await onLogin(current);
    } catch (e) { setError(e); setPassword(""); } finally { setBusy(false); }
  };
  return <AuthFrame title="Sign in to RAGCentre">
    <p className="muted">Use the local account created by your platform administrator.</p>
    {notice ? <p role="status">{notice}</p> : null}
    <form onSubmit={(e) => void submit(e)} className="auth-fields">
      <Field label="Username">{(a) => <TextInput {...a} autoComplete="username" autoCapitalize="none" required maxLength={64} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />}</Field>
      <Field label="Password">{(a) => <TextInput {...a} type="password" autoComplete="current-password" required maxLength={128} value={password} onChange={(e) => setPassword(e.target.value)} />}</Field>
      {error ? <ErrorState error={error} title="Sign-in failed" /> : null}
      <Button type="submit" variant="primary" loading={busy}>Sign in</Button>
    </form>
    <p className="muted">Need a password reset? Contact the server operator.</p>
    <Button variant="ghost" onClick={onLegacy}>Use a legacy API token</Button>
  </AuthFrame>;
}

export function SetupPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => new URLSearchParams(location.hash.slice(1)).get("token") ?? "");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [mismatch, setMismatch] = useState(false);
  // Remove the one-time credential from the address bar before it can be copied or revisited.
  useEffect(() => { if (location.hash) void navigate(location.pathname, { replace: true }); }, [location.hash, location.pathname, navigate]);
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setMismatch(password !== confirm);
    if (password !== confirm) return;
    setBusy(true); setError(undefined);
    try { await request<void>("/auth/setup", { method: "POST", body: { token, password }, auth: "public" }); setPassword(""); setConfirm(""); setDone(true); }
    catch (err) { setError(err); } finally { setBusy(false); }
  };
  return <AuthFrame title={done ? "Account ready" : "Set up your account"}>
    {done ? <><p>Your password is set. Sign in to continue.</p><Button onClick={() => void navigate("/", { replace: true })} variant="primary">Go to sign in</Button></> : !token ? <p>This setup link is missing its token. Ask your platform administrator for a new link.</p> : <form className="auth-fields" onSubmit={(e) => void submit(e)}>
      <p className="muted">Choose a password with 15–128 characters. Spaces and Unicode are welcome.</p>
      <Field label="New password">{(a) => <TextInput {...a} type="password" minLength={15} maxLength={128} required autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />}</Field>
      <Field label="Confirm password" error={mismatch ? "The passwords do not match." : undefined}>{(a) => <TextInput {...a} type="password" minLength={15} maxLength={128} required autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />}</Field>
      {error ? <ErrorState error={error} title="Account setup failed" /> : null}
      <Button type="submit" variant="primary" loading={busy}>Set password</Button>
    </form>}
  </AuthFrame>;
}

export function AccountPage() {
  const auth = useAuth();
  const { request, isCurrent } = useTransport();
  const [currentPassword, setCurrentPassword] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(undefined);
    try {
      await request<void>("/auth/password", { method: "POST", body: { current_password: currentPassword, password } });
      if (isCurrent()) await auth.refresh();
    } catch (err) { if (isCurrent()) setError(err); } finally { if (isCurrent()) { setBusy(false); setCurrentPassword(""); setPassword(""); } }
  };
  return <div className="page-pad"><header className="page-head"><h1>Account</h1></header>
    <p>{auth.current?.user.display_name} <span className="mono muted">{auth.current?.user.username}</span></p>
    <form className="auth-fields account-password" onSubmit={(e) => void submit(e)}>
      <h2>Change password</h2><p className="muted">Changing your password signs out every session. Use 15–128 characters.</p>
      <Field label="Current password">{(a) => <TextInput {...a} type="password" autoComplete="current-password" required value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} />}</Field>
      <Field label="New password">{(a) => <TextInput {...a} type="password" autoComplete="new-password" required minLength={15} maxLength={128} value={password} onChange={(e) => setPassword(e.target.value)} />}</Field>
      {error ? <ErrorState error={error} title="Password change failed" /> : null}
      <Button type="submit" variant="primary" loading={busy}>Change password and sign out</Button>
    </form>
  </div>;
}
