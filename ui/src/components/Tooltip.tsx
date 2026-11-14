import { cloneElement, useId, useLayoutEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * Tooltip shown on hover and keyboard focus, rendered in a portal with fixed
 * positioning so scroll containers never clip it.
 */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactElement<Record<string, unknown>> }) {
  const id = useId();
  const anchor = useRef<HTMLSpanElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    if (!open || !anchor.current || !tip.current) return;
    const a = anchor.current.getBoundingClientRect();
    const t = tip.current.getBoundingClientRect();
    let top = a.top - t.height - 6;
    if (top < 4) top = a.bottom + 6;
    const left = Math.min(Math.max(4, a.left + a.width / 2 - t.width / 2), window.innerWidth - t.width - 4);
    setPos({ top, left });
  }, [open]);

  const show = () => setOpen(true);
  const hide = () => {
    setOpen(false);
    setPos(null);
  };

  return (
    <span ref={anchor} className="rc-tip-anchor" onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      {cloneElement(children, { "aria-describedby": open ? id : undefined })}
      {open
        ? createPortal(
            <div ref={tip} id={id} role="tooltip" className="rc-tooltip" style={pos ? { top: pos.top, left: pos.left } : { top: -9999, left: -9999 }}>
              {content}
            </div>,
            document.body,
          )
        : null}
    </span>
  );
}
