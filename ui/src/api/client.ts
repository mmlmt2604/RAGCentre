import type { ApiError as ApiErrorBody } from "./types";

const TOKEN_KEY = "ragcentre.token";
let memoryToken: string | null = null;

export function getToken(): string | null {
  try { return sessionStorage.getItem(TOKEN_KEY) ?? memoryToken; } catch { return memoryToken; }
}
export function setToken(token: string | null): void {
  memoryToken = token;
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* Storage may be unavailable. */ }
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string, readonly details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
  }
  get isNetwork(): boolean { return this.status === 0; }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();
const contextListeners = new Set<Listener>();
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}
export function onContextInvalid(listener: Listener): () => void {
  contextListeners.add(listener);
  return () => contextListeners.delete(listener);
}

type TransportState = { mode: "legacy" | "local" | "bootstrap"; teamId?: string; csrf?: string; generation: number; controller: AbortController };
let transport: TransportState = { mode: "legacy", generation: 0, controller: new AbortController() };
/** Replacing context aborts fetches, downloads and XHRs before any new view is mounted. */
export function configureTransport(context: { mode: TransportState["mode"]; teamId?: string; csrf?: string }): number {
  transport.controller.abort();
  transport = { ...context, generation: transport.generation + 1, controller: new AbortController() };
  return transport.generation;
}
export function transportGeneration(): number { return transport.generation; }
export function hasLocalContext(): boolean { return transport.mode !== "legacy"; }
export function isCurrentGeneration(generation: number): boolean { return transport.generation === generation; }

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
  auth?: "public" | "session";
}
const aborted = () => new DOMException("The account or team context changed.", "AbortError");
function assertCurrent(scope: TransportState) {
  if (scope !== transport || scope.controller.signal.aborted) throw aborted();
}
function teamPath(path: string): boolean {
  return !/^\/(auth(?:\/|$)|health$|users(?:\/|$)|me\/teams$|teams$|admin(?:\/|$)|ocr-services(?:\/|$)|provider-types$)/.test(path);
}
function headersFor(scope: TransportState, path: string, method: string, auth?: RequestOptions["auth"]): Record<string, string> {
  assertCurrent(scope);
  if (auth === "public") return { Accept: "application/json" };
  if (scope.mode === "bootstrap" && auth !== "session") throw aborted();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (scope.mode === "local" || auth === "session") {
    if (scope.teamId && teamPath(path)) headers["X-RAGCentre-Team-ID"] = scope.teamId;
    if (method !== "GET" && scope.csrf) headers["X-RAGCentre-CSRF"] = scope.csrf;
  } else {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}
function notify(scope: TransportState, status: number, auth?: RequestOptions["auth"], code?: string) {
  assertCurrent(scope);
  if (auth === "public") return;
  if (status === 401) unauthorizedListeners.forEach((listener) => listener());
  if (scope.mode === "local" && (status === 403 || code === "team_required")) contextListeners.forEach((listener) => listener());
}
function errorFrom(status: number, statusText: string, text: string): ApiError {
  let parsed: ApiErrorBody | null = null;
  try { parsed = JSON.parse(text) as ApiErrorBody; } catch { /* A proxy may return non-JSON. */ }
  return new ApiError(status, parsed?.error?.code ?? "http_error", parsed?.error?.message ?? `The server answered ${status} ${statusText}.`, parsed?.error?.details);
}
const networkError = () => new ApiError(0, "network_error", "RAGCentre could not be reached. Check that the server is running.");

async function scopedRequest<T>(scope: TransportState, path: string, options: RequestOptions = {}): Promise<T> {
  const url = new URL(`/v1${path}`, window.location.origin);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  const method = options.method ?? "GET";
  const headers = headersFor(scope, path, method, options.auth);
  let body: BodyInit | undefined;
  if (options.body instanceof FormData) body = options.body;
  else if (options.body !== undefined || (method !== "GET" && path.startsWith("/auth/"))) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body ?? {});
  }
  const signal = options.signal ? AbortSignal.any([scope.controller.signal, options.signal]) : scope.controller.signal;
  let response: Response;
  try { response = await fetch(url, { method, headers, body, signal, credentials: "same-origin", cache: "no-store" }); }
  catch (error) {
    if (signal.aborted) throw aborted();
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw networkError();
  }
  assertCurrent(scope);
  const text = response.status === 204 ? "" : await response.text();
  assertCurrent(scope);
  if (!response.ok) {
    const error = errorFrom(response.status, response.statusText, text);
    notify(scope, response.status, options.auth, error.code);
    throw error;
  }
  return (text ? JSON.parse(text) : undefined) as T;
}
export function request<T>(path: string, options: RequestOptions = {}): Promise<T> { return scopedRequest<T>(transport, path, options); }

