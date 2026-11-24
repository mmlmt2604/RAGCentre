import { Check, Copy } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

export function CodeBlock({ code, label, maxHeight, copyLabel }: { code: string; label?: string; maxHeight?: number; copyLabel?: boolean }) {
  const [feedback, setFeedback] = useState<"copied" | "manual">();
  const content = useRef<HTMLElement>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async () => {
    window.clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(code);
      if (!content.current) return;
      setFeedback("copied");
      timer.current = window.setTimeout(() => setFeedback(undefined), 1400);
    } catch {
      const selection = window.getSelection();
      if (!selection || !content.current) return;
      const range = document.createRange();
      range.selectNodeContents(content.current);
      selection.removeAllRanges();
      selection.addRange(range);
      setFeedback("manual");
    }
  };
  return (
    <div className="rc-code">
      <div className="rc-code__bar">
        <span>{label}</span>
        {feedback === "manual" ? <span role="status">Press ⌘C / Ctrl+C</span> : null}
        <button type="button" className={copyLabel ? "rc-btn rc-btn--ghost rc-btn--sm" : "rc-iconbtn"} onClick={copy}
          aria-label={feedback === "copied" ? "Copied" : copyLabel ? "Copy" : "Copy code"}>
          {feedback === "copied" ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
          {copyLabel ? <span>{feedback === "copied" ? "Copied" : "Copy"}</span> : null}
        </button>
      </div>
      <pre style={{ maxHeight }}>
        <code ref={content}>{code}</code>
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
