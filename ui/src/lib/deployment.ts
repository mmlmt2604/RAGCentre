/** The server supplies the trusted external path prefix in the built HTML. */
export function browserBasePath(): string {
  const value = document.querySelector<HTMLMetaElement>('meta[name="ragcentre-base-path"]')?.content ?? "";
  return /^\/(?:[a-zA-Z0-9_-]+\/?)+$/.test(value) ? value.replace(/\/$/, "") : "";
}
export function apiUrl(path: string): URL {
  return new URL(`${browserBasePath()}/v1${path}`, window.location.origin);
}
export function publicAccountLink(path: string, token: string, canonicalBase?: string | null): string {
  const base = canonicalBase ?? `${window.location.origin}${browserBasePath()}`;
  return new URL(`${path.replace(/^\//, "")}#token=${encodeURIComponent(token)}`, `${base.replace(/\/$/, "")}/`).toString();
}