type UploadOptions = { method?: "POST" | "PUT"; onProgress?: (fraction: number) => void; signal?: AbortSignal };
function scopedUpload<T>(scope: TransportState, path: string, form: FormData, options: UploadOptions = {}): Promise<{ status: number; body: T }> {
  return new Promise((resolve, reject) => {
    const headers = headersFor(scope, path, options.method ?? "POST");
    if (options.signal?.aborted) return reject(aborted());
    const xhr = new XMLHttpRequest();
    const abort = () => { xhr.abort(); reject(aborted()); };
    const cleanup = () => {
      scope.controller.signal.removeEventListener("abort", abort);
      options.signal?.removeEventListener("abort", abort);
    };
    xhr.open(options.method ?? "POST", new URL(`/v1${path}`, window.location.origin).toString());
    xhr.withCredentials = true;
    for (const [key, value] of Object.entries(headers)) xhr.setRequestHeader(key, value);
    xhr.upload.onprogress = (e) => {
      if (scope === transport && !scope.controller.signal.aborted && e.lengthComputable && e.total > 0) options.onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      cleanup();
      try {
        assertCurrent(scope);
        if (xhr.status >= 200 && xhr.status < 300) {
          options.onProgress?.(1);
          resolve({ status: xhr.status, body: (xhr.responseText ? JSON.parse(xhr.responseText) : undefined) as T });
        } else {
          const error = errorFrom(xhr.status, xhr.statusText, xhr.responseText);
          notify(scope, xhr.status, undefined, error.code);
          reject(error);
        }
      } catch (error) { reject(error); }
    };
    xhr.onerror = () => { cleanup(); reject(networkError()); };
    xhr.onabort = () => { cleanup(); reject(aborted()); };
    scope.controller.signal.addEventListener("abort", abort, { once: true });
    options.signal?.addEventListener("abort", abort, { once: true });
    xhr.send(form);
  });
}
export function upload<T>(path: string, form: FormData, options: UploadOptions = {}) { return scopedUpload<T>(transport, path, form, options); }
async function scopedDownload(scope: TransportState, path: string, filename: string): Promise<void> {
  const headers = headersFor(scope, path, "GET");
  let response: Response;
  try { response = await fetch(new URL(`/v1${path}`, window.location.origin), { headers, credentials: "same-origin", signal: scope.controller.signal, cache: "no-store" }); }
  catch { if (scope.controller.signal.aborted) throw aborted(); throw networkError(); }
  assertCurrent(scope);
  if (!response.ok) {
    const error = errorFrom(response.status, response.statusText, await response.text());
    notify(scope, response.status, undefined, error.code);
    throw error;
  }
  const blob = await response.blob();
  assertCurrent(scope);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function download(path: string, filename: string) { return scopedDownload(transport, path, filename); }
/** Capture once per mounted view; chained work cannot send a later step into another team. */
export function captureTransport() {
  const scope = transport;
  return {
    request: <T>(path: string, options?: RequestOptions) => scopedRequest<T>(scope, path, options),
    upload: <T>(path: string, form: FormData, options?: UploadOptions) => scopedUpload<T>(scope, path, form, options),
    download: (path: string, filename: string) => scopedDownload(scope, path, filename),
    isCurrent: () => scope === transport && !scope.controller.signal.aborted,
  };
}
