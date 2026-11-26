import type { ApiError as ApiErrorBody, CsrfToken } from "./types";

const CSRF_HEADER = "X-RAGCentre-CSRF";
const MUTATION_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * The browser authenticates with the session cookie, never a bearer token (sessions for humans,
 * keys for automation). Same-origin fetch/XHR send the cookie by default; state-changing calls
 * also send the session's CSRF secret, fetched once and cached in memory.
 */
let memoryCsrf: string | null = null;
let csrfInFlight: Promise<string | null> | null = null;

export function setCsrf(token: string | null): void {
  memoryCsrf = token;
}

function fetchCsrf(): Promise<string | null> {
  if (!csrfInFlight) {
    csrfInFlight = fetch(new URL("/v1/auth/csrf", window.location.origin).toString(), { headers: { Accept: "application/json" } })
      .then(async (res) => (res.ok ? ((await res.json()) as CsrfToken).token : null))
      .catch(() => null)
      .finally(() => {
        csrfInFlight = null;
      });
  }
  return csrfInFlight;
}

async function csrfHeaders(method: string): Promise<Record<string, string>> {
  if (!MUTATION_METHODS.has(method)) return {};
  if (!memoryCsrf) memoryCsrf = await fetchCsrf();
  return memoryCsrf ? { [CSRF_HEADER]: memoryCsrf } : {};
}

/** An error returned by the API, or a network failure (status 0). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | undefined;

  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  get isNetwork(): boolean {
    return this.status === 0;
  }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

/** Subscribe to 401 responses so the auth gate can send the user to login. */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

/**
 * Auth endpoints legitimately return 401 (a wrong login, "who am I" when signed out). Only an
 * unexpected 401 on a protected resource means "your session expired", which is what the listener
 * is for — so those never notify, and re-checking the session cannot loop.
 */
function shouldNotifyUnauthorized(path: string): boolean {
  return !path.startsWith("/auth/");
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const url = new URL(`/v1${path}`, window.location.origin);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }

  const headers: Record<string, string> = { Accept: "application/json", ...(await csrfHeaders(method)) };

  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(url, { method, headers, body, signal: options.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "RAGCentre could not be reached. Check that the server is running.");
  }

  if (response.status === 401 && shouldNotifyUnauthorized(path)) unauthorizedListeners.forEach((listener) => listener());

  if (!response.ok) {
    const parsed = (await response.json().catch(() => null)) as ApiErrorBody | null;
    const err = parsed?.error;
    throw new ApiError(
      response.status,
      err?.code ?? "http_error",
      err?.message ?? `The server answered ${response.status} ${response.statusText}.`,
      err?.details,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function errorFrom(status: number, statusText: string, text: string): ApiError {
  let parsed: ApiErrorBody | null = null;
  try {
    parsed = JSON.parse(text) as ApiErrorBody;
  } catch {
    /* not JSON */
  }
  const err = parsed?.error;
  return new ApiError(status, err?.code ?? "http_error", err?.message ?? `The server answered ${status} ${statusText}.`, err?.details);
}

/**
 * Multipart upload with progress (fetch cannot report upload progress). Resolves with the parsed
 * body and the status code, so callers can tell "created" (201) from "already there" (200).
 */
export async function upload<T>(
  path: string,
  form: FormData,
  options: { method?: "POST" | "PUT"; onProgress?: (fraction: number) => void; signal?: AbortSignal } = {},
): Promise<{ status: number; body: T }> {
  const csrf = await csrfHeaders(options.method ?? "POST");
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(options.method ?? "POST", new URL(`/v1${path}`, window.location.origin).toString());
    for (const [k, v] of Object.entries({ Accept: "application/json", ...csrf })) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) options.onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status === 401 && shouldNotifyUnauthorized(path)) unauthorizedListeners.forEach((listener) => listener());
      if (xhr.status >= 200 && xhr.status < 300) {
        options.onProgress?.(1);
        resolve({ status: xhr.status, body: (xhr.responseText ? JSON.parse(xhr.responseText) : undefined) as T });
      } else reject(errorFrom(xhr.status, xhr.statusText, xhr.responseText));
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "RAGCentre could not be reached. Check that the server is running."));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    options.signal?.addEventListener("abort", () => xhr.abort());
    xhr.send(form);
  });
}

/** Download a file the API serves (the session cookie authorises it; a plain link cannot). */
export async function download(path: string, filename: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(new URL(`/v1${path}`, window.location.origin).toString());
  } catch {
    throw new ApiError(0, "network_error", "RAGCentre could not be reached. Check that the server is running.");
  }
  if (response.status === 401 && shouldNotifyUnauthorized(path)) unauthorizedListeners.forEach((listener) => listener());
  if (!response.ok) throw errorFrom(response.status, response.statusText, await response.text());
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
