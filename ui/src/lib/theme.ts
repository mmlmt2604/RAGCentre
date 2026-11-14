import { useEffect, useState } from "react";

export type ThemePref = "dark" | "light" | "system";
const KEY = "ragcentre.theme";

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "dark" || v === "light" || v === "system") return v;
  } catch {
    /* storage unavailable */
  }
  return "dark";
}

function resolve(pref: ThemePref): "dark" | "light" {
  if (pref !== "system") return pref;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function applyTheme(pref: ThemePref): void {
  document.documentElement.dataset.theme = resolve(pref);
}

/** Theme preference, defaulting to dark (the console is dark-first). */
export function useTheme(): [ThemePref, (pref: ThemePref) => void] {
  const [pref, setPref] = useState<ThemePref>(readPref);

  useEffect(() => {
    applyTheme(pref);
    try {
      localStorage.setItem(KEY, pref);
    } catch {
      /* storage unavailable */
    }
    if (pref !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => applyTheme("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [pref]);

  return [pref, setPref];
}

export function initTheme(): void {
  applyTheme(readPref());
}
