import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router";
import { ApiError, configureTransport, onContextInvalid, onUnauthorized, request, captureTransport } from "../../api/client";
import type { Page } from "../../api/types";
import { Button } from "../../components/Button";
import { ErrorState } from "../../components/States";
import { ToastProvider } from "../../components/Toast";
import { AccountPage, AuthFrame, Login } from "./AuthForms";
import { AuthContext, useAuth, type AuthState } from "./context";
import type { AuthConfig, CurrentUser, Team, TeamMembership } from "./types";

const TEAM_KEY = "ragcentre.selected-team";
function readTeam() { try { return sessionStorage.getItem(TEAM_KEY); } catch { return null; } }
function saveTeam(id: string | null) { try { if (id) sessionStorage.setItem(TEAM_KEY, id); else sessionStorage.removeItem(TEAM_KEY); } catch { /* Per-tab convenience only. */ } }
interface Runtime {
  stage: "loading" | "ready" | "login" | "uninitialized" | "error" | "denied";
  config: AuthConfig | null;
  current: CurrentUser | null;
  teams: TeamMembership[];
  teamId: string | null;
  generation: number;
  legacy: boolean;
  error?: unknown;
}
const initial: Runtime = { stage: "loading", config: null, current: null, teams: [], teamId: null, generation: 0, legacy: false };
const signature = (current: CurrentUser | null) => JSON.stringify(current);
const privileges = (current: CurrentUser) => JSON.stringify([current.user.id, current.user.is_platform_admin, current.memberships.map((m) => [m.team_id, m.role])]);

function SessionScope({ children }: { children: ReactNode }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: {
    staleTime: 1000, retry: (n, e) => n < 2 && e instanceof ApiError && (e.isNetwork || e.status >= 500),
  } } }));
  useEffect(() => () => { void client.cancelQueries(); client.clear(); }, [client]);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
async function allTeams(): Promise<TeamMembership[]> {
  const { request } = captureTransport();
  const teams: TeamMembership[] = [];
  let cursor: string | null = null;
  do {
    const page: Page<Team> = await request("/teams", { auth: "session", query: { limit: 500, cursor } });
    teams.push(...page.items.map((t) => ({ team_id: t.id, team_name: t.name, team_slug: t.slug, role: "owner" as const })));
    cursor = page.next_cursor;
  } while (cursor);
  return teams;
}

