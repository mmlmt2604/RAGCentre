import { Check, Copy } from "lucide-react";
import { useMemo, useRef, useState, type KeyboardEvent } from "react";

export function CodeBlock({ code, label, maxHeight }: { code: string; label?: string; maxHeight?: number }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard unavailable */
    }
  };
  return (
    <div className="rc-code">
      <div className="rc-code__bar">
        <span>{label}</span>
        <button type="button" className="rc-iconbtn" onClick={copy} aria-label={copied ? "Copied" : "Copy code"}>
          {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
        </button>
      </div>
      <pre style={{ maxHeight }}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

/**
 * Plain-text code editor for JSON and YAML: line-number gutter, Tab inserts two
 * spaces, Escape then Tab leaves the field (so keyboard users are never trapped).
 */
export function CodeEditor({
  value,
  onChange,
  label,
  language,
  rows = 18,
  invalidLine,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  language: "json" | "yaml";
  rows?: number;
  invalidLine?: number | null;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const [escaped, setEscaped] = useState(false);
  const lines = useMemo(() => value.split("\n").length, [value]);

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape") {
      setEscaped(true);
      return;
    }
    if (e.key === "Tab" && !escaped && !e.shiftKey) {
      e.preventDefault();
      const el = e.currentTarget;
      const { selectionStart: s, selectionEnd: end } = el;
      const next = `${value.slice(0, s)}  ${value.slice(end)}`;
      onChange(next);
      requestAnimationFrame(() => el.setSelectionRange(s + 2, s + 2));
      return;
    }
    setEscaped(false);
  };

  return (
    <div className="rc-editor" data-lang={language}>
      <div className="rc-editor__gutter" ref={gutter} aria-hidden>
        {Array.from({ length: Math.max(lines, rows) }, (_, i) => (
          <span key={i} data-bad={invalidLine === i + 1 || undefined}>
            {i + 1}
          </span>
        ))}
      </div>
      <textarea
        ref={ref}
        aria-label={label}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        rows={rows}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKey}
        onScroll={(e) => {
          if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop;
        }}
      />
    </div>
  );
}
