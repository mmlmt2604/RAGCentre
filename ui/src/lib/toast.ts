import { createContext, useContext, type ReactNode } from "react";

export type Tone = "ok" | "danger" | "info";
export interface ToastItem {
  id: number;
  tone: Tone;
  title: ReactNode;
  body?: ReactNode;
}

export const ToastCtx = createContext<((t: Omit<ToastItem, "id">) => void) | null>(null);

export function useToast() {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx;
}
