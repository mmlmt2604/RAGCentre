import type { Provider } from "../../api/types";

export type ProviderSummary = Pick<Provider, "id" | "name" | "kind" | "type" | "builtin"> & { team_id: string | null };
export type TeamRole = "viewer" | "editor" | "owner";
export interface User {
  id: string;
  username: string;
  display_name: string;
  status: "pending" | "active" | "disabled";
  is_platform_admin: boolean;
  created_at: string;
  updated_at: string;
}
export interface TeamMembership { team_id: string; team_name: string; team_slug: string; role: TeamRole }
export interface CurrentUser { user: User; memberships: TeamMembership[]; csrf_token: string }
export interface AuthConfig { mode: "legacy" | "local"; setup_required: boolean; browser_base_url?: string | null; browser_base_path?: string; supported_flows?: string[] }
export interface Team { id: string; name: string; slug: string; created_at: string }
export interface TeamMember { user_id: string; username: string; display_name: string; status: User["status"]; role: TeamRole }
export interface SetupResult { user: User; setup_token: string }
export type Capability = "edit" | "providers" | "manage" | "platform" | "members";
