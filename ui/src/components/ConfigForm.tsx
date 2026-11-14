import { Lock } from "lucide-react";
import type { ReactNode } from "react";
import { display, type ConfigField, type FormValues } from "../lib/configSchema";
import { Checkbox, Field, Select, TextInput } from "./Field";

/** One input generated from a config-schema field. */
export function ConfigInput({
  field,
  value,
  error,
  onChange,
  disabled,
}: {
  field: ConfigField;
  value: string | boolean | undefined;
  error?: string;
  onChange: (v: string | boolean) => void;
  disabled?: boolean;
}) {
  const label = (
    <>
      {field.label}
      {field.required ? <span className="cfg-req" aria-hidden> *</span> : null}
      <span className="cfg-key mono">{field.key}</span>
    </>
  );
  const hint = [field.description, rangeHint(field), defaultHint(field)].filter(Boolean).join(" · ") || undefined;

  if (field.kind === "boolean") {
    return (
      <div className="rc-field cfg-bool">
        <Checkbox label={label} checked={value === true} onChange={(e) => onChange(e.target.checked)} disabled={disabled} />
        {hint ? <p className="rc-field__hint">{hint}</p> : null}
      </div>
    );
  }
  return (
    <Field label={label} hint={hint} error={error ?? null}>
      {(a11y) =>
        field.kind === "enum" ? (
          <Select {...a11y} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
            {!field.required ? <option value="">Default{field.default !== undefined && field.default !== null ? ` (${String(field.default)})` : ""}</option> : null}
            {field.options.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </Select>
        ) : (
          <TextInput
            {...a11y}
            mono={field.format === "uri" || field.key.endsWith("url") || field.key.endsWith("path")}
            inputMode={field.kind === "integer" ? "numeric" : field.kind === "number" ? "decimal" : undefined}
            value={String(value ?? "")}
            placeholder={placeholder(field)}
            onChange={(e) => onChange(e.target.value)}
            disabled={disabled}
            required={field.required}
          />
        )
      }
    </Field>
  );
}

function rangeHint(f: ConfigField): string | null {
  const lo = f.min ?? (f.exclusiveMin !== null ? `>${f.exclusiveMin}` : null);
  if (lo !== null && f.max !== null) return `${lo}–${f.max}`;
  if (lo !== null) return `min ${lo}`;
  if (f.max !== null) return `max ${f.max}`;
  return null;
}

function defaultHint(f: ConfigField): string | null {
  if (f.default === undefined || f.default === null || f.default === "" || f.kind === "boolean") return null;
  return `default ${String(f.default)}`;
}

function placeholder(f: ConfigField): string | undefined {
  if (f.format === "uri" || f.key.endsWith("url")) return "http://host:port";
  if (f.default !== undefined && f.default !== null && f.default !== "") return String(f.default);
  if (f.nullable) return "Not set";
  return undefined;
}

/** A group of generated inputs. `locked` fields render read-only with the reason. */
export function ConfigFields({
  fields,
  values,
  errors,
  onChange,
  disabled,
}: {
  fields: ConfigField[];
  values: FormValues;
  errors: Record<string, string>;
  onChange: (key: string, v: string | boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="cfg-grid">
      {fields.map((f) => (
        <ConfigInput key={f.key} field={f} value={values[f.key]} error={errors[f.key]} onChange={(v) => onChange(f.key, v)} disabled={disabled} />
      ))}
    </div>
  );
}

/** Read-only key/value list for permanent settings. */
export function LockedSettings({ items, children }: { items: { key: string; label: string; value: unknown }[]; children?: ReactNode }) {
  return (
    <div className="cfg-locked">
      <dl className="cfg-locked__list">
        {items.map((i) => (
          <div key={i.key} className="cfg-locked__row">
            <dt>
              <Lock size={12} strokeWidth={2} aria-hidden />
              {i.label} <span className="cfg-key mono">{i.key}</span>
            </dt>
            <dd className="mono">{display(i.value)}</dd>
          </div>
        ))}
      </dl>
      {children}
    </div>
  );
}
