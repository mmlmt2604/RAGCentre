import { useId } from "react";
import { Segmented, TextInput } from "../../components/Field";

export interface SecretDraft {
  mode: "env" | "value" | "skip";
  value: string;
}

/**
 * Choose where a secret comes from: an environment variable on the server, or a
 * value stored encrypted (only when the server has RAGCENTRE_SECRET_KEY).
 */
export function SecretInput({
  secretKey,
  draft,
  onChange,
  storageEnabled,
  allowSkip = false,
  error,
  disabled,
}: {
  secretKey: string;
  draft: SecretDraft;
  onChange: (d: SecretDraft) => void;
  storageEnabled: boolean;
  allowSkip?: boolean;
  error?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const options: { value: SecretDraft["mode"]; label: string; disabled?: boolean; title?: string }[] = [
    { value: "env", label: "Environment variable" },
    { value: "value", label: "Store value", disabled: !storageEnabled, title: storageEnabled ? undefined : "This server cannot store secrets (no RAGCENTRE_SECRET_KEY)" },
    ...(allowSkip ? [{ value: "skip" as const, label: "Skip" }] : []),
  ];
  return (
    <div className="secret-in">
      <div className="secret-in__head">
        <span className="mono secret-in__key">{secretKey}</span>
        <Segmented
          label={`Source for ${secretKey}`}
          value={draft.mode}
          onChange={(mode) => onChange({ mode, value: mode === draft.mode ? draft.value : "" })}
          options={options.map((option) => ({ ...option, disabled: disabled || option.disabled }))}
        />
      </div>
      {draft.mode === "skip" ? (
        <p className="rc-field__hint">Set it later on the provider's page.</p>
      ) : (
        <>
          <TextInput
            id={id}
            mono
            type={draft.mode === "value" ? "password" : "text"}
            autoComplete="off"
            spellCheck={false}
            aria-label={draft.mode === "env" ? `Environment variable for ${secretKey}` : `Value for ${secretKey}`}
            placeholder={draft.mode === "env" ? "OPENAI_API_KEY" : "Paste the value"}
            value={draft.value}
            onChange={(e) => onChange({ ...draft, value: e.target.value })}
            aria-invalid={error ? true : undefined}
            aria-describedby={`${id}-hint`}
            disabled={disabled}
          />
          {error ? (
            <p className="rc-field__error" id={`${id}-hint`}>
              {error}
            </p>
          ) : (
            <p className="rc-field__hint" id={`${id}-hint`}>
              {draft.mode === "env"
                ? "Only the variable name is saved. The server reads its value each time it calls the service."
                : "Encrypted with the server's RAGCENTRE_SECRET_KEY. It cannot be read back."}
            </p>
          )}
        </>
      )}
      {!storageEnabled ? (
        <p className="rc-field__hint secret-in__off">
          Storing values is off: this server has no <span className="mono">RAGCENTRE_SECRET_KEY</span>. Use an environment variable, or set the key on the
          server and restart it.
        </p>
      ) : null}
    </div>
  );
}
