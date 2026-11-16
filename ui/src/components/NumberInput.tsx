import { useState } from "react";
import { Field, TextInput } from "./Field";

/**
 * A numeric field that keeps what the user typed and reports a number only when it is valid
 * (blank reports `undefined`, meaning "use the default"). Limits are shown and checked.
 */
export function NumberInput({
  label,
  value,
  onChange,
  min,
  max,
  exclusiveMin,
  exclusiveMax,
  integer = true,
  hint,
  placeholder,
  error,
  describedBy,
  invalid,
}: {
  label: React.ReactNode;
  value: number | undefined;
  onChange: (v: number | undefined) => void;
  min?: number;
  max?: number;
  /** Limits the value may not equal (JSON Schema's `exclusiveMinimum` and `exclusiveMaximum`). */
  exclusiveMin?: number;
  exclusiveMax?: number;
  integer?: boolean;
  hint?: string;
  placeholder?: string;
  error?: string | null;
  /** Id of a message elsewhere that also describes this box (a group's shared error), and whether it is the problem. */
  describedBy?: string;
  invalid?: boolean;
}) {
  // While focused, the field shows exactly what was typed; otherwise it shows the value.
  const [draft, setDraft] = useState<string | null>(null);
  const text = draft ?? (value === undefined ? "" : String(value));
  const lower = exclusiveMin !== undefined ? `greater than ${exclusiveMin}` : min !== undefined ? `at least ${min}` : "";
  const upper = exclusiveMax !== undefined ? `less than ${exclusiveMax}` : max !== undefined ? `at most ${max}` : "";
  const closed = exclusiveMin === undefined && exclusiveMax === undefined && min !== undefined && max !== undefined;
  const range = closed ? `${min}–${max}` : [lower, upper].filter(Boolean).join(", ");
  function check(t: string | null): string | null {
    if (t === null || t.trim() === "") return null;
    const n = Number(t);
    if (!Number.isFinite(n) || (integer && !Number.isInteger(n))) return integer ? "Enter a whole number." : "Enter a number.";
    if ((min !== undefined && n < min) || (max !== undefined && n > max) || (exclusiveMin !== undefined && n <= exclusiveMin) || (exclusiveMax !== undefined && n >= exclusiveMax)) return `Enter a value ${range}.`;
    return null;
  }
  return (
    <Field label={label} hint={[hint, range].filter(Boolean).join(" · ") || undefined} error={check(draft) ?? error ?? null}>
      {(a11y) => (
        <TextInput
          {...a11y}
          aria-describedby={[a11y["aria-describedby"], describedBy].filter(Boolean).join(" ") || undefined}
          aria-invalid={a11y["aria-invalid"] || invalid || undefined}
          mono
          inputMode={integer ? "numeric" : "decimal"}
          value={text}
          placeholder={placeholder}
          onFocus={() => setDraft(text)}
          onBlur={() => setDraft(null)}
          onChange={(e) => {
            const t = e.target.value;
            setDraft(t);
            if (t.trim() === "") return onChange(undefined);
            if (check(t) === null) onChange(Number(t));
          }}
        />
      )}
    </Field>
  );
}
