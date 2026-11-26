import type { Provider, Source, SourceStatus } from "../../api/types";

export const STATUS_META: Record<SourceStatus, { label: string; tone: "muted" | "accent" | "danger" }> = {
  idle: { label: "Idle", tone: "muted" },
  syncing: { label: "Syncing", tone: "accent" },
  error: { label: "Error", tone: "danger" },
};

/** A compact, human-readable summary of the connector scope. */
export function scopeText(scope: Record<string, unknown>): string {
  const parts = Object.entries(scope).map(([k, v]) => {
    const text = typeof v === "string" ? v : JSON.stringify(v);
    return `${k}=${text.length > 28 ? `${text.slice(0, 27)}…` : text}`;
  });
  return parts.length ? parts.join(", ") : "All";
}

/** Connector providers (the only kind a source can use). */
export function connectorProviders(providers: Provider[]): Provider[] {
  return providers.filter((p) => p.kind === "connector").sort((a, b) => a.name.localeCompare(b.name));
}

/** Parse a JSON scope/template draft; returns the object or an error message. */
export function parseJsonObject(text: string): { value?: Record<string, unknown>; error?: string } {
  const trimmed = text.trim();
  if (!trimmed) return { value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (e) {
    return { error: e instanceof Error ? `Not valid JSON: ${e.message}` : "Not valid JSON." };
  }
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") return { error: "Must be a JSON object, like {\"key\": \"value\"}." };
  return { value: parsed as Record<string, unknown> };
}

/** A human-readable schedule: "every hour" / "manual only". */
export function scheduleLabel(seconds: number): string {
  if (seconds <= 0) return "Manual only";
  if (seconds % 86_400 === 0) return `Every ${seconds / 86_400} d`;
  if (seconds % 3600 === 0) return `Every ${seconds / 3600} h`;
  if (seconds % 60 === 0) return `Every ${seconds / 60} min`;
  return `Every ${seconds}s`;
}

/** The provider behind a source, or undefined while it loads. */
export function providerOf(providers: Provider[] | undefined, source: Source): Provider | undefined {
  return providers?.find((p) => p.id === source.provider_id);
}
