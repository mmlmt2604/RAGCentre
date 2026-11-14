import { absoluteTime, relativeTime, shortRelativeTime } from "../lib/format";

/** Relative time, with the absolute time on hover and for assistive tech. */
export function Time({ iso, compact = false }: { iso: string; compact?: boolean }) {
  return (
    <time dateTime={iso} title={absoluteTime(iso)}>
      {compact ? shortRelativeTime(iso) : relativeTime(iso)}
    </time>
  );
}
