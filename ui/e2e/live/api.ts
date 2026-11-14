// A few direct API calls for seeding and polling in the live end-to-end tests.
const port = Number(process.env.RAGCENTRE_E2E_PORT ?? 8711);
export const apiBase = `http://127.0.0.1:${port}/v1`;

export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const res = await fetch(`${apiBase}${path}`, init);
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}: ${text}`);
  return (text ? JSON.parse(text) : undefined) as T;
}

export interface Seeded {
  corpusId: string;
  corpusName: string;
  pipelines: { id: string; name: string }[];
}
