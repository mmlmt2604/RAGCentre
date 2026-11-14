import type { EvalComparison, EvalRunDetail } from "../../api/types";

export const fmt = (x: number) => x.toFixed(3);
export const signed = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(3)}`;

/** "1, 3, 5, 10" -> [1, 3, 5, 10], or an error. */
export function parseK(text: string): { ks?: number[]; error?: string } {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  if (!parts.length) return { ks: undefined };
  const ks = parts.map(Number);
  if (ks.some((k) => !Number.isInteger(k) || k < 1 || k > 1000)) return { error: "Use whole numbers from 1 to 1000, separated by commas." };
  return { ks: [...new Set(ks)].sort((a, b) => a - b) };
}

/** Metric names grouped for display: hit_rate@k, recall@k, … then mrr. */
export function metricOrder(names: string[]): string[] {
  const kinds = ["hit_rate", "recall", "precision", "ndcg"];
  const at = (m: string) => Number(m.split("@")[1] ?? Infinity);
  return [...names].sort((a, b) => {
    const ka = kinds.indexOf(a.split("@")[0]!);
    const kb = kinds.indexOf(b.split("@")[0]!);
    return (ka < 0 ? 99 : ka) - (kb < 0 ? 99 : kb) || at(a) - at(b);
  });
}

export function comparisonFor(run: EvalRunDetail, a: string, b: string, metric: string): EvalComparison | null {
  const c = run.comparisons.find((x) => x.metric === metric && ((x.a === a && x.b === b) || (x.a === b && x.b === a)));
  if (!c) return null;
  if (c.a === a) return c;
  // Stored the other way round: flip so diff is always b − a.
  return { ...c, a, b, mean_a: c.mean_b, mean_b: c.mean_a, diff: -c.diff, ci_low: -c.ci_high, ci_high: -c.ci_low };
}

function list(xs: string[]): string {
  return xs.length < 3 ? xs.join(" and ") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`;
}

/**
 * A plain-language reading of b against a. It names significant differences as such and calls the
 * rest what they are: not distinguishable from chance with this many questions.
 */
export function summary(run: EvalRunDetail, a: string, b: string, items: number): string {
  const name = (id: string) => run.summaries.find((s) => s.pipeline_id === id)?.pipeline_name ?? "the other pipeline";
  const metrics = metricOrder(Object.keys(run.summaries[0]?.metrics ?? {}));
  const cs = metrics.map((m) => comparisonFor(run, a, b, m)).filter((c): c is EvalComparison => Boolean(c));
  const better = cs.filter((c) => c.significant && c.diff > 0);
  const worse = cs.filter((c) => c.significant && c.diff < 0);
  const parts: string[] = [];
  const best = (xs: EvalComparison[]) => xs.reduce((m, c) => (Math.abs(c.diff) > Math.abs(m.diff) ? c : m));
  if (better.length) {
    const top = best(better);
    parts.push(`${name(b)} is significantly better than ${name(a)} on ${better.length} of ${cs.length} metrics; the largest gain is ${top.metric} (${signed(top.diff)}, 95% CI ${signed(top.ci_low)} to ${signed(top.ci_high)}).`);
  }
  if (worse.length) {
    const top = best(worse);
    parts.push(`It is significantly worse on ${worse.length} (largest: ${top.metric}, ${signed(top.diff)}).`);
  }
  const rest = cs.filter((c) => !c.significant);
  if (!better.length && !worse.length) parts.push(`No metric differs significantly between ${name(a)} and ${name(b)}: with ${items} questions, the differences seen could be chance.`);
  else if (rest.length) parts.push(`The differences in ${list(rest.slice(0, 4).map((c) => c.metric))}${rest.length > 4 ? ` and ${rest.length - 4} more` : ""} are not significant with ${items} questions and could be chance.`);
  return parts.join(" ");
}
