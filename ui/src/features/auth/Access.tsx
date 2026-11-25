import type { ReactNode } from "react";
import { useCan } from "./context";
import type { Capability } from "./types";

export function Can({ capability, children }: { capability: Capability; children: ReactNode }) {
  return useCan(capability) ? children : null;
}
export function Require({ capability, children }: { capability: Capability; children: ReactNode }) {
  if (!useCan(capability)) return <div className="page-pad"><h1>Access restricted</h1><p className="muted">Your role in this team does not allow this action. Contact a team owner if you need access.</p></div>;
  return children;
}
