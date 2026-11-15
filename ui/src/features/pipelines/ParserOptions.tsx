import { RotateCcw } from "lucide-react";
import { useState } from "react";
import { Button, IconButton } from "../../components/Button";
import { Checkbox, Field, Select, TextInput } from "../../components/Field";
import { NumberInput } from "../../components/NumberInput";
import { isObject, type OptionSpec } from "./optionSpecs";

type Options = Record<string, unknown>;

const show = (v: unknown) => (v === null ? "none" : typeof v === "string" ? JSON.stringify(v) : Array.isArray(v) || isObject(v) ? JSON.stringify(v) : String(v));

function hintFor(s: OptionSpec): string | undefined {
  const parts: string[] = [];
  if (s.description) parts.push(/[.!?]$/.test(s.description) ? s.description : `${s.description}.`);
  if (s.default !== undefined) parts.push(`Default ${show(s.default)}.`);
  return parts.join(" ") || undefined;
}

const code = (key: string) => <code className="mono">{key}</code>;

/** One property edited as JSON text; a blank box means unset. */
function JsonKey({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const text = value === undefined ? "" : JSON.stringify(value);
  return (
    <Field label={code(spec.key)} hint={hintFor(spec) ?? "A JSON value."} error={error ?? null}>
      {(a11y) => (
        <TextInput
          {...a11y}
          mono
          key={text}
          defaultValue={text}
          placeholder="not set"
          onBlur={(e) => {
            const t = e.target.value.trim();
            if (!t) return onChange(undefined);
            try {
              const v = JSON.parse(t);
              e.target.setCustomValidity("");
              onChange(v);
            } catch {
              e.target.setCustomValidity("Not valid JSON");
              e.target.reportValidity();
            }
          }}
        />
      )}
    </Field>
  );
}

/** A comma-separated list of strings. It keeps what was typed while the field has focus. */
function StringList({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: string[] | undefined) => void; error?: string }) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = Array.isArray(value) ? value.join(", ") : "";
  return (
    <Field label={code(spec.key)} hint={hintFor(spec) ? `${hintFor(spec)} Separate items with commas.` : "Separate items with commas."} error={error ?? null}>
      {(a11y) => (
        <TextInput
          {...a11y}
          mono
          value={draft ?? shown}
          placeholder="not set"
          onFocus={() => setDraft(shown)}
          onBlur={() => setDraft(null)}
          onChange={(e) => {
            setDraft(e.target.value);
            const items = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
            onChange(items.length ? items : undefined);
          }}
        />
      )}
    </Field>
  );
}

function OptionControl({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const c = spec.control;
  const hint = hintFor(spec);
  if (c.kind === "boolean")
    return (
      <Field hint={hint} error={error ?? null}>
        {(a11y) => (
          <Checkbox
            {...a11y}
            checked={typeof value === "boolean" ? value : spec.default === true}
            onChange={(e) => onChange(e.target.checked)}
            label={code(spec.key)}
          />
        )}
      </Field>
    );
  if (c.kind === "enum")
    return (
      <Field label={code(spec.key)} hint={hint} error={error ?? null}>
        {(a11y) => (
          <Select {...a11y} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value || undefined)}>
            <option value="">{spec.default !== undefined ? `Not set (${show(spec.default)})` : "Not set"}</option>
            {c.choices.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
            {typeof value === "string" && !c.choices.includes(value) ? <option value={value}>{value} (not allowed)</option> : null}
          </Select>
        )}
      </Field>
    );
  if (c.kind === "integer" || c.kind === "number")
    return (
      <NumberInput
        label={code(spec.key)}
        integer={c.kind === "integer"}
        min={c.min}
        max={c.max}
        hint={hint}
        placeholder={typeof spec.default === "number" ? String(spec.default) : "not set"}
        value={typeof value === "number" ? value : undefined}
        onChange={onChange}
        error={error}
      />
    );
  if (c.kind === "string")
    return (
      <Field label={code(spec.key)} hint={hint} error={error ?? null}>
        {(a11y) => (
          <TextInput
            {...a11y}
            value={typeof value === "string" ? value : ""}
            placeholder={typeof spec.default === "string" && spec.default ? spec.default : "not set"}
            onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
          />
        )}
      </Field>
    );
  if (c.kind === "strings") return <StringList spec={spec} value={value} onChange={onChange} error={error} />;
  return <JsonKey spec={spec} value={value} onChange={onChange} error={error} />;
}

