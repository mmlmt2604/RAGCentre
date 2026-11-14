import type { MetadataSchema } from "../../api/types";
import { Field, Select, TextInput } from "../../components/Field";

const TYPE_HINT = { str: "text", int: "integer", float: "number", bool: "true / false" } as const;

/**
 * Inputs for a corpus's metadata fields, typed by its schema. A blank field is left unset and the
 * schema default applies, which the placeholder shows.
 */
export function MetadataFields({
  schema,
  draft,
  onChange,
  errors,
  disabled,
}: {
  schema: MetadataSchema;
  draft: Record<string, string>;
  onChange: (draft: Record<string, string>) => void;
  errors: Record<string, string>;
  disabled?: boolean;
}) {
  const entries = Object.entries(schema.fields);
  if (!entries.length) return <p className="muted">This corpus has no metadata fields. Add them in the corpus settings.</p>;
  return (
    <div className="cfg-grid">
      {entries.map(([key, spec]) => (
        <Field
          key={key}
          label={
            <>
              <span className="mono">{key}</span> <span className="cfg-key">{TYPE_HINT[spec.type]}</span>
            </>
          }
          hint={`Blank uses the default, ${JSON.stringify(spec.default)}.`}
          error={errors[key] ?? null}
        >
          {(a11y) =>
            spec.type === "bool" ? (
              <Select {...a11y} value={draft[key] ?? ""} onChange={(e) => onChange({ ...draft, [key]: e.target.value })} disabled={disabled}>
                <option value="">Default ({String(spec.default)})</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </Select>
            ) : (
              <TextInput
                {...a11y}
                mono={spec.type !== "str"}
                inputMode={spec.type === "int" ? "numeric" : spec.type === "float" ? "decimal" : undefined}
                placeholder={String(spec.default) || "empty"}
                value={draft[key] ?? ""}
                onChange={(e) => onChange({ ...draft, [key]: e.target.value })}
                disabled={disabled}
              />
            )
          }
        </Field>
      ))}
    </div>
  );
}
