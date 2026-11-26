import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router";
import { ApiError } from "../../api/client";
import { useAuthConfig, useMe, useSetup } from "../../api/hooks";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { NoAccount } from "./RequireAuth";

const USERNAME_RE = /^[a-z0-9][a-z0-9_.-]{2,63}$/;

/** First-run account creation; you are signed in immediately afterwards. */
export function Setup() {
  const navigate = useNavigate();
  const config = useAuthConfig();
  const me = useMe();
  const setup = useSetup();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  if (me.isSuccess) return <Navigate to="/" replace />;
  if (config.data && !config.data.setup_required) return <Navigate to="/login" replace />;
  if (config.data && config.data.setup_required && !config.data.setup_allowed) return <NoAccount />;

  const trimmed = username.trim();
  const usernameError = trimmed && !USERNAME_RE.test(trimmed) ? "Lowercase letters, digits, dots, dashes and underscores; at least 3 characters." : null;
  const passwordError = password.length > 0 && password.length < 15 ? "At least 15 characters." : null;
  const confirmError = confirm && confirm !== password ? "Passwords do not match." : null;
  const blocked = !trimmed || !password || Boolean(usernameError || passwordError || confirmError);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    setup.mutate({ username: trimmed, display_name: displayName.trim(), password }, { onSuccess: () => navigate("/", { replace: true }) });
  };

  const error = setup.error instanceof ApiError ? setup.error : null;

  return (
    <form className="auth-form" onSubmit={submit} noValidate>
      <header className="auth-head">
        <h1>Create your account</h1>
        <p>RAGCentre keeps one account. It signs you into this browser and lets you mint API keys for automation.</p>
      </header>
      <Field label="Username" hint="Used to sign in. Lowercase letters, digits, dots, dashes and underscores." error={usernameError}>
        {(a) => <TextInput {...a} mono value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus data-autofocus />}
      </Field>
      <Field label="Display name" hint="Optional.">
        {(a) => <TextInput {...a} value={displayName} onChange={(e) => setDisplayName(e.target.value)} autoComplete="name" />}
      </Field>
      <Field label="Password" hint="At least 15 characters." error={passwordError}>
        {(a) => <TextInput {...a} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />}
      </Field>
      <Field label="Confirm password" error={confirmError}>
        {(a) => <TextInput {...a} type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />}
      </Field>
      {error ? (
        <div className="form-error" role="alert">
          <strong>{error.message}</strong>
        </div>
      ) : null}
      <Button type="submit" variant="primary" loading={setup.isPending} disabled={blocked}>
        Create account
      </Button>
    </form>
  );
}
