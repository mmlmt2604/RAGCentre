import type { Corpus, Document, FieldType, MetadataSchema, PipelineSummary, Scalar } from "../../api/types";

const MIME_LABEL: Record<string, string> = {
  "application/pdf": "PDF",
  "text/markdown": "Markdown",
  "text/plain": "Text",
  "text/html": "HTML",
  "text/csv": "CSV",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
};

export function mimeLabel(mime: string): string {
  if (MIME_LABEL[mime]) return MIME_LABEL[mime]!;
  if (mime.startsWith("image/")) return `Image (${mime.slice(6)})`;
  return mime;
}

/** Non-archived pipelines with the active one first. */
export function orderedPipelines(corpus: Corpus | undefined): PipelineSummary[] {
  if (!corpus) return [];
  return [...corpus.pipelines].sort((a, b) => Number(b.id === corpus.active_pipeline_id) - Number(a.id === corpus.active_pipeline_id) || a.name.localeCompare(b.name));
}

/** Metadata as shown: every schema field, with defaults marked. */
export function metadataView(doc: Document, schema: MetadataSchema): { key: string; value: Scalar; isDefault: boolean }[] {
  return Object.entries(schema.fields).map(([key, spec]) => ({
    key,
    value: key in doc.metadata ? doc.metadata[key]! : spec.default,
    isDefault: !(key in doc.metadata),
  }));
}

/** Text in a metadata input -> typed value; `undefined` when left blank (the field's default applies). */
export function parseMetaValue(text: string, type: FieldType): { value?: Scalar; error?: string } {
  if (text === "") return {};
  if (type === "str") return { value: text };
  if (type === "bool") return text === "true" ? { value: true } : text === "false" ? { value: false } : { error: "Choose true or false." };
  const n = Number(text.trim());
  if (!Number.isFinite(n)) return { error: "Enter a number." };
  if (type === "int" && !Number.isInteger(n)) return { error: "Enter a whole number." };
  return { value: n };
}

export function metaDraft(doc: Pick<Document, "metadata"> | null, schema: MetadataSchema): Record<string, string> {
  return Object.fromEntries(Object.keys(schema.fields).map((k) => [k, doc && k in doc.metadata ? String(doc.metadata[k]) : ""]));
}

/** Validate a metadata draft; returns the typed metadata (blank fields omitted) or per-field errors. */
export function buildMetadata(draft: Record<string, string>, schema: MetadataSchema): { metadata: Record<string, Scalar>; errors: Record<string, string> } {
  const metadata: Record<string, Scalar> = {};
  const errors: Record<string, string> = {};
  for (const [key, spec] of Object.entries(schema.fields)) {
    const r = parseMetaValue(draft[key] ?? "", spec.type);
    if (r.error) errors[key] = r.error;
    else if (r.value !== undefined) metadata[key] = r.value;
  }
  return { metadata, errors };
}

export function shortName(filename: string): string {
  return filename.split("/").pop() ?? filename;
}

// ---------- uploads ----------

export interface Staged {
  key: string;
  file: File;
  status: "staged" | "uploading" | "added" | "exists" | "failed";
  progress: number;
  message?: string;
  documentId?: string;
}

export function stage(files: FileList | File[]): Staged[] {
  return Array.from(files).map((file, i) => ({ key: `${file.name}:${file.size}:${file.lastModified}:${Date.now()}:${i}`, file, status: "staged", progress: 0 }));
}
