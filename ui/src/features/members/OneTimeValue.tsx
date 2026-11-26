import { useState } from "react";
import { Button } from "../../components/Button";
import { TextInput } from "../../components/Field";

export function OneTimeValue({ value, title, onClose }: { value: string; title: string; onClose: () => void }) {
  const [copiedValue, setCopiedValue] = useState<string>();
  const [failed, setFailed] = useState(false);
  return <section className="form-section" aria-label={title}>
    <h2>{title}</h2><p>Copy and share this value privately now. It is shown only once and is not saved in this browser.</p>
    <TextInput aria-label={title} readOnly mono value={value} onFocus={(e) => e.target.select()} />
    <div className="form-actions"><Button onClick={() => {
      setFailed(false);
      void Promise.resolve().then(() => navigator.clipboard.writeText(value)).then(() => setCopiedValue(value)).catch(() => setFailed(true));
    }}>{copiedValue === value ? "Copied" : "Copy"}</Button><Button variant="ghost" onClick={onClose}>Dismiss</Button>{failed ? <span role="status">Select the value and copy it manually.</span> : null}</div>
  </section>;
}