/** The whole-object JSON box: the same control the parse route has when its parser reports no schema. */
export function JsonOptions({
  value,
  onChange,
  label,
  error,
}: {
  value: Options | undefined;
  onChange: (v: Options | undefined) => void;
  label: string;
  error?: string | null;
}) {
  const text = value && Object.keys(value).length ? JSON.stringify(value) : "";
  return (
    <Field label={label} hint='A JSON object passed to the service, e.g. {"ocr": true}. Leave blank for none.' error={error ?? null}>
      {(a11y) => (
        <TextInput
          {...a11y}
          mono
          key={text}
          defaultValue={text}
          placeholder="{}"
          onBlur={(e) => {
            const t = e.target.value.trim();
            if (!t) return onChange(undefined);
            try {
              const v = JSON.parse(t);
              if (v && typeof v === "object" && !Array.isArray(v)) {
                e.target.setCustomValidity("");
                onChange(v as Options);
              } else throw new Error();
            } catch {
              e.target.setCustomValidity("Not a JSON object");
              e.target.reportValidity();
            }
          }}
        />
      )}
    </Field>
  );
}

/**
 * A parse route's options, edited as a form built from the parser's `options_schema`.
 * An unset control is absent from the options: defaults are shown, never written (they would change
 * the fingerprint). `problems` have paths relative to the options object: "", ".key", ".key[0]".
 */
export function ParserOptions({
  specs,
  value,
  onChange,
  problems,
}: {
  specs: OptionSpec[];
  value: Options | undefined;
  onChange: (v: Options | undefined) => void;
  problems: { path: string; message: string }[];
}) {
  const [asJson, setAsJson] = useState(false);
  const current = value ?? {};
  const known = new Set(specs.map((s) => s.key));
  const unknown = Object.keys(current).filter((k) => !known.has(k));
  const errFor = (key: string) => problems.find((p) => p.path === `.${key}` || p.path.startsWith(`.${key}.`) || p.path.startsWith(`.${key}[`))?.message;
  const whole = problems.find((p) => p.path === "")?.message;

  const set = (key: string, v: unknown) => {
    const next = { ...current };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(Object.keys(next).length ? next : undefined);
  };

  const toggle = (
    <Button size="sm" variant="ghost" aria-pressed={asJson} onClick={() => setAsJson(!asJson)}>
      {asJson ? "Edit as form" : "Edit as JSON"}
    </Button>
  );

  if (asJson)
    return (
      <div className="opts">
        <div className="opts__head">
          <span className="opts__title">Options</span>
          {toggle}
        </div>
        <JsonOptions
          label="Options as JSON"
          value={value}
          onChange={onChange}
          error={problems.length ? problems.map((p) => `${p.path.replace(/^\./, "") || "options"}: ${p.message}`).join(" ") : null}
        />
      </div>
    );

  return (
    <div className="opts" role="group" aria-label="Parser options">
      <div className="opts__head">
        <span className="opts__title">Options</span>
        <span className="opts__note">Only the options you set are sent; the rest use the parser's defaults.</span>
        {toggle}
      </div>
      {whole ? <p className="rc-field__error">{whole}</p> : null}
      <div className="cfg-grid">
        {specs.map((s) => (
          <div key={s.key} className="opt">
            <OptionControl spec={s} value={current[s.key]} onChange={(v) => set(s.key, v)} error={errFor(s.key)} />
            {s.key in current ? (
              <span className="opt__reset">
                <IconButton icon={RotateCcw} label={`Reset ${s.key}`} onClick={() => set(s.key, undefined)} />
              </span>
            ) : null}
          </div>
        ))}
        {unknown.map((k) => (
          <div key={k} className="opt">
            <JsonKey spec={{ key: k, control: { kind: "json" }, description: "This parser does not list this option.", default: undefined }} value={current[k]} onChange={(v) => set(k, v)} error={errFor(k)} />
            <span className="opt__reset">
              <IconButton icon={RotateCcw} label={`Remove ${k}`} onClick={() => set(k, undefined)} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
