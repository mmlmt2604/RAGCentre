import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";
import { Button } from "./Button";

/**
 * Modal dialog on the native <dialog> element (focus trap, Escape, top layer).
 * Reserve for tasks that need protected focus: confirmations of destructive or
 * consequential actions, and the access-token prompt.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  width = 440,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) {
      if (typeof el.showModal === "function") el.showModal();
      else el.setAttribute("open", "");
    } else if (!open && el.open) {
      if (typeof el.close === "function") el.close();
      else el.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="rc-dialog"
      aria-labelledby={titleId}
      style={{ width }}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      {open ? (
        <div className="rc-dialog__inner">
          <header className="rc-dialog__head">
            <h2 id={titleId}>{title}</h2>
            <button type="button" className="rc-iconbtn" aria-label="Close" onClick={onClose}>
              <X size={16} strokeWidth={1.75} aria-hidden />
            </button>
          </header>
          <div className="rc-dialog__body">{children}</div>
          {footer ? <footer className="rc-dialog__foot">{footer}</footer> : null}
        </div>
      ) : null}
    </dialog>
  );
}

/** A confirmation that names the thing and states the consequence. */
export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  children,
  confirmLabel,
  destructive = false,
  pending = false,
  error,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: ReactNode;
  children: ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  pending?: boolean;
  error?: ReactNode;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant={destructive ? "danger" : "primary"} loading={pending} onClick={onConfirm} autoFocus>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
      {error ? <div className="rc-dialog__error" role="alert">{error}</div> : null}
    </Dialog>
  );
}
