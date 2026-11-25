import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { captureTransport, configureTransport, download, request, setToken, upload } from "./client";
import { recentQueries, rememberQuery } from "../features/query/recent";

beforeEach(() => { configureTransport({ mode: "legacy" }); setToken(null); sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { configureTransport({ mode: "legacy" }); setToken(null); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const ok = () => new Response(JSON.stringify({ ok: true }), { headers: { "Content-Type": "application/json" } });

describe("account-aware transport", () => {
  it("preserves bearer authentication in legacy mode", async () => {
    const fetcher = vi.fn().mockResolvedValue(ok()); vi.stubGlobal("fetch", fetcher);
    setToken("legacy-secret");
    await request("/corpora");
    expect(fetcher.mock.calls[0]![1]).toMatchObject({ headers: { Authorization: "Bearer legacy-secret" }, credentials: "same-origin" });
  });
  it("uses cookies, CSRF and a selected team without an old bearer credential", async () => {
    const fetcher = vi.fn().mockImplementation(async () => ok()); vi.stubGlobal("fetch", fetcher);
    setToken("stale-bearer"); configureTransport({ mode: "local", teamId: "team-a", csrf: "csrf-a" });
    await request("/corpora/c/query", { method: "POST", body: { query: "hello" } });
    await request("/auth/me", { auth: "session" });
    await request("/auth/login", { method: "POST", body: { username: "viewer", password: "password" }, auth: "public" });
    const query = fetcher.mock.calls[0]![1] as RequestInit;
    expect(query).toMatchObject({ credentials: "same-origin", cache: "no-store", headers: { "X-RAGCentre-Team-ID": "team-a", "X-RAGCentre-CSRF": "csrf-a", "Content-Type": "application/json" } });
    expect(query.headers).not.toHaveProperty("Authorization");
    expect(fetcher.mock.calls[1]![1].headers).not.toHaveProperty("X-RAGCentre-Team-ID");
    expect(fetcher.mock.calls[2]![1].headers).not.toHaveProperty("Authorization");
    expect(fetcher.mock.calls[2]![1].headers).not.toHaveProperty("X-RAGCentre-CSRF");
  });
  it("rejects a late response and prevents subsequent steps from adopting a new team", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    configureTransport({ mode: "local", teamId: "team-a", csrf: "csrf-a" });
    const old = captureTransport();
    const pending = old.request("/corpora");
    const result = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    configureTransport({ mode: "local", teamId: "team-b", csrf: "csrf-a" });
    finish(ok()); await result;
    await expect(old.request("/providers", { method: "POST", body: {} })).rejects.toMatchObject({ name: "AbortError" });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect((fetcher.mock.calls[0]![1].signal as AbortSignal).aborted).toBe(true);
  });
  it("aborts XHR and suppresses progress from its previous context", async () => {
    class FakeXhr {
      static instances: FakeXhr[] = [];
      headers: Record<string, string> = {}; withCredentials = false; status = 201; responseText = "{}"; statusText = "Created";
      upload: { onprogress?: (event: ProgressEvent) => void } = {};
      onabort?: () => void; onload?: () => void;
      constructor() { FakeXhr.instances.push(this); }
      open() {} setRequestHeader(key: string, value: string) { this.headers[key] = value; }
      send() {} abort() { this.onabort?.(); }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    configureTransport({ mode: "local", teamId: "a", csrf: "csrf" });
    const progress = vi.fn(); const pending = upload("/corpora/c/documents", new FormData(), { onProgress: progress });
    const xhr = FakeXhr.instances[0]!;
    const result = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(xhr.headers).toMatchObject({ "X-RAGCentre-Team-ID": "a", "X-RAGCentre-CSRF": "csrf" });
    expect(xhr.withCredentials).toBe(true);
    configureTransport({ mode: "local", teamId: "b", csrf: "csrf" });
    xhr.upload.onprogress?.(new ProgressEvent("progress", { lengthComputable: true, loaded: 1, total: 1 }));
    xhr.onload?.(); await result; expect(progress).not.toHaveBeenCalled();
  });
  it("does not create a download URL for a late blob", async () => {
    let finish!: (blob: Blob) => void;
    const makeUrl = vi.fn(); vi.stubGlobal("URL", class extends URL { static createObjectURL = makeUrl; });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, blob: () => new Promise<Blob>((resolve) => { finish = resolve; }) }));
    configureTransport({ mode: "local", teamId: "a" });
    const pending = download("/documents/d/content", "doc.txt");
    const result = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await Promise.resolve();
    configureTransport({ mode: "local", teamId: "b" });
    finish(new Blob(["private document"])); await result;
    expect(makeUrl).not.toHaveBeenCalled();
  });
  it("keeps local query history out of storage and clears it on a context change", () => {
    configureTransport({ mode: "local", teamId: "a" }); rememberQuery("corpus", "private query");
    expect(recentQueries("corpus")).toEqual(["private query"]);
    expect(localStorage.getItem("ragcentre.recent-queries.corpus")).toBeNull();
    configureTransport({ mode: "local", teamId: "b" }); expect(recentQueries("corpus")).toEqual([]);
  });
});