/** Public bootstrap completes before any protected hook is mounted. */
export function AuthGate() {
  const [runtime, setRuntime] = useState<Runtime>(initial);
  const state = useRef(runtime);
  const sequence = useRef(0);
  const refreshing = useRef(false);
  const channel = useRef<BroadcastChannel | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const legacyClient = useQueryClient();
  const invalidateScope = useCallback(() => { sequence.current++; configureTransport({ mode: "bootstrap" }); }, []);
  const publish = useCallback((next: Runtime) => { state.current = next; setRuntime(next); }, []);
  const clearLegacy = useCallback(() => { void legacyClient.cancelQueries(); legacyClient.clear(); }, [legacyClient]);

  const signedOut = useCallback(() => {
    sequence.current++;
    refreshing.current = false;
    const generation = configureTransport({ mode: "local" });
    clearLegacy();
    publish({ ...state.current, stage: "login", current: null, teams: [], teamId: null, generation, legacy: false });
  }, [clearLegacy, publish]);

  const activate = useCallback(async (current: CurrentUser, config: AuthConfig, force = false, denied = false) => {
    const ticket = sequence.current;
    const teams = current.user.is_platform_admin ? await allTeams() : current.memberships;
    if (ticket !== sequence.current) return;
    const desired = state.current.teamId ?? readTeam();
    const teamId = teams.find((t) => t.team_id === desired)?.team_id ?? teams[0]?.team_id ?? null;
    const changed = force || state.current.stage !== "ready" || signature(current) !== signature(state.current.current) || teamId !== state.current.teamId || JSON.stringify(teams) !== JSON.stringify(state.current.teams);
    const generation = changed ? configureTransport({ mode: "local", teamId: teamId ?? undefined, csrf: current.csrf_token }) : state.current.generation;
    if (changed) clearLegacy();
    const previous = state.current.current;
    if (previous && (previous.user.id !== current.user.id || previous.user.is_platform_admin !== current.user.is_platform_admin || JSON.stringify(previous.memberships) !== JSON.stringify(current.memberships) || state.current.teamId !== teamId)) {
      void navigate("/", { replace: true });
    }
    saveTeam(teamId);
    const stage = denied && previous && privileges(previous) === privileges(current) && state.current.teamId === teamId ? "denied" : "ready";
    publish({ stage, config, current, teams, teamId, generation, legacy: false });
  }, [clearLegacy, navigate, publish]);

  const refresh = useCallback(async (block = true, denied = false) => {
    if (refreshing.current) return;
    refreshing.current = true;
    const ticket = ++sequence.current;
    if (block) {
      const generation = configureTransport({ mode: "bootstrap" });
      publish({ ...state.current, stage: "loading", generation });
    }
    try {
      const current = await request<CurrentUser>("/auth/me", { auth: "session" });
      if (ticket !== sequence.current) return;
      await activate(current, state.current.config ?? { mode: "local", setup_required: false }, block, denied);
    } catch (error) {
      if (ticket !== sequence.current) return;
      if (error instanceof ApiError && error.status === 401) signedOut();
      else publish({ ...state.current, stage: "error", error });
    } finally { if (ticket === sequence.current) refreshing.current = false; }
  }, [activate, publish, signedOut]);

  useEffect(() => {
    const ticket = ++sequence.current;
    configureTransport({ mode: "bootstrap" });
    void (async () => {
      try {
        const config = await request<AuthConfig>("/auth/config", { auth: "public" });
        if (ticket !== sequence.current) return;
        if (config.mode === "legacy") {
          const generation = configureTransport({ mode: "legacy" });
          publish({ ...initial, config, stage: "ready", legacy: true, generation });
        } else if (config.setup_required) {
          configureTransport({ mode: "local" });
          publish({ ...initial, config, stage: "uninitialized" });
        } else {
          publish({ ...initial, config });
          await refresh();
        }
      } catch (error) {
        if (ticket === sequence.current) publish({ ...state.current, stage: "error", error });
      }
    })();
    return invalidateScope;
  }, [invalidateScope, publish, refresh]);

  useEffect(() => {
    const off401 = onUnauthorized(() => { if (!state.current.legacy) signedOut(); });
    const offContext = onContextInvalid(() => { if (!state.current.legacy) void refresh(true, true); });
    const focus = () => { if (!document.hidden && state.current.current && !state.current.legacy) void refresh(); };
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", focus);
    const interval = window.setInterval(() => { if (!document.hidden && state.current.current && !state.current.legacy) void refresh(false); }, 60_000);
    if (typeof BroadcastChannel !== "undefined") {
      const bc = new BroadcastChannel("ragcentre.auth");
      channel.current = bc;
      bc.onmessage = () => { if (state.current.config?.mode === "local" && !state.current.legacy) void refresh(); };
    }
    return () => { off401(); offContext(); window.removeEventListener("focus", focus); document.removeEventListener("visibilitychange", focus); window.clearInterval(interval); channel.current?.close(); channel.current = null; };
  }, [refresh, signedOut]);

  const useLegacy = () => {
    sequence.current++; refreshing.current = false;
    clearLegacy();
    const generation = configureTransport({ mode: "legacy" });
    publish({ ...state.current, stage: "ready", current: null, teams: [], teamId: null, legacy: true, generation });
    void navigate("/", { replace: true });
  };
  const auth: AuthState = {
    mode: runtime.legacy ? "legacy" : "local", current: runtime.current, teams: runtime.teams, teamId: runtime.teamId, generation: runtime.generation, localAvailable: runtime.config?.mode === "local",
    refresh: async () => { await refresh(); channel.current?.postMessage("changed"); },
    useLocal: () => { void refresh(); },
    switchTeam: (id) => {
      if (!runtime.teams.some((t) => t.team_id === id) || id === runtime.teamId) return;
      sequence.current++; refreshing.current = false;
      const generation = configureTransport({ mode: "local", teamId: id, csrf: runtime.current?.csrf_token });
      clearLegacy(); saveTeam(id);
      void navigate("/", { replace: true });
      publish({ ...runtime, teamId: id, generation });
    },
    logout: async () => {
      await request<void>("/auth/logout", { method: "POST" });
      signedOut(); channel.current?.postMessage("changed");
      void navigate("/", { replace: true });
    },
  };

  let content: ReactNode;
  if (runtime.stage === "loading") content = <AuthFrame title="Connecting to RAGCentre"><p role="status">Checking your session…</p></AuthFrame>;
  else if (runtime.stage === "error") content = <AuthFrame title="Unable to connect"><ErrorState error={runtime.error} onRetry={() => window.location.reload()} /></AuthFrame>;
  else if (runtime.stage === "denied") content = <AuthFrame title="Access restricted"><p>This action is unavailable for your role in the selected team. Contact a team owner if you need access.</p><div className="inline-gap"><Button onClick={() => { void navigate("/", { replace: true }); void refresh(); }}>Return to corpora</Button><Link to="/account">Account and password</Link><LogoutButton /></div>{location.pathname === "/account" ? <AccountPage /> : null}</AuthFrame>;
  else if (runtime.stage === "uninitialized") content = <AuthFrame title="Initialize local accounts"><p>A server operator must create the first administrator on the server:</p><pre className="mono">ragcentre user init-admin</pre><p className="muted">Then reload this page to sign in.</p><Button onClick={() => window.location.reload()}>Reload</Button><Button variant="ghost" onClick={useLegacy}>Use a legacy API token</Button></AuthFrame>;
  else if (runtime.stage === "login") content = <Login onLegacy={useLegacy} onLogin={async (current) => {
    await activate(current, runtime.config!, true);
    channel.current?.postMessage("changed");
    void navigate("/", { replace: true });
  }} />;
  else if (!runtime.legacy && !runtime.teamId && !(runtime.current?.user.is_platform_admin && ["/users", "/teams", "/account", "/admin"].includes(location.pathname))) content = <AuthFrame title="Team access pending"><p>You are signed in as <strong>{runtime.current?.user.username}</strong>. {runtime.current?.user.is_platform_admin ? "Create a team with an initial owner to start working." : "Ask a team owner to add your username to a team."}</p><div className="inline-gap">{runtime.current?.user.is_platform_admin ? <><Link to="/users">Manage users</Link><Link to="/teams">Create a team</Link></> : null}<Link to="/account">Account and password</Link><Button onClick={() => void refresh()}>Check access</Button><LogoutButton /></div>{location.pathname === "/account" ? <AccountPage /> : null}</AuthFrame>;
  else content = <Outlet />;

  return <AuthContext.Provider value={auth}><ToastProvider key={runtime.generation}>{runtime.stage === "ready" && !runtime.legacy ? <SessionScope>{content}</SessionScope> : <div className="auth-root">{content}</div>}</ToastProvider></AuthContext.Provider>;
}
export function LogoutButton() {
  const auth = useAuth();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  return <><Button variant="ghost" loading={busy} onClick={() => { setBusy(true); void auth.logout().catch(setError).finally(() => setBusy(false)); }}>Sign out</Button>{error ? <span role="alert">{error instanceof Error ? error.message : "Sign-out failed. Try again."}</span> : null}</>;
}
