import { LogOut, LockKeyhole } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router";
import { ApiError } from "../../api/client";
import { useChangePassword, useLogout, useMe } from "../../api/hooks";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { Badge } from "../../components/Status";
import { useToast } from "../../lib/toast";
import { ApiKeys } from "../keys/ApiKeys";

/** Account details, password change and sign-out. */
function Account() {
  const navigate = useNavigate();
  const toast = useToast();
  const me = useMe();
  const changePassword = useChangePassword();
  const logout = useLogout();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");

  const user = me.data;
  if (me.isError && !me.data) return <Navigate to="/login" replace />;

  const pwError = next.length > 0 && next.length < 15 ? "At least 15 characters." : null;
  const confirmError = confirm && confirm !== next ? "Passwords do not match." : null;
  const blocked = !current || !next || Boolean(pwError || confirmError);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    changePassword.mutate(
      { current_password: current, new_password: next },
      {
        onSuccess: () => {
          toast({ tone: "ok", title: "Password changed", body: "Sign in again with the new password." });
          navigate("/login", { replace: true });
        },
      },
    );
  };

  const error = changePassword.error instanceof ApiError ? changePassword.error : null;

  return (
    <section className="form-section" aria-labelledby="account-title">
      <header className="prov-group__head">
        <h2 id="account-title">Your account</h2>
        <Button
          variant="ghost"
          size="sm"
          icon={LogOut}
          loading={logout.isPending}
          onClick={() => logout.mutate(undefined, { onSuccess: () => navigate("/login", { replace: true }) })}
        >
          Sign out
        </Button>
      </header>
      {user ? (
        <dl className="facts-list">
          <div>
            <dt>Username</dt>
            <dd className="mono">{user.username}</dd>
          </div>
          <div>
            <dt>Display name</dt>
            <dd>{user.display_name || <span className="muted">—</span>}</dd>
          </div>
          <div>
            <dt>Role</dt>
            <dd>{user.is_platform_admin ? <Badge tone="accent">platform admin</Badge> : <Badge>member</Badge>}</dd>
          </div>
        </dl>
      ) : null}

      <form className="stack-form" onSubmit={submit} noValidate>
        <p className="form-section__desc">Changing your password revokes every other session, including this one.</p>
        <Field label="Current password">
          {(a) => <TextInput {...a} type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />}
        </Field>
        <Field label="New password" hint="At least 15 characters." error={pwError}>
          {(a) => <TextInput {...a} type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />}
        </Field>
        <Field label="Confirm new password" error={confirmError}>
          {(a) => <TextInput {...a} type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />}
        </Field>
        {error ? (
          <div className="form-error" role="alert">
            <strong>{error.message}</strong>
          </div>
        ) : null}
        <div className="form-actions">
          <Button type="submit" variant="primary" icon={LockKeyhole} loading={changePassword.isPending} disabled={blocked}>
            Change password
          </Button>
        </div>
      </form>
    </section>
  );
}

/** Account settings and API-key management, both under one address. */
export function AccountPage() {
  return (
    <div className="page-pad narrow-page account">
      <header className="page-head">
        <h1>Account</h1>
        <p className="page-head__sub">Your login, and the keys that automation uses instead of one.</p>
      </header>
      <Account />
      <ApiKeys />
    </div>
  );
}
