import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { NavLink } from "react-router";

/** Navigation tabs: each tab is a link, so the selected tab lives in the URL. */
export function NavTabs({ items, label }: { items: { to: string; label: ReactNode; count?: number; end?: boolean }[]; label: string }) {
  return (
    <nav className="rc-tabs" aria-label={label}>
      {items.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className="rc-tabs__tab">
          {item.label}
          {item.count !== undefined ? <span className="rc-tabs__count num">{item.count}</span> : null}
        </NavLink>
      ))}
    </nav>
  );
}

/** In-page tabs with roving focus (arrow keys) following the WAI-ARIA tabs pattern. */
export function Tabs<T extends string>({
  value,
  onChange,
  items,
  label,
}: {
  value: T;
  onChange: (v: T) => void;
  items: { value: T; label: ReactNode; count?: number }[];
  label: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const next = (i + dir + items.length) % items.length;
    refs.current[next]?.focus();
    onChange(items[next]!.value);
  };
  return (
    <div className="rc-tabs" role="tablist" aria-label={label}>
      {items.map((item, i) => (
        <button
          key={item.value}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="tab"
          aria-selected={item.value === value}
          tabIndex={item.value === value ? 0 : -1}
          className="rc-tabs__tab"
          onClick={() => onChange(item.value)}
          onKeyDown={(e) => onKey(e, i)}
        >
          {item.label}
          {item.count !== undefined ? <span className="rc-tabs__count num">{item.count}</span> : null}
        </button>
      ))}
    </div>
  );
}
