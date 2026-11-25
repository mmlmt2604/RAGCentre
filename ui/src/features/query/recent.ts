import { hasLocalContext, transportGeneration } from "../../api/client";

const KEY = (corpusId: string) => `ragcentre.recent-queries.${corpusId}`;
const MAX = 8;
let generation = -1;
const sessionQueries = new Map<string, string[]>();
function currentQueries() {
  if (generation !== transportGeneration()) { sessionQueries.clear(); generation = transportGeneration(); }
  return sessionQueries;
}
/** Session query text stays in this context's memory. Legacy mode retains its existing history. */
export function recentQueries(corpusId: string): string[] {
  if (hasLocalContext()) return currentQueries().get(corpusId) ?? [];
  try {
    const raw = localStorage.getItem(KEY(corpusId));
    const value = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(value) ? value.filter((x): x is string => typeof x === "string").slice(0, MAX) : [];
  } catch { return []; }
}
export function rememberQuery(corpusId: string, q: string): string[] {
  const next = [q, ...recentQueries(corpusId).filter((x) => x !== q)].slice(0, MAX);
  if (hasLocalContext()) currentQueries().set(corpusId, next);
  else { try { localStorage.setItem(KEY(corpusId), JSON.stringify(next)); } catch { /* Optional legacy history. */ } }
  return next;
}
export function forgetQueries(corpusId: string): void {
  currentQueries().delete(corpusId);
  try { localStorage.removeItem(KEY(corpusId)); } catch { /* Storage may be unavailable. */ }
}
