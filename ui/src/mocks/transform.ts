// Mock query transforms (see src/ragcentre/retrieval/transform.py). The "language model" is a
// deterministic rule set so the probes UI and its tests work offline: a small synonym table, a
// word-order shuffle, and a split on " and " for decomposition.
import type { ProbeKind, QueryProbe, QueryTransformConfig, TransformMode } from "../api/types";

export const TRANSFORM_IMPL: Record<TransformMode, number> = { rewrite: 1, multi_query: 1, hyde: 1, decompose: 1 };

// Words people use for what the mock corpus (see SENTENCES in fixtures.ts) calls something else.
const SYNONYMS: Record<string, string> = {
  reimbursement: "refund",
  reimbursements: "refunds",
  throttling: "rate limit",
  credentials: "token",
  password: "token",
  logs: "audit log",
  outage: "retry",
  spreadsheet: "csv",
  dashboard: "usage dashboard",
  offline: "cache",
};

export interface MockProbe extends QueryProbe {
  vector: boolean; // vector sources search it
  keyword: boolean; // keyword sources search it
}

const KIND: Record<TransformMode, ProbeKind> = { rewrite: "rewrite", multi_query: "paraphrase", hyde: "hypothetical", decompose: "sub_question" };

function generate(mode: TransformMode, text: string, n: number): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (mode === "hyde") {
    const passage = `This section explains ${text.trim().replace(/\?+$/, "")}. The details are described below, including limits, defaults and how long it takes.`;
    return Array.from({ length: n }, (_, i) => (i === 0 ? passage : `${passage} (variant ${i + 1})`));
  }
  if (mode === "decompose") return text.split(/\s+and\s+|[?;]\s*/i).map((s) => s.trim()).filter(Boolean).slice(0, n);
  const swapped = words.map((w) => SYNONYMS[w.toLowerCase()] ?? w).join(" ");
  const variants = [swapped !== words.join(" ") ? swapped : `information about ${text.trim()}`, [...words].reverse().join(" "), `information about ${text.trim()}`];
  for (let i = variants.length; i < n; i++) variants.push(`details on ${text.trim()} (${i})`);
  return variants.slice(0, mode === "rewrite" ? 1 : n);
}

/** The probes for a query, in the order and with the rules of the real transform. */
export function mockProbes(cfg: QueryTransformConfig, text: string, hasKeywordSource: boolean): MockProbe[] {
  const n = cfg.mode === "rewrite" ? 1 : Math.min(8, Math.max(1, cfg.n ?? 3));
  const seen = new Set([text.trim().toLowerCase()]);
  const generated = generate(cfg.mode, text, n + 2)
    .map((t) => t.trim().slice(0, 1000))
    .filter((t) => {
      const key = t.toLowerCase();
      if (!t || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, n);
  const original: MockProbe = { index: 0, text, kind: "original", vector: true, keyword: true };
  if (!generated.length) return [original];
  const probes: MockProbe[] = [];
  if (cfg.include_original ?? true) probes.push(original);
  else if (cfg.mode === "hyde" && hasKeywordSource) probes.push({ ...original, vector: false });
  generated.forEach((t, i) => probes.push({ index: i + 1, text: t, kind: KIND[cfg.mode], vector: true, keyword: cfg.mode !== "hyde" }));
  return probes;
}
