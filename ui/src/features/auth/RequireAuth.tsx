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

  // Render through until we know auth is required (open mode stays instant; the server still
  // guards every route, so nothing is exposed before a redirect).
  if (!config.data?.auth_required) return <Outlet />;

  if (me.isLoading) return <GateLoading />;
  if (me.isSuccess) return <Outlet />;

  if (config.data.setup_required) {
    return config.data.setup_allowed ? <Navigate to="/setup" replace state={{ from: location }} /> : <NoAccount />;
  }
  return <Navigate to="/login" replace state={{ from: location }} />;
}
