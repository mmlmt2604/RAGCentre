import { useEffect, useRef } from "react";
import { useOcr } from "../../api/hooks";
import type { OcrState } from "../../api/types";
import { useToast } from "../../lib/toast";
import { isProblem, OCR_STATE } from "./model";

/**
 * Announces changes, never the state the page opened with: a shim appearing, OCR becoming ready,
 * OCR breaking. The toast region is a live region, so screen readers hear the same thing.
 */
export function OcrWatcher() {
  const { data } = useOcr();
  const toast = useToast();
  const seen = useRef<{ states: Map<string, OcrState>; settled: boolean } | null>(null);

  useEffect(() => {
    if (!data) return;
    const before = seen.current;
    seen.current = { states: new Map(data.services.map((s) => [s.id, s.state])), settled: !data.scanning || Boolean(before?.settled) };
    if (!before) return;
    for (const s of data.services) {
      const was = before.states.get(s.id);
      if (was === s.state) continue;
      const name = s.provider_name ?? s.name ?? s.base_url;
      if (!s.provider_id) {
        if (was === undefined && before.settled && s.state === "detected")
          toast({ tone: "info", title: `Found ${name}`, body: "Add it in Providers to test whether its OCR engine works." });
      } else if (s.state === "ready") {
        toast({ tone: "ok", title: `${name}: OCR ready`, body: "The shim and its OCR engine are running." });
      } else if (isProblem(s.state) && (was === undefined || !isProblem(was))) {
        toast({ tone: "danger", title: `${name}: ${OCR_STATE[s.state].label}`, body: s.message });
      }
    }
  }, [data, toast]);

  return null;
}
