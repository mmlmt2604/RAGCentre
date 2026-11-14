const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const nf = new Intl.NumberFormat("en");

export function relativeTime(iso: string, from = Date.now()): string {
  const seconds = Math.round((new Date(iso).getTime() - from) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  if (abs < 60 * 45) return rtf.format(Math.round(seconds / 60), "minute");
  if (abs < 3600 * 22) return rtf.format(Math.round(seconds / 3600), "hour");
  if (abs < 86400 * 26) return rtf.format(Math.round(seconds / 86400), "day");
  if (abs < 86400 * 320) return rtf.format(Math.round(seconds / (86400 * 30)), "month");
  return rtf.format(Math.round(seconds / (86400 * 365)), "year");
}

export function shortRelativeTime(iso: string, from = Date.now()): string {
  const s = Math.max(0, Math.round((from - new Date(iso).getTime()) / 1000));
  if (s < 45) return "now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 45) return `${Math.round(s / 86400)}d ago`;
  if (s < 86400 * 365) return `${Math.round(s / (86400 * 30))}mo ago`;
  return `${Math.round(s / (86400 * 365))}y ago`;
}

export function absoluteTime(iso: string): string {
  return new Date(iso).toLocaleString("en", { dateStyle: "medium", timeStyle: "medium" });
}

export function count(n: number): string {
  return nf.format(n);
}

export function percent(fraction: number): string {
  if (fraction > 0.995 && fraction < 1) return "99%";
  return `${Math.round(fraction * 100)}%`;
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function duration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${count(n)} ${n === 1 ? one : many}`;
}
