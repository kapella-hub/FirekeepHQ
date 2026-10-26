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
import type { KeepHighlights, KeepItem, KeepStatus } from "./keep.js";

export const STUDIO_INVOKE_CHANNEL = "studio:invoke";
export const STUDIO_EVENT_CHANNEL = "studio:event";

export interface StudioUpdateState {
  readonly phase: "disabled" | "idle" | "checking" | "current" | "available" | "downloading" | "ready" | "error";
  readonly currentVersion: string;
  readonly availableVersion: string | null;
  readonly progressPercent: number | null;
  readonly automatic: boolean;
  readonly detail: string;
}

/** Fixed destinations: the renderer names a target, never a URL. */
export const FEEDBACK_URLS = {
  discussion: "https://github.com/kapella-hub/FirekeepHQ/discussions/15",
  issue: "https://github.com/kapella-hub/FirekeepHQ/issues/new/choose",
} as const;
export type FeedbackTarget = keyof typeof FEEDBACK_URLS;

export type StudioAction =
  | { type: "bootstrap" }
  | { type: "dashboard.open" }
  | { type: "feedback.open"; target: FeedbackTarget }
  | { type: "feedback.dismiss" }
  | { type: "clipboard.read" }
  | { type: "clipboard.write"; text: string }
  | { type: "decision.load"; url: string }
  | { type: "decision.submit"; url: string; answers: DecisionAnswers }
  | { type: "message.send"; text: string }
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
  /** Only a path already in `recentWorkspaces`; any other path needs the native dialog (workspace.choose). */
  | { type: "workspace.set"; path: string }
  | { type: "workspace.detectTest" }
  | { type: "settings.keepPanel"; open: boolean }
  | { type: "keep.status" }
  | { type: "keep.retry" }
  | { type: "keep.search"; query: string; kind: "memory" | "skill" }
  | { type: "keep.skills" }
  | { type: "keep.team" }
  | { type: "keep.highlights" }
  | { type: "keep.feedback"; memoryIds: string[]; useful: boolean; comment?: string }
  | { type: "keep.receipt.retry"; runId: string }
  | { type: "session.new" }
  | { type: "session.resume"; sessionId: string }
  | { type: "session.rename"; name: string }
  | { type: "session.update"; sessionId: string; name: string; color: SessionColor }
  | { type: "mission.run" }
  /** createMission + addMissionCheck per check + runMission; the native confirmation still gates execution. */
  | { type: "mission.quick"; goal: string; checks: string[] }
  | { type: "mission.continue" }
  | { type: "mission.repair"; note: string }
  | { type: "mission.complete"; taskResult: "success" | "partial" | "failure"; note?: string }
  | { type: "mission.cancel" }
  | { type: "theme.set"; theme: ThemeMode }
  | { type: "update.check" }
  | { type: "update.install" }
  | { type: "run.cancel"; runId?: string };

export interface BootstrapResult {
  readonly type: "bootstrap";
  readonly appName: "Firekeep Studio";
  readonly version: string;
  readonly dashboardAvailable: boolean;
  readonly update: StudioUpdateState;
  readonly snapshot: StudioSnapshot;
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly events: readonly RuntimeEvent[];
  readonly sessions: readonly StudioSessionSummary[];
}

export type StudioActionResult =
  | BootstrapResult
  | { readonly type: "error"; readonly message: string }
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
  | { readonly type: "update"; readonly state: StudioUpdateState }
  | { readonly type: "keep-status"; readonly status: KeepStatus }
  | { readonly type: "keep-items"; readonly items: readonly KeepItem[] }
  | { readonly type: "keep-team"; readonly markdown: string }
  | { readonly type: "keep-highlights"; readonly highlights: KeepHighlights }
  | { readonly type: "keep-feedback" }
  | { readonly type: "test-command"; readonly command: string | null };

export type StudioPushEvent =
  | { readonly type: "runtime.event"; readonly event: RuntimeEvent }
  | { readonly type: "snapshot"; readonly snapshot: StudioSnapshot }
  | { readonly type: "decision.available"; readonly board: DecisionBoardDocument }
  | { readonly type: "update"; readonly state: StudioUpdateState }
  | { readonly type: "sessions"; readonly sessions: readonly StudioSessionSummary[] }
  | { readonly type: "keep-status"; readonly status: KeepStatus };

export interface StudioBridge {
  invoke(action: StudioAction): Promise<StudioActionResult>;
  subscribe(listener: (event: StudioPushEvent) => void): () => void;
}
