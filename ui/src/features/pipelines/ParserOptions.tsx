import { RotateCcw } from "lucide-react";
import { useState } from "react";
import { Button, IconButton } from "../../components/Button";
import { Checkbox, Field, Select, TextInput } from "../../components/Field";
import { NumberInput } from "../../components/NumberInput";
import type { OptionSpec } from "./optionsSchema";

type Options = Record<string, unknown>;

const show = (v: unknown) => (v === null ? "none" : typeof v === "string" || Array.isArray(v) || (typeof v === "object" && v) ? JSON.stringify(v) : String(v));

function hintFor(spec: OptionSpec): string | undefined {
  const parts: string[] = [];
  if (spec.description) parts.push(/[.!?]$/.test(spec.description) ? spec.description : `${spec.description}.`);
  if (spec.default !== undefined && spec.default !== null && spec.control.kind !== "boolean" && !/\bdefault\b/i.test(spec.description ?? "")) parts.push(`Default ${show(spec.default)}.`);
  return parts.join(" ") || undefined;
}

const Name = ({ k }: { k: string }) => <code className="mono">{k}</code>;

/** A JSON value for options the form has no control for (lists, mixed choices) and for keys the
 *  parser does not list. */
function JsonValue({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const [bad, setBad] = useState(false);
  const text = value === undefined ? "" : JSON.stringify(value);
  return (
    <Field label={<Name k={spec.key} />} hint={hintFor(spec) ?? "A JSON value."} error={bad ? "Not valid JSON." : (error ?? null)}>
      {(a11y) => (
        <TextInput
          {...a11y}
          mono
          key={text}
          defaultValue={text}
          placeholder="not set"
          spellCheck={false}
          onBlur={(e) => {
            const t = e.target.value.trim();
            if (!t) return (setBad(false), onChange(undefined));
            try {
              onChange(JSON.parse(t));
              setBad(false);
            } catch {
              setBad(true);
            }
          }}
        />
      )}
    </Field>
  );
}

function OptionControl({ spec, value, onChange, error }: { spec: OptionSpec; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const c = spec.control;
  const hint = hintFor(spec);
  if (c.kind === "boolean") {
    return (
      <Field hint={hint} error={error ?? null}>
        {(a11y) => <Checkbox {...a11y} checked={typeof value === "boolean" ? value : spec.default === true} onChange={(e) => onChange(e.target.checked)} label={<Name k={spec.key} />} />}
      </Field>
    );
  }
  if (c.kind === "enum") {
    return (
      <Field label={<Name k={spec.key} />} hint={hint} error={error ?? null}>
        {(a11y) => (
          <Select {...a11y} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value || undefined)}>
            <option value="">{spec.default !== undefined && spec.default !== null ? `Not set (${show(spec.default)})` : "Not set"}</option>
            {c.choices.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
            {typeof value === "string" && !c.choices.includes(value) ? <option value={value}>{value} (not allowed)</option> : null}
          </Select>
        )}
      </Field>
    );
  }
  if (c.kind === "integer" || c.kind === "number") {
    return (
      <NumberInput
        label={<Name k={spec.key} />}
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
  }
  if (c.kind === "string") {
    return (
      <Field label={<Name k={spec.key} />} hint={hint} error={error ?? null}>
        {(a11y) => <TextInput {...a11y} value={typeof value === "string" ? value : ""} placeholder={typeof spec.default === "string" && spec.default ? spec.default : "not set"} onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)} />}
      </Field>
    );
  }
  return <JsonValue spec={spec} value={value} onChange={onChange} error={error} />;
}

/**
 * The form for one parse route's options, generated from the parser's `options_schema`. Only the
 * options you set are sent; the rest keep the parser's defaults. A key the schema does not list
 * (a typo, or an option from an older parser) is shown too, so it can be seen and removed.
 */
export function ParserOptions({
  specs,
  value,
  onChange,
  problems,
  asJson,
}: {
  specs: OptionSpec[];
  value: Options | undefined;
  onChange: (v: Options | undefined) => void;
  problems: { path: string; message: string }[];
  /** The JSON-box editor, shown when the user switches to it. */
  asJson: React.ReactNode;
}) {
  const [json, setJson] = useState(false);
  const current = value ?? {};
  const known = new Set(specs.map((s) => s.key));
  const extra = Object.keys(current).filter((k) => !known.has(k));
  const errorFor = (key: string) => problems.find((p) => p.path === `.${key}` || p.path.startsWith(`.${key}.`) || p.path.startsWith(`.${key}[`))?.message;
  const set = (key: string, v: unknown) => {
    const next = { ...current };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(Object.keys(next).length ? next : undefined);
  };
  const toggle = (
    <Button size="sm" variant="ghost" aria-pressed={json} onClick={() => setJson(!json)}>
      {json ? "Edit as form" : "Edit as JSON"}
    </Button>
  );
  if (json) {
    return (
      <div className="opts">
        <div className="opts__head">
          <span className="opts__title">Options</span>
          {toggle}
        </div>
        {asJson}
      </div>
    );
  }
  return (
    <div className="opts" role="group" aria-label="Parser options">
      <div className="opts__head">
        <span className="opts__title">Options</span>
        <span className="opts__note">Only the options you set are sent; the rest use the parser's defaults.</span>
        {toggle}
      </div>
      <div className="cfg-grid">
        {specs.map((spec) => (
          <div key={spec.key} className="opt">
            <OptionControl spec={spec} value={current[spec.key]} onChange={(v) => set(spec.key, v)} error={errorFor(spec.key)} />
            {spec.key in current ? (
              <span className="opt__reset">
                <IconButton icon={RotateCcw} label={`Reset ${spec.key}`} onClick={() => set(spec.key, undefined)} />
              </span>
            ) : null}
          </div>
        ))}
        {extra.map((key) => (
          <div key={key} className="opt">
            <JsonValue spec={{ key, control: { kind: "json" }, description: "This parser does not list this option.", default: undefined }} value={current[key]} onChange={(v) => set(key, v)} error={errorFor(key)} />
            <span className="opt__reset">
              <IconButton icon={RotateCcw} label={`Remove ${key}`} onClick={() => set(key, undefined)} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
