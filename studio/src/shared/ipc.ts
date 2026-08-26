import type { CommandCompletion, CommandResult } from "../core/slash-commands.js";
import type { SessionColor, StudioSessionSummary } from "../core/session-store.js";
import type { ReviewerMode, RuntimeDiagnostic, StudioSnapshot, ThemeMode } from "../core/studio-service.js";
import type {
  LoginMethod,
  LoginResult,
  RuntimeDescriptor,
  RuntimeEffort,
  RuntimeEvent,
  RuntimeModel,
  RuntimePermissionMode,
} from "../core/runtime.js";
import type { DecisionAnswers, DecisionBoardDocument } from "./decision-board.js";

export const STUDIO_INVOKE_CHANNEL = "studio:invoke";
export const STUDIO_EVENT_CHANNEL = "studio:event";

export interface VoiceInputOutcome {
  readonly state: "complete" | "empty" | "cancelled" | "unavailable";
  readonly text: string;
  readonly detail: string;
}

export type StudioAction =
  | { type: "bootstrap" }
  | { type: "dashboard.open" }
  | { type: "clipboard.read" }
  | { type: "clipboard.write"; text: string }
  | { type: "decision.load"; url: string }
  | { type: "decision.submit"; url: string; answers: DecisionAnswers }
  | { type: "message.send"; text: string }
  | { type: "message.sendTo"; runtimeId: string; text: string }
  | { type: "command.execute"; input: string }
  | { type: "command.complete"; input: string }
  | { type: "runtime.probe"; runtimeId?: string }
  | { type: "runtime.models"; runtimeId: string }
  | { type: "runtime.login"; runtimeId: string; method?: LoginMethod; secret?: string }
  | { type: "runtime.logout"; runtimeId: string }
  | { type: "primary.set"; runtimeId: string }
  | { type: "reviewer.add"; runtimeId: string }
  | { type: "reviewer.remove"; runtimeId: string }
  | { type: "reviewer.mode"; mode: ReviewerMode }
  | { type: "review.run"; runtimeId?: string; focus?: string }
  | { type: "model.set"; runtimeId: string; modelId: string }
  | { type: "effort.set"; runtimeId: string; effort: RuntimeEffort | null }
  | { type: "permission.set"; runtimeId: string; mode: RuntimePermissionMode }
  | { type: "approval.resolve"; approvalId: string; decision: string }
  | { type: "workspace.choose" }
  | { type: "session.new" }
  | { type: "session.resume"; sessionId: string }
  | { type: "session.rename"; name: string }
  | { type: "session.update"; sessionId: string; name: string; color: SessionColor }
  | { type: "mission.run" }
  | { type: "mission.continue" }
  | { type: "mission.repair"; note: string }
  | { type: "mission.complete"; taskResult: "success" | "partial" | "failure"; note?: string }
  | { type: "mission.cancel" }
  | { type: "theme.set"; theme: ThemeMode }
  | { type: "voice.set"; enabled: boolean }
  | { type: "voice.input.start"; language?: string }
  | { type: "voice.input.stop" }
  | { type: "run.cancel"; runId?: string };

export interface BootstrapResult {
  readonly type: "bootstrap";
  readonly appName: "Firekeep Studio";
  readonly version: string;
  readonly dashboardAvailable: boolean;
  readonly snapshot: StudioSnapshot;
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly events: readonly RuntimeEvent[];
  readonly sessions: readonly StudioSessionSummary[];
}

export type StudioActionResult =
  | BootstrapResult
  | { readonly type: "state"; readonly snapshot: StudioSnapshot; readonly sessions?: readonly StudioSessionSummary[]; readonly events?: readonly RuntimeEvent[] }
  | { readonly type: "command"; readonly result: CommandResult; readonly snapshot: StudioSnapshot; readonly sessions: readonly StudioSessionSummary[]; readonly events: readonly RuntimeEvent[] }
  | { readonly type: "completions"; readonly items: readonly CommandCompletion[] }
  | { readonly type: "diagnostics"; readonly items: readonly RuntimeDiagnostic[] }
  | { readonly type: "models"; readonly runtimeId: string; readonly items: readonly RuntimeModel[] }
  | { readonly type: "login"; readonly runtimeId: string; readonly result: LoginResult }
  | { readonly type: "approval"; readonly accepted: boolean }
  | { readonly type: "clipboard-read"; readonly text: string }
  | { readonly type: "clipboard-written" }
  | { readonly type: "decision"; readonly board: DecisionBoardDocument }
  | { readonly type: "decision-submitted"; readonly url: string }
  | ({ readonly type: "voice-input" } & VoiceInputOutcome);

export type StudioPushEvent =
  | { readonly type: "runtime.event"; readonly event: RuntimeEvent }
  | { readonly type: "snapshot"; readonly snapshot: StudioSnapshot }
  | { readonly type: "decision.available"; readonly board: DecisionBoardDocument }
  | { readonly type: "sessions"; readonly sessions: readonly StudioSessionSummary[] };

export interface StudioBridge {
  invoke(action: StudioAction): Promise<StudioActionResult>;
  subscribe(listener: (event: StudioPushEvent) => void): () => void;
}
