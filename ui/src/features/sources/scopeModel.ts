import type { ProviderType } from "../../api/types";
import { parseJsonObject } from "./model";

export interface ScopeSchema {
  type: "object";
  properties: Record<string, { type: string | string[]; title: string; description?: string }>;
  required: string[];
  "x-broad-scope"?: { unless_any: string[]; message: string };
}

export function scopeSchemaFor(types: ProviderType[] | undefined, type: string | undefined): ScopeSchema | null {
  return (types?.find((t) => t.kind === "connector" && t.type === type) as (ProviderType & { scope_schema?: ScopeSchema | null }) | undefined)?.scope_schema ?? null;
}

export function scopeProblem(text: string, schema: ScopeSchema | null): string | undefined {
  const parsed = parseJsonObject(text);
  if (parsed.error) return parsed.error;
  if (!schema) return;
  for (const [key, field] of Object.entries(schema.properties)) {
    const value = parsed.value?.[key];
    if (schema.required.includes(key) && (typeof value !== "string" || !value.trim())) return `${field.title} is required.`;
    if (value != null && typeof value !== "string") return `${field.title} must be a string or null.`;
  }
}

export function broadScopeMessage(text: string, schema: ScopeSchema | null): string | undefined {
  const parsed = parseJsonObject(text);
  if (!parsed.value) return;
  if (!schema) return Object.keys(parsed.value).length === 0 ? "An empty scope may import everything accessible to this connector. Check the connector's scope contract before continuing." : undefined;
  if (scopeProblem(text, schema)) return;
  const broad = schema["x-broad-scope"];
  if (broad && !broad.unless_any.some((key) => typeof parsed.value![key] === "string" && (parsed.value![key] as string).length > 0)) return broad.message;
}

export const SCHEDULES = { manual: 0, hourly: 3600, daily: 86400 } as const;
export type SchedulePreset = keyof typeof SCHEDULES | "custom";
export function schedulePreset(seconds: string): SchedulePreset {
  return (Object.entries(SCHEDULES).find(([, value]) => String(value) === seconds)?.[0] as SchedulePreset | undefined) ?? "custom";
}
export function scheduleProblem(seconds: string): string | undefined {
  if (!seconds.trim() || !Number.isSafeInteger(Number(seconds)) || Number(seconds) < 0) return "Enter a whole number of seconds ≥ 0.";
}
