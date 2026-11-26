import { Link, NavLink, Outlet } from "react-router";
import { useAuth } from "../auth/context";

export function SettingsLayout({ scope }: { scope: "team" | "instance" }) {
  const auth = useAuth();
  const team = auth.teams.find((item) => item.team_id === auth.teamId);
  const base = `/settings/${scope}`;
  const items: [string, string][] = scope === "team" ? [
    [base, "General"], [`${base}/members`, "Members & invitations"], ["/providers", "Services"], [`${base}/tokens`, "API tokens"], [`${base}/setup`, "Setup"],
  ] : [[base, "Overview"], [`${base}/accounts`, "Accounts"], [`${base}/teams`, "Teams"], [`${base}/health`, "Server health"]];
  if (auth.mode !== "local") return <div className="page-pad"><h1>Individual accounts required</h1><p>Sign in with a local account to manage membership settings. Legacy API tokens remain available for existing automation.</p></div>;
  return <div className="settings-layout">
    <aside className="settings-nav"><h2>{scope === "team" ? "Team settings" : "Instance administration"}</h2>{scope === "team" ? <p className="muted">{team?.team_name ?? "Select a team"}</p> : <p className="muted">All accounts and teams</p>}<nav aria-label={scope === "team" ? "Team settings" : "Instance administration"}>
      {items.map(([to, label]) => <NavLink key={to} to={to} end={to === base}>{label}</NavLink>)}
    </nav></aside><div className="settings-content"><Outlet /></div>
  </div>;
}

export function TeamSettingsOverview() {
  const auth = useAuth();
  const team = auth.teams.find((item) => item.team_id === auth.teamId);
  return <div className="page-pad"><header className="page-head"><h1>{team?.team_name ?? "Team"}</h1></header>
    <p className="muted">These settings apply to the team selected above. Every member can read this team's documents.</p>
    <dl className="settings-overview">
      <div><dt><Link to="/settings/team/members">Members & invitations</Link></dt><dd>Invite a teammate, choose their permissions, or remove access.</dd></div>
      <div><dt><Link to="/providers">Services</Link></dt><dd>Connect parsers, embedding models, vector stores, and data connectors.</dd></div>
      <div><dt><Link to="/settings/team/tokens">API tokens</Link></dt><dd>Manage independent credentials for CLI, MCP, and other automation. Removing a member does not revoke these tokens.</dd></div>
      <div><dt><Link to="/settings/team/setup">Setup</Link></dt><dd>Resume configuration and verify your first document search.</dd></div>
    </dl>
  </div>;
}
export function InstanceSettingsOverview() {
  return <div className="page-pad"><header className="page-head"><h1>Instance administration</h1></header><p className="muted">Platform administrators manage accounts and infrastructure across this server. Team owners manage their own team's settings.</p>
    <dl className="settings-overview"><div><dt><Link to="/settings/instance/accounts">Accounts</Link></dt><dd>Create or disable accounts, issue recovery links, and manage platform privileges.</dd></div><div><dt><Link to="/settings/instance/teams">Teams</Link></dt><dd>Create a team with an active owner and switch its working context.</dd></div><div><dt><Link to="/settings/instance/health">Server health</Link></dt><dd>Check workers, credential storage, and maintenance options.</dd></div></dl>
  </div>;
}
