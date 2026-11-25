import { Activity, Database, Monitor, Moon, Plug, RotateCw, Settings2, Sun, WifiOff, Users, UsersRound, KeyRound } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, type ReactNode } from "react";
import { Link, NavLink, Outlet, useMatch, useParams } from "react-router";
import { useCorpus, useHealth } from "../api/hooks";
import { StatusIndicator } from "../components/Status";
import { OcrBarStatus } from "../features/ocr/OcrBarStatus";
import { OcrWatcher } from "../features/ocr/OcrWatcher";
import { WelcomeSetup } from "../features/welcome/WelcomeSetup";
import { count } from "../lib/format";
import { useTheme, type ThemePref } from "../lib/theme";
import { allowed, useAuth, useCan } from "../features/auth/context";
import { Can } from "../features/auth/Access";
import { LogoutButton } from "../features/auth/AuthGate";
import type { Capability } from "../features/auth/types";
import { Select } from "../components/Field";
import { TokenPrompt } from "./TokenPrompt";

const DevMenu = import.meta.env.DEV && import.meta.env.VITE_MOCK !== "0" ? lazy(() => import("./DevMenu")) : null;

const RAIL: { to: string; label: string; icon: typeof Database; end?: boolean; match?: string[]; capability?: Capability; localOnly?: boolean }[] = [
  { to: "/", label: "Corpora", icon: Database, end: true, match: ["/", "/corpora/*"] },
  { to: "/providers", label: "Providers", icon: Plug, capability: "providers" },
  { to: "/activity", label: "Activity", icon: Activity },
  { to: "/members", label: "Members", icon: Users, capability: "members", localOnly: true },
  { to: "/team-tokens", label: "Team tokens", icon: KeyRound, capability: "manage", localOnly: true },
  { to: "/users", label: "Users", icon: UsersRound, capability: "platform", localOnly: true },
  { to: "/teams", label: "Teams", icon: UsersRound, capability: "platform", localOnly: true },
  { to: "/admin", label: "Admin", icon: Settings2, capability: "platform" },
];

function RailLink({ to, label, icon: Icon, end }: (typeof RAIL)[number]) {
  const inCorpus = useMatch("/corpora/*");
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => `shell-rail__link${isActive || (to === "/" && inCorpus) ? " is-on" : ""}`}
      aria-label={label}
      title={label}
    >
      <Icon size={18} strokeWidth={1.75} aria-hidden />
    </NavLink>
  );
}

function ThemeSwitch() {
  const [pref, setPref] = useTheme();
  const order: ThemePref[] = ["dark", "light", "system"];
  const Icon = pref === "dark" ? Moon : pref === "light" ? Sun : Monitor;
  const next = order[(order.indexOf(pref) + 1) % order.length]!;
  return (
    <button type="button" className="rc-iconbtn" onClick={() => setPref(next)} aria-label={`Theme: ${pref}. Switch to ${next}.`} title={`Theme: ${pref}`}>
      <Icon size={16} strokeWidth={1.75} aria-hidden />
    </button>
  );
}

function Crumbs() {
  const { corpusId } = useParams();
  const corpus = useCorpus(corpusId);
  const section = useMatch("/corpora/:corpusId/:section/*")?.params.section;
  const top = useMatch("/:top/*")?.params.top;
  const parts: ReactNode[] = [<Link key="home" to="/">RAGCentre</Link>];
  if (corpusId) {
    parts.push(<Link key="c" to={`/corpora/${corpusId}/pipelines`}>{corpus.data?.name ?? "…"}</Link>);
    if (section) parts.push(<span key="s">{section[0]!.toUpperCase() + section.slice(1)}</span>);
  } else if (top && top !== "corpora") {
    parts.push(<span key="t">{top[0]!.toUpperCase() + top.slice(1)}</span>);
  }
  return (
    <nav className="shell-crumbs" aria-label="Breadcrumb">
      {parts.map((p, i) => (
        <span key={i} className="shell-crumbs__part">
          {i > 0 ? <span className="shell-crumbs__sep" aria-hidden>/</span> : null}
          {p}
        </span>
      ))}
    </nav>
  );
}

function StatusBar() {
  const auth = useAuth();
  const health = useHealth();
  const connected = health.isSuccess;
  return (
    <footer className="shell-status">
      <span className={`shell-status__conn ${connected ? "is-ok" : health.isError ? "is-down" : ""}`}>
        <span className="shell-status__dot" aria-hidden />
        {connected ? "Connected" : health.isError ? "Disconnected" : "Connecting…"}
      </span>
      <Can capability="platform"><OcrBarStatus /></Can>
      {health.data ? (
        <>
          <span className="shell-status__fact">{count(health.data.workers)} workers</span>
          <span className="shell-status__fact">v{health.data.version}</span>
          {health.data.auth_required ? <span className="shell-status__fact">{auth.mode === "local" ? "Session auth" : "Token auth"}</span> : null}
        </>
      ) : null}
      <span className="shell-status__spacer" />
      {DevMenu ? (
        <Suspense fallback={null}>
          <DevMenu />
        </Suspense>
      ) : null}
    </footer>
  );
}

