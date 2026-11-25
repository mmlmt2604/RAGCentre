import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Page } from "../../api/types";
import { useTransport } from "../auth/context";

/** Management lists follow cursors so owners and administrators can reach every account. */
export function useDirectory<T>(path: string, enabled = true) {
  const { request } = useTransport();
  return useQuery({ queryKey: ["directory", path], enabled, queryFn: async ({ signal }) => {
    const items: T[] = [];
    let cursor: string | null = null;
    do {
      const page: Page<T> = await request(path, { query: { limit: 500, cursor }, signal });
      items.push(...page.items); cursor = page.next_cursor;
    } while (cursor);
    return items;
  } });
}
export function useManagementAction<T, R = void>(action: (value: T, transport: ReturnType<typeof useTransport>) => Promise<R>) {
  const transport = useTransport();
  const qc = useQueryClient();
  return useMutation({ mutationFn: (value: T) => action(value, transport), onSuccess: () => { if (transport.isCurrent()) void qc.invalidateQueries({ queryKey: ["directory"] }); } });
}
