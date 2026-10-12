export type KeepStatus =
  | { readonly state: "starting" }
  | { readonly state: "missing" }
  | { readonly state: "unconfigured"; readonly detail: string }
  | { readonly state: "unreachable"; readonly detail: string; readonly retryInMs: number | null }
  | { readonly state: "connected"; readonly agentId: string; readonly personalMode: boolean; readonly kitVersion: string };

export interface KeepItem { readonly title: string; readonly body: string }

export interface KeepHighlights {
  readonly online: string;
  readonly bulletin: string;
  readonly paused: readonly { readonly sessionId: string; readonly goal: string }[];
}

export const KEEP_INSTALL_COMMANDS = {
  unix: "curl -fsSL https://firekeep.ai/latest/install | sh",
  windows: "irm https://firekeep.ai/latest/install.ps1 | iex",
} as const;
