import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router";
import { onUnauthorized } from "../../api/client";
import { keys, useAuthConfig, useMe } from "../../api/hooks";

function GateLoading() {
  return (
    <div className="auth-layout">
      <div className="auth-card">
        <p className="auth-loading" role="status">
          <span className="rc-spinner" aria-hidden /> Checking your session…
        </p>
      </div>
    </div>
  );
}

/** No account yet, and setup isn't offered on this bind: the operator must run the CLI. */
export function NoAccount() {
  return (
    <div className="auth-layout">
      <div className="auth-card">
        <h1>Set up your account</h1>
        <p className="auth-card__lead">
          This RAGCentre server has no account yet, and first-run setup is not available from this address. Create one from the server's
          shell:
        </p>
        <pre className="auth-command">
          <code>ragcentre account init</code>
        </pre>
        <p className="auth-card__hint">Then refresh this page to sign in.</p>
      </div>
    </div>
  );
}

/**
 * The authenticated side of the router. In open mode (no account, no token) it renders straight
 * through; otherwise it shows the app only to a signed-in session, sending the visitor to setup
 * (first run) or login. A 401 on a protected resource revokes the gate back to login.
 */
export function RequireAuth() {
  const qc = useQueryClient();
  const config = useAuthConfig();
  const me = useMe();
  const location = useLocation();

  useEffect(() => onUnauthorized(() => void qc.invalidateQueries({ queryKey: keys.me })), [qc]);

  const cfg = config.data;

  // A signed-in session always wins, even over a config that is still refetching after login/setup.
  if (me.isSuccess) return <Outlet />;

  // First-run setup (no account yet): offer it in the browser when this bind allows it. A fresh
  // install has auth_required false, so this must come before the session check.
  if (cfg?.setup_required) {
    return cfg.setup_allowed ? <Navigate to="/setup" replace state={{ from: location }} /> : <NoAccount />;
  }

  // An account or token exists: require a signed-in session, or send the visitor to sign in.
  if (cfg?.auth_required) {
    if (me.isLoading) return <GateLoading />;
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  // Config still loading, or auth not required and nothing to set up: render through. The server
  // guards every route, so nothing is exposed before a redirect resolves.
  return <Outlet />;
}
