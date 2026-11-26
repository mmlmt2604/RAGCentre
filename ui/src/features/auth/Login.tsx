import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";
import { ApiError } from "../../api/client";
import { useAuthConfig, useLogin, useMe } from "../../api/hooks";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";

/** Sign in to an existing account (browser sessions; automation uses API keys instead). */
export function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const config = useAuthConfig();
  const me = useMe();
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname ?? "/";

  if (me.isSuccess) return <Navigate to={from} replace />;
  if (config.data?.setup_required) return <Navigate to="/setup" replace />;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    login.mutate({ username, password }, { onSuccess: () => navigate(from, { replace: true }) });
  };

  const error = login.error instanceof ApiError ? login.error : null;

  return (
    <form className="auth-form" onSubmit={submit} noValidate>
      <header className="auth-head">
        <h1>Sign in</h1>
        <p>Your session is a cookie in this browser. API keys for MCP or the CLI are managed under Account.</p>
      </header>
      <Field label="Username">
        {(a) => (
          <TextInput {...a} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus data-autofocus />
        )}
      </Field>
      <Field label="Password">
        {(a) => <TextInput {...a} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />}
      </Field>
      {error ? (
        <div className="form-error" role="alert">
          <strong>{error.message}</strong>
        </div>
      ) : null}
      <Button type="submit" variant="primary" loading={login.isPending} disabled={!username.trim() || !password}>
        Sign in
      </Button>
    </form>
  );
}
