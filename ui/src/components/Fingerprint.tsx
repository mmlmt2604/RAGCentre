import { Check, Copy } from "lucide-react";
import { forwardRef, useState } from "react";
import { fpSlot } from "../lib/fingerprint";
import { Tooltip } from "./Tooltip";

const Token = forwardRef<HTMLButtonElement, { value: string; length: number; onCopy: () => void; copied: boolean; label?: string; unique?: boolean }>(function Token(
  { value, length, onCopy, copied, label, unique, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      className="rc-fp"
      data-slot={fpSlot(value)}
      data-unique={unique || undefined}
      onClick={onCopy}
      aria-label={`${label ?? "Fingerprint"} ${value.slice(0, length)}. Copy full value.`}
      {...rest}
    >
      <span className="rc-fp__text">{value.slice(0, length)}</span>
      {copied ? <Check size={11} strokeWidth={2.5} aria-hidden /> : null}
    </button>
  );
});

/**
 * The signature move: a fingerprint rendered like a highlighted identifier.
 * Colour and text both derive from the hash, so shared steps are recognisable
 * across rows and screens. Click copies the full value; the tooltip shows it.
 */
export function Fingerprint({
  value,
  length = 8,
  label,
  unique,
  note,
}: {
  value: string;
  length?: number;
  label?: string;
  /** Used by no other pipeline: drawn dashed. */
  unique?: boolean;
  /** Extra line in the tooltip, e.g. which pipelines share it. */
  note?: React.ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard unavailable; the tooltip still shows the full value */
    }
  };
  return (
    <Tooltip
      content={
        <span className="rc-fp-tip">
          {label ? <span className="rc-fp-tip__label">{label}</span> : null}
          <span className="mono">{value}</span>
          {note ? <span className="rc-fp-tip__note">{note}</span> : null}
          <span className="rc-fp-tip__hint">
            <Copy size={11} aria-hidden /> {copied ? "Copied" : "Click to copy"}
          </span>
        </span>
      }
    >
      <Token value={value} length={length} onCopy={copy} copied={copied} label={label} unique={unique} />
    </Tooltip>
  );
}
