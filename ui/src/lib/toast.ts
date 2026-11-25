import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { isCurrentGeneration, transportGeneration } from "../api/client";

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
  const [generation] = useState(transportGeneration);
  const push = useCallback((item: Omit<ToastItem, "id">) => { if (isCurrentGeneration(generation)) ctx?.(item); }, [ctx, generation]);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return push;
}
