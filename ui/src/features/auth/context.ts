import { createContext, useContext, useState } from "react";
import { captureTransport } from "../../api/client";
import type { Capability, CurrentUser, TeamMembership } from "./types";

export interface AuthState {
  mode: "legacy" | "local";
  current: CurrentUser | null;
  teams: TeamMembership[];
  teamId: string | null;
  generation: number;
  localAvailable: boolean;
  switchTeam: (id: string) => void;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  useLocal: () => void;
}
export const AuthContext = createContext<AuthState>({
  mode: "legacy", current: null, teams: [], teamId: null, generation: 0, localAvailable: false,
  switchTeam: () => {}, refresh: async () => {}, logout: async () => {}, useLocal: () => {},
});
export const useAuth = () => useContext(AuthContext);
export function allowed(auth: Pick<AuthState, "mode" | "current" | "teamId">, capability: Capability): boolean {
  if (auth.mode === "legacy") return capability !== "members";
  if (auth.current?.user.is_platform_admin) return true;
  if (capability === "platform") return false;
  const role = auth.current?.memberships.find((m) => m.team_id === auth.teamId)?.role;
  if (capability === "manage" || capability === "members") return role === "owner";
  if (capability === "edit" || capability === "providers") return role === "owner" || role === "editor";
  return false;
}
export function useCan(capability: Capability): boolean { return allowed(useAuth(), capability); }
export function useTransport() { return useState(captureTransport)[0]; }
