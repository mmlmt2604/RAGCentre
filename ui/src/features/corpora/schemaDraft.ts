// Draft metadata fields for the schema editor, and turning them into a MetadataSchema (§10.3).
import type { FieldType, MetadataSchema, Scalar } from "../../api/types";
import { METADATA_FIELD_RE } from "../../lib/configSchema";

export interface DraftField {
  name: string;
  type: FieldType;
  default: string; // edited as text; booleans as "true"/"false"
}

export const TYPE_LABEL: Record<FieldType, string> = { str: "text", int: "integer", float: "number", bool: "true / false" };

export function emptyDraft(): DraftField {
  return { name: "", type: "str", default: "" };
}

function parseDefault(d: DraftField): Scalar | null {
  const t = d.default.trim();
  if (d.type === "str") return d.default;
  if (d.type === "bool") return t === "true" ? true : t === "false" ? false : null;
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  if (d.type === "int" && !Number.isInteger(n)) return null;
  return n;
}

/** Validate new fields against the schema rules (§10.3); returns per-row errors and the schema. */
export function buildSchema(existing: MetadataSchema, drafts: DraftField[]): { schema: MetadataSchema; errors: Record<number, string> } {
  const fields = { ...existing.fields };
  const errors: Record<number, string> = {};
  drafts.forEach((d, i) => {
    const name = d.name.trim();
    if (!name && d.default === "") return; // an untouched blank row is ignored
    if (!METADATA_FIELD_RE.test(name)) errors[i] = "Use letters, digits and underscores, starting with a letter or underscore.";
    else if (name === "document_id") errors[i] = "document_id is reserved; every record already has it.";
    else if (name in fields) errors[i] = `${name} already exists.`;
    else {
      const value = parseDefault(d);
      if (value === null) errors[i] = d.type === "bool" ? "Choose a default." : `Give a default ${TYPE_LABEL[d.type]}.`;
      else fields[name] = { type: d.type, default: value };
    }
  });
  return { schema: { fields }, errors };
}

