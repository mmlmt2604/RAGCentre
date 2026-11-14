import { Lock, Plus, Trash2 } from "lucide-react";
import type { FieldType, MetadataSchema } from "../../api/types";
import { Button, IconButton } from "../../components/Button";
import { Select, TextInput } from "../../components/Field";
import { emptyDraft, TYPE_LABEL, type DraftField } from "./schemaDraft";

/**
 * Existing fields are read-only (the schema is additive); new rows add fields, each with the
 * required default that fills records written before the field existed.
 */
export function SchemaEditor({
  existing,
  drafts,
  onChange,
  errors,
  disabled,
}: {
  existing: MetadataSchema;
  drafts: DraftField[];
  onChange: (drafts: DraftField[]) => void;
  errors: Record<number, string>;
  disabled?: boolean;
}) {
  const set = (i: number, patch: Partial<DraftField>) => onChange(drafts.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  const existingEntries = Object.entries(existing.fields);
  return (
    <div className="schema-ed">
      <table className="rc-table schema-ed__table">
        <thead>
          <tr>
            <th>Field</th>
            <th>Type</th>
            <th>Default</th>
            <th className="rc-col-tight">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr className="schema-ed__fixed">
            <td>
              <span className="mono">document_id</span>
            </td>
            <td className="muted">text</td>
            <td className="muted">set for every record</td>
            <td className="rc-col-tight">
              <Lock size={12} strokeWidth={2} aria-label="Built in" className="schema-ed__lock" />
            </td>
          </tr>
          {existingEntries.map(([name, spec]) => (
            <tr key={name} className="schema-ed__fixed">
              <td>
                <span className="mono">{name}</span>
              </td>
              <td>{TYPE_LABEL[spec.type]}</td>
              <td className="mono">{JSON.stringify(spec.default)}</td>
              <td className="rc-col-tight">
                <Lock size={12} strokeWidth={2} aria-label="Cannot be changed" className="schema-ed__lock" />
              </td>
            </tr>
          ))}
          {drafts.map((d, i) => (
            <tr key={i} className="schema-ed__new">
              <td>
                <TextInput
                  mono
                  aria-label={`New field ${i + 1} name`}
                  placeholder="field_name"
                  value={d.name}
                  onChange={(e) => set(i, { name: e.target.value })}
                  aria-invalid={errors[i] ? true : undefined}
                  aria-describedby={errors[i] ? `schema-err-${i}` : undefined}
                  disabled={disabled}
                />
                {errors[i] ? (
                  <p className="rc-field__error" id={`schema-err-${i}`}>
                    {errors[i]}
                  </p>
                ) : null}
              </td>
              <td>
                <Select aria-label={`New field ${i + 1} type`} value={d.type} onChange={(e) => set(i, { type: e.target.value as FieldType, default: e.target.value === "bool" ? "false" : "" })} disabled={disabled}>
                  {(Object.keys(TYPE_LABEL) as FieldType[]).map((t) => (
                    <option key={t} value={t}>
                      {TYPE_LABEL[t]}
                    </option>
                  ))}
                </Select>
              </td>
              <td>
                {d.type === "bool" ? (
                  <Select aria-label={`New field ${i + 1} default`} value={d.default} onChange={(e) => set(i, { default: e.target.value })} disabled={disabled}>
                    <option value="false">false</option>
                    <option value="true">true</option>
                  </Select>
                ) : (
                  <TextInput
                    mono={d.type !== "str"}
                    aria-label={`New field ${i + 1} default`}
                    inputMode={d.type === "int" ? "numeric" : d.type === "float" ? "decimal" : undefined}
                    placeholder={d.type === "str" ? "empty text" : "0"}
                    value={d.default}
                    onChange={(e) => set(i, { default: e.target.value })}
                    disabled={disabled}
                  />
                )}
              </td>
              <td className="rc-col-tight">
                <IconButton icon={Trash2} label={`Remove new field ${i + 1}`} onClick={() => onChange(drafts.filter((_, j) => j !== i))} disabled={disabled} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="schema-ed__foot">
        <Button size="sm" icon={Plus} onClick={() => onChange([...drafts, emptyDraft()])} disabled={disabled}>
          Add field
        </Button>
      </div>
    </div>
  );
}
