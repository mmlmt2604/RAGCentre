import { Activity, ChevronDown, Database, Monitor, Moon, Plug, RotateCw, Settings2, Sun, UserRound, WifiOff } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useMatch, useNavigate, useParams } from "react-router";
import { useCorpus, useHealth, useLogout, useMe } from "../api/hooks";
import { StatusIndicator } from "../components/Status";
import { OcrBarStatus } from "../features/ocr/OcrBarStatus";
import { OcrWatcher } from "../features/ocr/OcrWatcher";
import { WelcomeSetup } from "../features/welcome/WelcomeSetup";
import { count } from "../lib/format";
import { useTheme, type ThemePref } from "../lib/theme";

const DevMenu = import.meta.env.DEV && import.meta.env.VITE_MOCK !== "0" ? lazy(() => import("./DevMenu")) : null;

const RAIL = [
  { to: "/", label: "Corpora", icon: Database, end: true, match: ["/", "/corpora/*"] },
  { to: "/providers", label: "Providers", icon: Plug },
  { to: "/activity", label: "Activity", icon: Activity },
  { to: "/admin", label: "Admin", icon: Settings2 },
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

/** The signed-in account's affordance in the header: name, account page, sign out. */
function AccountMenu() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  if (!me.data) return null;

  return (
    <div className="shell-account" ref={ref}>
      <button
        type="button"
        className="shell-account__btn"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="menu"
        title={`Signed in as ${me.data.username}`}
      >
        <UserRound size={15} strokeWidth={1.75} aria-hidden />
        <span className="shell-account__name">{me.data.username}</span>
        <ChevronDown size={12} strokeWidth={1.75} aria-hidden />
      </button>
      {open ? (
        <div className="shell-account__menu" role="menu">
          <Link to="/account" role="menuitem" onClick={() => setOpen(false)}>
            Account &amp; API keys
          </Link>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              logout.mutate(undefined, { onSuccess: () => navigate("/login", { replace: true }) });
            }}
          >
            Sign out
          </button>
        </div>
      ) : null}
    </div>
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
  const health = useHealth();
  const connected = health.isSuccess;
  return (
    <footer className="shell-status">
      <span className={`shell-status__conn ${connected ? "is-ok" : health.isError ? "is-down" : ""}`}>
        <span className="shell-status__dot" aria-hidden />
        {connected ? "Connected" : health.isError ? "Disconnected" : "Connecting…"}
      </span>
      <OcrBarStatus />
      {health.data ? (
        <>
          <span className="shell-status__fact">{count(health.data.workers)} workers</span>
          <span className="shell-status__fact">v{health.data.version}</span>
          {health.data.auth_required ? <span className="shell-status__fact">Signed in</span> : null}
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
        {RAIL.map((r) => (
          <RailLink key={r.to} {...r} />
        ))}
      </nav>
      <header className="shell-top">
        <Crumbs />
        <span className="shell-top__spacer" />
        <AccountMenu />
        <ThemeSwitch />
      </header>
      <div className="shell-body">
        <OfflineBanner />
        <Outlet />
      </div>
      <StatusBar />
      <OcrWatcher />
      <WelcomeSetup />
    </div>
  );
}

/** Layout for every page inside a corpus: corpus column + content. */
export function CorpusLayout() {
  const { corpusId } = useParams();
  const corpus = useCorpus(corpusId);
  const c = corpus.data;
  const active = c?.pipelines.find((p) => p.id === c.active_pipeline_id);
  const degraded = c?.pipelines.filter((p) => p.status === "degraded").length ?? 0;
  const base = `/corpora/${corpusId}`;
  const items = [
    { to: `${base}/documents`, label: "Documents", count: c?.document_count },
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
          {items.map((i) => (
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