/** When the server stops answering, say so and keep showing the last data received. */
function OfflineBanner() {
  const health = useHealth();
  const qc = useQueryClient();
  if (!health.isError) return null;
  return (
    <div className="shell-offline" role="alert">
      <WifiOff size={14} aria-hidden />
      <span>
        <strong>Can't reach RAGCentre.</strong> Showing the last data received; changes will fail until the server answers.
      </span>
      <button
        type="button"
        className="rc-btn rc-btn--sm"
        onClick={() => {
          void health.refetch();
          void qc.invalidateQueries();
        }}
      >
        <RotateCw size={12} aria-hidden /> Retry
      </button>
    </div>
  );
}

export function Shell() {
  const auth = useAuth();
  return (
    <div className="shell">
      <a href="#main" className="shell-skip">Skip to content</a>
      <nav className="shell-rail" aria-label="Primary">
        <Link to="/" className="shell-rail__mark" aria-label="RAGCentre home">
          <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden>
            <rect x="7" y="8" width="4" height="16" fill="var(--accent)" />
            <rect x="14" y="8" width="11" height="4" fill="var(--ok)" />
            <rect x="14" y="14" width="8" height="4" fill="var(--num)" />
            <rect x="14" y="20" width="11" height="4" fill="var(--warn)" />
          </svg>
        </Link>
        {RAIL.filter((r) => (!r.capability || allowed(auth, r.capability)) && (!r.localOnly || auth.mode === "local")).map((r) => (
          <RailLink key={r.to} {...r} />
        ))}
      </nav>
      <header className="shell-top">
        <Crumbs />
        <span className="shell-top__spacer" />
        {auth.mode === "local" ? <div className="shell-account">
          {auth.teams.length ? <Select aria-label="Selected team" value={auth.teamId ?? ""} onChange={(e) => auth.switchTeam(e.target.value)}>{auth.teams.map((team) => <option key={team.team_id} value={team.team_id}>{team.team_name} · {auth.current?.user.is_platform_admin ? "admin" : team.role}</option>)}</Select> : null}
          <Link to="/account">{auth.current?.user.display_name || auth.current?.user.username}</Link><LogoutButton />
        </div> : auth.localAvailable ? <button type="button" className="rc-btn rc-btn--ghost" onClick={auth.useLocal}>Sign in with an account</button> : null}
        <ThemeSwitch />
      </header>
      <div className="shell-body">
        <OfflineBanner />
        <Outlet />
      </div>
      <StatusBar />
      {auth.mode === "legacy" ? <TokenPrompt /> : null}
      <Can capability="platform"><OcrWatcher /></Can>
      {(auth.mode === "legacy" || auth.teamId) ? <Can capability="manage"><WelcomeSetup /></Can> : null}
    </div>
  );
}

/** Layout for every page inside a corpus: corpus column + content. */
export function CorpusLayout() {
  const manage = useCan("manage");
  const edit = useCan("edit");
  const { corpusId } = useParams();
  const corpus = useCorpus(corpusId);
  const c = corpus.data;
  const active = c?.pipelines.find((p) => p.id === c.active_pipeline_id);
  const degraded = c?.pipelines.filter((p) => p.status === "degraded").length ?? 0;
  const base = `/corpora/${corpusId}`;
  const items = [
    { to: `${base}/documents`, label: "Documents", count: c?.document_count },
    { to: `${base}/sources`, label: "Sources" },
    { to: `${base}/pipelines`, label: "Pipelines", count: c?.pipelines.length, alert: degraded },
    { to: `${base}/query`, label: "Query" },
    { to: `${base}/eval`, label: "Evaluation" },
    { to: `${base}/settings`, label: "Settings" },
  ];
  return (
    <div className="corpus-layout">
      <aside className="corpus-col" aria-label="Corpus">
        <div className="corpus-col__head">
          <h2 className="corpus-col__name">{c?.name ?? <span className="rc-skel" style={{ width: 120, height: 14 }} />}</h2>
          {c ? (
            <dl className="corpus-col__facts">
              <div>
                <dt>Documents</dt>
                <dd className="num">{count(c.document_count)}</dd>
              </div>
              <div>
                <dt>Active</dt>
                <dd>
                  {active ? (
                    <Link to={`${base}/pipelines?selected=${active.id}`} className="corpus-col__active">
                      <span className="mono">{active.name}</span>
                      <StatusIndicator state={active.status} compact />
                    </Link>
                  ) : (
                    <span className="corpus-col__none">None yet</span>
                  )}
                </dd>
              </div>
            </dl>
          ) : null}
        </div>
        <nav className="corpus-col__nav" aria-label="Corpus sections">
          {items.filter((i) => (i.label !== "Sources" || manage) && (i.label !== "Settings" || edit)).map((i) => (
            <NavLink key={i.to} to={i.to} className="corpus-col__link">
              <span>{i.label}</span>
              {i.alert ? (
                <span className="corpus-col__alert num" title={`${i.alert} degraded`}>
                  {i.alert} degraded
                </span>
              ) : i.count !== undefined ? (
                <span className="corpus-col__count num">{count(i.count)}</span>
              ) : null}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main id="main" className="corpus-main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}

/** Layout for top-level pages (no corpus column). */
export function PageLayout() {
  return (
    <main id="main" className="page-main" tabIndex={-1}>
      <Outlet />
    </main>
  );
}
