export const RUNTIME_CAPABILITIES = [
  "chat",
  "review",
  "streaming",
  "tools",
  "approvals",
  "resume",
  "models",
  "images",
  "audio-input",
  "usage",
  "reasoning",
  "firekeep-hooks",
] as const;

export type RuntimeCapability = (typeof RUNTIME_CAPABILITIES)[number];
export type RuntimePermissionMode = "safe" | "standard" | "unrestricted";
export const RUNTIME_EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
export type RuntimeEffort = (typeof RUNTIME_EFFORTS)[number];
export type RunMode = "primary" | "review" | "handoff" | "compare";

export interface RuntimeDescriptor {
  readonly id: string;
  readonly displayName: string;
  readonly description: string;
  readonly transport: string;
  readonly capabilities: readonly RuntimeCapability[];
  readonly loginMethods?: readonly LoginMethod[];
  readonly icon?: string;
  readonly accent?: string;
}

export interface RuntimeConnection {
  readonly state: "missing" | "disconnected" | "ready" | "error";
  readonly version?: string;
  readonly detail: string;
  readonly executable?: string;
}

export interface RuntimeAuthStatus {
  readonly state: "unavailable" | "disconnected" | "connected" | "error";
  readonly label?: string;
  readonly detail?: string;
  readonly methods?: readonly LoginMethod[];
}

export type LoginMethod = "browser" | "device" | "api-key" | "console" | "sso";

export interface LoginRequest {
  readonly method?: LoginMethod;
  readonly secret?: string;
}

export type LoginResult =
  | { readonly state: "complete"; readonly message: string }
  | { readonly state: "browser"; readonly url: string; readonly message: string }
  | {
      readonly state: "device";
      readonly url: string;
      readonly code: string;
      readonly message: string;
    }
  | { readonly state: "external"; readonly message: string };

export interface RuntimeModel {
  readonly id: string;
  readonly displayName: string;
  readonly description?: string;
  readonly isDefault?: boolean;
  readonly efforts?: readonly RuntimeEffort[];
  readonly inputModalities?: readonly ("text" | "image" | "audio")[];
}

export interface RuntimeUsage {
  readonly inputTokens?: number;
  readonly cacheCreationInputTokens?: number;
  readonly cachedInputTokens?: number;
  readonly outputTokens?: number;
  readonly reasoningTokens?: number;
  readonly totalTokens?: number;
  readonly costUsd?: number;
  readonly durationMs?: number;
}

export interface RuntimeApprovalPrompt {
  readonly id: string;
  readonly title: string;
  readonly detail: string;
  readonly options: readonly string[];
}

export type RuntimeApprovalHandler = (prompt: RuntimeApprovalPrompt) => Promise<string>;

export type MessageRole = "user" | "assistant" | "reviewer" | "system";

export type RuntimeEventPayload =
  | { readonly kind: "run.started"; readonly mode: RunMode; readonly permissionMode: RuntimePermissionMode; readonly model?: string }
  | { readonly kind: "run.completed"; readonly durationMs: number }
  | { readonly kind: "run.failed"; readonly cancelled: boolean; readonly error: string; readonly durationMs: number }
  | { readonly kind: "message.delta"; readonly messageId: string; readonly role: MessageRole; readonly text: string }
  | { readonly kind: "message.completed"; readonly messageId: string; readonly role: MessageRole; readonly text: string }
  | { readonly kind: "reasoning.delta"; readonly itemId: string; readonly text: string }
  | { readonly kind: "tool.started"; readonly toolCallId: string; readonly name: string; readonly input?: unknown }
  | { readonly kind: "tool.updated"; readonly toolCallId: string; readonly update: string }
  | { readonly kind: "tool.completed"; readonly toolCallId: string; readonly name: string; readonly output?: unknown; readonly failed?: boolean }
  | { readonly kind: "diff.updated"; readonly itemId: string; readonly diff: string }
  | { readonly kind: "approval.requested"; readonly approvalId: string; readonly title: string; readonly detail: string; readonly options: readonly string[] }
  | { readonly kind: "approval.resolved"; readonly approvalId: string; readonly decision: string }
  | { readonly kind: "usage.updated"; readonly usage: RuntimeUsage }
  | { readonly kind: "notice"; readonly level: "info" | "warning" | "error"; readonly message: string; readonly detail?: string };

export interface RuntimeEvent extends RuntimeEventPayloadBase {
  readonly payload: RuntimeEventPayload;
}

export interface RuntimeEventPayloadBase {
  readonly id: string;
  readonly runId: string;
  readonly studioSessionId: string;
  readonly runtimeId: string;
  readonly timestamp: string;
  readonly raw?: unknown;
}

export type RuntimeEventSink = (event: RuntimeEventPayload, raw?: unknown) => void;

export interface RunRequest {
  readonly runId: string;
  readonly studioSessionId: string;
  readonly prompt: string;
  readonly mode: RunMode;
  readonly cwd?: string;
  readonly nativeSessionId?: string;
  readonly model?: string;
  readonly effort?: RuntimeEffort;
  readonly permissionMode: RuntimePermissionMode;
  readonly focus?: string;
  readonly requestApproval?: RuntimeApprovalHandler;
}

export interface RunResult {
  readonly nativeSessionId?: string;
  readonly finalText: string;
  readonly usage?: RuntimeUsage;
}

export interface AgentRuntime {
  readonly descriptor: RuntimeDescriptor;
  probe(): Promise<RuntimeConnection>;
  authStatus(): Promise<RuntimeAuthStatus>;
  login(request: LoginRequest): Promise<LoginResult>;
  logout(): Promise<void>;
  listModels(): Promise<RuntimeModel[]>;
  run(
    request: RunRequest,
    sink: RuntimeEventSink,
    signal: AbortSignal,
  ): Promise<RunResult>;
}
