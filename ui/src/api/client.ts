import type { ApiError as ApiErrorBody } from "./types";

const TOKEN_KEY = "ragcentre.token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token lives for this page only */
  }
  memoryToken = token;
}

let memoryToken: string | null = null;

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

/** Subscribe to 401 responses so the shell can ask for a token. */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = new URL(`/v1${path}`, window.location.origin);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }

  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken() ?? memoryToken;
  if (token) headers.Authorization = `Bearer ${token}`;

  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(url, { method: options.method ?? "GET", headers, body, signal: options.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "RAGCentre could not be reached. Check that the server is running.");
  }

  if (response.status === 401) unauthorizedListeners.forEach((listener) => listener());

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

function authHeader(): Record<string, string> {
  const token = getToken() ?? memoryToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
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
export function upload<T>(
  path: string,
  form: FormData,
  options: { method?: "POST" | "PUT"; onProgress?: (fraction: number) => void; signal?: AbortSignal } = {},
): Promise<{ status: number; body: T }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(options.method ?? "POST", new URL(`/v1${path}`, window.location.origin).toString());
    for (const [k, v] of Object.entries({ Accept: "application/json", ...authHeader() })) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) options.onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status === 401) unauthorizedListeners.forEach((listener) => listener());
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

/** Download a file the API serves (with the access token, which a plain link cannot send). */
export async function download(path: string, filename: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(new URL(`/v1${path}`, window.location.origin), { headers: authHeader() });
  } catch {
    throw new ApiError(0, "network_error", "RAGCentre could not be reached. Check that the server is running.");
  }
  if (response.status === 401) unauthorizedListeners.forEach((listener) => listener());
  if (!response.ok) throw errorFrom(response.status, response.statusText, await response.text());
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
