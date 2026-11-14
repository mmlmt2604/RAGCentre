import { Check, ChevronDown, Search } from "lucide-react";
import { forwardRef, useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";

interface FieldProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
}

/** Label, control, hint and error laid out consistently, with ids wired for assistive tech. */
export function Field({ label, hint, error, children }: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const describedBy = error || hint ? hintId : undefined;
  return (
    <div className="rc-field">
      {label ? (
        <label className="rc-field__label" htmlFor={id}>
          {label}
        </label>
      ) : null}
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {error ? (
        <p className="rc-field__error" id={hintId}>
          {error}
        </p>
      ) : hint ? (
        <p className="rc-field__hint" id={hintId}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { mono?: boolean }>(function TextInput(
  { className, mono, ...rest },
  ref,
) {
  return <input ref={ref} className={["rc-input", mono ? "mono" : "", className ?? ""].join(" ")} {...rest} />;
});

export const SearchInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function SearchInput({ className, ...rest }, ref) {
  return (
    <span className={`rc-search ${className ?? ""}`}>
      <Search size={14} strokeWidth={1.75} aria-hidden />
      <input ref={ref} type="search" className="rc-input" {...rest} />
    </span>
  );
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select({ className, children, ...rest }, ref) {
  return (
    <span className={`rc-select ${className ?? ""}`}>
      <select ref={ref} {...rest}>
        {children}
      </select>
      <ChevronDown size={14} strokeWidth={1.75} aria-hidden />
    </span>
  );
});

export function Checkbox({ label, className, ...rest }: InputHTMLAttributes<HTMLInputElement> & { label?: ReactNode }) {
  return (
    <label className={`rc-check ${className ?? ""}`}>
      <input type="checkbox" {...rest} />
      <span className="rc-check__box" aria-hidden>
        <Check size={12} strokeWidth={2.5} />
      </span>
      {label ? <span className="rc-check__label">{label}</span> : null}
    </label>
  );
}

/** A compact group of mutually exclusive options (radio semantics). */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: ReactNode; count?: number; disabled?: boolean; title?: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  const name = useId();
  return (
    <div className="rc-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <label key={o.value} className="rc-seg__opt" data-on={o.value === value || undefined} data-disabled={o.disabled || undefined} title={o.title}>
          <input type="radio" name={name} value={o.value} checked={o.value === value} disabled={o.disabled} onChange={() => onChange(o.value)} />
          {o.label}
          {o.count !== undefined ? <span className="rc-seg__count num">{o.count}</span> : null}
        </label>
      ))}
    </div>
  );
}
