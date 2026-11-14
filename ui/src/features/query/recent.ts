// Recent queries per corpus in localStorage. Every access is guarded: the screen works without storage.
const KEY = (corpusId: string) => `ragcentre.recent-queries.${corpusId}`;
const MAX = 8;

export function recentQueries(corpusId: string): string[] {
  try {
    const raw = localStorage.getItem(KEY(corpusId));
    const v = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string").slice(0, MAX) : [];
  } catch {
    return [];
  }
}

export function rememberQuery(corpusId: string, q: string): string[] {
  const next = [q, ...recentQueries(corpusId).filter((x) => x !== q)].slice(0, MAX);
  try {
    localStorage.setItem(KEY(corpusId), JSON.stringify(next));
  } catch {
    /* storage unavailable or full: keep going without it */
  }
  return next;
}

export function forgetQueries(corpusId: string): void {
  try {
    localStorage.removeItem(KEY(corpusId));
  } catch {
    /* ignore */
  }
}
