import { CircleAlert, CircleCheck, X } from "lucide-react";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { ToastCtx, type ToastItem } from "../lib/toast";

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(1);

  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((x) => x.id !== id)), []);
  const push = useCallback(
    (t: Omit<ToastItem, "id">) => {
      const id = next.current++;
      setItems((xs) => [...xs.slice(-3), { ...t, id }]);
      window.setTimeout(() => dismiss(id), t.tone === "danger" ? 9000 : 5000);
    },
    [dismiss],
  );
  const value = useMemo(() => push, [push]);

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="rc-toasts" role="region" aria-label="Notifications">
        <div aria-live="polite" aria-atomic="false">
          {items.map((t) => (
            <div key={t.id} className={`rc-toast rc-toast--${t.tone}`} role={t.tone === "danger" ? "alert" : "status"}>
              {t.tone === "danger" ? <CircleAlert size={16} aria-hidden /> : <CircleCheck size={16} aria-hidden />}
              <div className="rc-toast__text">
                <strong>{t.title}</strong>
                {t.body ? <p>{t.body}</p> : null}
              </div>
              <button type="button" className="rc-iconbtn" aria-label="Dismiss notification" onClick={() => dismiss(t.id)}>
                <X size={14} aria-hidden />
              </button>
            </div>
          ))}
        </div>
      </div>
    </ToastCtx.Provider>
  );
}
