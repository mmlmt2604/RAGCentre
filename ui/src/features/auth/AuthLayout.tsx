import { Outlet } from "react-router";

/** Full-page, no-shell layout for sign-in and first-run setup. */
export function AuthLayout() {
  return (
    <div className="auth-layout">
      <div className="auth-card">
        <div className="auth-mark">
          <svg viewBox="0 0 32 32" width="28" height="28" aria-hidden>
            <rect x="7" y="8" width="4" height="16" fill="var(--accent)" />
            <rect x="14" y="8" width="11" height="4" fill="var(--ok)" />
            <rect x="14" y="14" width="8" height="4" fill="var(--num)" />
            <rect x="14" y="20" width="11" height="4" fill="var(--warn)" />
          </svg>
          <span className="auth-mark__name">RAGCentre</span>
        </div>
        <Outlet />
      </div>
    </div>
  );
}
