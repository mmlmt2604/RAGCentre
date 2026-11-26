// Mock account, session and API-key operations (the behaviour of the backend's accounts service
// and auth/keys routers, close enough for the UI's login/setup/account/keys screens).
import type { ApiKey, AuthConfig, Me, WhoAmI } from "../api/types";
import { uuidFrom } from "./hash";
import { MockError } from "./providers";
import { now, type ApiKeyRec, type MockState } from "./store";

export const MOCK_CSRF = "mock-csrf";
export const MOCK_USER_ID = "user-1";
export const MIN_PASSWORD_LENGTH = 15;

export type Principal = { kind: "admin_token" | "key" | "session" | "none"; scope: "full" | "read" | "none" };

export function authRequired(state: MockState): boolean {
  return state.account !== null || state.adminToken !== null;
}

export function authConfig(state: MockState): AuthConfig {
  const hasAccount = state.account !== null;
  return {
    auth_required: hasAccount || state.adminToken !== null,
    setup_required: !hasAccount,
    login_available: hasAccount,
    setup_allowed: true, // the mock always simulates a loopback bind
  };
}

export function me(state: MockState): Me | null {
  if (!state.account) return null;
  return { id: MOCK_USER_ID, username: state.account.username, display_name: state.account.display_name, is_platform_admin: true };
}

export function resolvePrincipal(state: MockState, request: Request): Principal {
  const auth = request.headers.get("Authorization");
  if (auth?.startsWith("Bearer ")) {
    const token = auth.slice("Bearer ".length);
    if (state.adminToken !== null && token === state.adminToken) return { kind: "admin_token", scope: "full" };
    const key = state.apiKeys.find((k) => k.token === token);
    if (key) {
      key.last_used_at = now();
      return { kind: "key", scope: key.scope };
    }
    return { kind: "none", scope: "none" };
  }
  return state.session ? { kind: "session", scope: "full" } : { kind: "none", scope: "none" };
}

export function whoami(state: MockState, request: Request): WhoAmI {
  const p = resolvePrincipal(state, request);
  return { scope: p.scope, kind: p.kind };
}

export function apiKeyOut(key: ApiKeyRec): ApiKey {
  return { id: key.id, name: key.name, scope: key.scope, created_at: key.created_at, last_used_at: key.last_used_at };
}

export function requireSession(state: MockState): void {
  if (!state.session) throw new MockError(401, "unauthorized", "This endpoint requires a signed-in session.");
}

export function createAccount(state: MockState, username: string, displayName: string, password: string): void {
  if (state.account) throw new MockError(403, "setup_unavailable", "An account already exists; sign in instead.");
  state.account = { username, display_name: displayName, password };
  state.session = true;
}

export function verifyLogin(state: MockState, username: string, password: string): boolean {
  const a = state.account;
  if (!a || a.username !== username.trim().toLowerCase() || a.password !== password) return false;
  state.session = true;
  return true;
}

export function createKey(state: MockState, name: string, scope: "read" | "full"): { id: string; name: string; scope: "read" | "full"; key: string; created_at: string } {
  const key: ApiKeyRec = {
    id: uuidFrom(`key:${name}:${now()}`),
    name,
    scope,
    token: `mock-key-${uuidFrom(`token:${name}:${now()}`)}`,
    created_at: now(),
    last_used_at: null,
  };
  state.apiKeys.push(key);
  return { id: key.id, name: key.name, scope: key.scope, key: key.token, created_at: key.created_at };
}

export function revokeKey(state: MockState, id: string): void {
  const i = state.apiKeys.findIndex((k) => k.id === id);
  if (i < 0) throw new MockError(404, "not_found", "API key was not found.");
  state.apiKeys.splice(i, 1);
}
