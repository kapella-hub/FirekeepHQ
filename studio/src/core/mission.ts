import type { RuntimeEffort, RuntimePermissionMode } from "./runtime.js";

export const MISSION_PHASES = [
  "draft",
  "running",
  "verifying",
  "repairing",
  "reviewing",
  "paused",
  "awaiting-approval",
  "succeeded",
  "partial",
  "failed",
  "cancelled",
] as const;

export type MissionPhase = (typeof MISSION_PHASES)[number];
export type MissionNextAction = "primary" | "verify" | "review" | "finish";
export type MissionTaskResult = "success" | "partial" | "failure";
export type MissionTaskResultSource = "verified" | "human_confirmed";

export interface MissionCheck {
  readonly id: string;
  readonly name: string;
  readonly command: string;
  readonly timeoutMs: number;
}

export interface MissionCheckExecution {
  readonly exitCode: number | null;
  readonly signal: string | null;
  readonly stdout: string;
  readonly stderr: string;
  readonly timedOut: boolean;
  readonly truncated: boolean;
  readonly durationMs: number;
}

export interface MissionCheckRunner {
  run(check: MissionCheck, cwd: string, signal: AbortSignal): Promise<MissionCheckExecution>;
}

export interface MissionCheckReceipt extends MissionCheckExecution {
  readonly id: string;
  readonly checkId: string;
  readonly name: string;
  readonly command: string;
  readonly attempt: number;
  readonly startedAt: string;
  readonly completedAt: string;
  readonly passed: boolean;
}

export interface MissionReviewReceipt {
  readonly id: string;
  readonly runtimeId: string;
  readonly attempt: number;
  readonly completedAt: string;
  readonly status: "completed" | "failed";
  readonly text: string;
}

export interface MissionOutcomeReceipt {
  readonly taskResult: MissionTaskResult;
  readonly taskResultSource: MissionTaskResultSource;
  readonly completedAt: string;
  readonly note?: string;
  readonly checkReceiptIds: readonly string[];
  readonly reviewReceiptIds: readonly string[];
}

export interface MissionRuntimeSettings {
  readonly model?: string;
  readonly effort?: RuntimeEffort;
  readonly permissionMode: RuntimePermissionMode;
}

export interface MissionSnapshot {
  readonly version: 1;
  readonly id: string;
  readonly goal: string;
  readonly phase: MissionPhase;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly startedAt: string | null;
  readonly completedAt: string | null;
  readonly workspacePath: string | null;
  readonly primaryRuntimeId: string | null;
  readonly reviewerRuntimeIds: readonly string[];
  readonly runtimeSettings: Readonly<Record<string, MissionRuntimeSettings>>;
  readonly checks: readonly MissionCheck[];
  readonly tokenBudget: number | null;
  readonly maxRepairAttempts: number;
  readonly attempt: number;
  readonly nextAction: MissionNextAction | null;
  readonly nextReviewerIndex: number;
  readonly measuredTokensAtStart: number;
  readonly measuredTokens: number;
  readonly lastPrimaryText: string;
  readonly manualRepairNote: string | null;
  readonly verificationPassed: boolean | null;
  readonly checkReceipts: readonly MissionCheckReceipt[];
  readonly reviewReceipts: readonly MissionReviewReceipt[];
  readonly outcome: MissionOutcomeReceipt | null;
  readonly blockReason: string | null;
  readonly executionApprovedAt: string | null;
}

export const DEFAULT_MISSION_TOKEN_BUDGET = 50_000;
export const DEFAULT_MISSION_REPAIR_ATTEMPTS = 1;
export const MAX_MISSION_CHECKS = 20;
export const MAX_MISSION_REPAIR_ATTEMPTS = 3;
export const DEFAULT_MISSION_CHECK_TIMEOUT_MS = 10 * 60_000;

export function isMissionTerminal(phase: MissionPhase): boolean {
  return phase === "succeeded" || phase === "partial" || phase === "failed" || phase === "cancelled";
}

export function missionOutcome(
  mission: MissionSnapshot,
  taskResult: MissionTaskResult,
  taskResultSource: MissionTaskResultSource,
  completedAt: string,
  note?: string,
): MissionOutcomeReceipt {
  return {
    taskResult,
    taskResultSource,
    completedAt,
    ...(note?.trim() ? { note: note.trim().slice(0, 4_000) } : {}),
    checkReceiptIds: mission.checkReceipts.map((receipt) => receipt.id),
    reviewReceiptIds: mission.reviewReceipts.map((receipt) => receipt.id),
  };
}

export function parseMissionSnapshot(value: unknown): MissionSnapshot | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Partial<MissionSnapshot>;
  if (source.version !== 1 || typeof source.id !== "string" || typeof source.goal !== "string") return null;
  if (!MISSION_PHASES.includes(source.phase as MissionPhase)) return null;
  if (typeof source.createdAt !== "string" || typeof source.updatedAt !== "string") return null;
  if (!Array.isArray(source.checks) || !Array.isArray(source.reviewerRuntimeIds)) return null;
  if (!Array.isArray(source.checkReceipts) || !Array.isArray(source.reviewReceipts)) return null;
  const checks = source.checks.filter(isMissionCheck);
  const checkReceipts = source.checkReceipts.filter(isCheckReceipt);
  const reviewReceipts = source.reviewReceipts.filter(isReviewReceipt);
  if (checks.length !== source.checks.length || checkReceipts.length !== source.checkReceipts.length || reviewReceipts.length !== source.reviewReceipts.length) return null;
  const outcome = source.outcome === null || source.outcome === undefined ? null : parseOutcome(source.outcome);
  if (source.outcome && !outcome) return null;
  const nextAction = source.nextAction === null || source.nextAction === undefined
    ? null
    : ["primary", "verify", "review", "finish"].includes(source.nextAction) ? source.nextAction : null;
  if (source.nextAction && !nextAction) return null;
  return {
    version: 1,
    id: source.id,
    goal: source.goal,
    phase: source.phase as MissionPhase,
    createdAt: source.createdAt,
    updatedAt: source.updatedAt,
    startedAt: typeof source.startedAt === "string" ? source.startedAt : null,
    completedAt: typeof source.completedAt === "string" ? source.completedAt : null,
    workspacePath: typeof source.workspacePath === "string" ? source.workspacePath : null,
    primaryRuntimeId: typeof source.primaryRuntimeId === "string" ? source.primaryRuntimeId : null,
    reviewerRuntimeIds: source.reviewerRuntimeIds.filter((item): item is string => typeof item === "string"),
    runtimeSettings: parseRuntimeSettings(source.runtimeSettings),
    checks,
    tokenBudget: positiveInteger(source.tokenBudget) ? source.tokenBudget as number : null,
    maxRepairAttempts: boundedInteger(source.maxRepairAttempts, 0, MAX_MISSION_REPAIR_ATTEMPTS) ? source.maxRepairAttempts as number : DEFAULT_MISSION_REPAIR_ATTEMPTS,
    attempt: boundedInteger(source.attempt, 0, MAX_MISSION_REPAIR_ATTEMPTS + 1) ? source.attempt as number : 0,
    nextAction: nextAction as MissionNextAction | null,
    nextReviewerIndex: nonnegativeInteger(source.nextReviewerIndex) ? source.nextReviewerIndex as number : 0,
    measuredTokensAtStart: nonnegativeInteger(source.measuredTokensAtStart) ? source.measuredTokensAtStart as number : 0,
    measuredTokens: nonnegativeInteger(source.measuredTokens) ? source.measuredTokens as number : 0,
    lastPrimaryText: typeof source.lastPrimaryText === "string" ? source.lastPrimaryText : "",
    manualRepairNote: typeof source.manualRepairNote === "string" ? source.manualRepairNote : null,
    verificationPassed: typeof source.verificationPassed === "boolean" ? source.verificationPassed : null,
    checkReceipts,
    reviewReceipts,
    outcome,
    blockReason: typeof source.blockReason === "string" ? source.blockReason : null,
    executionApprovedAt: typeof source.executionApprovedAt === "string" ? source.executionApprovedAt : null,
  };
}

function isMissionCheck(value: unknown): value is MissionCheck {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Partial<MissionCheck>;
  return typeof item.id === "string" && typeof item.name === "string" && typeof item.command === "string" && positiveInteger(item.timeoutMs);
}

function isCheckReceipt(value: unknown): value is MissionCheckReceipt {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Partial<MissionCheckReceipt>;
  return typeof item.id === "string" && typeof item.checkId === "string" && typeof item.name === "string"
    && typeof item.command === "string" && positiveInteger(item.attempt) && typeof item.startedAt === "string"
    && typeof item.completedAt === "string" && typeof item.passed === "boolean" && validExecution(item);
}

function isReviewReceipt(value: unknown): value is MissionReviewReceipt {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Partial<MissionReviewReceipt>;
  return typeof item.id === "string" && typeof item.runtimeId === "string" && positiveInteger(item.attempt)
    && typeof item.completedAt === "string" && (item.status === "completed" || item.status === "failed") && typeof item.text === "string";
}

function parseOutcome(value: unknown): MissionOutcomeReceipt | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Partial<MissionOutcomeReceipt>;
  if (!item.taskResult || !["success", "partial", "failure"].includes(item.taskResult)) return null;
  if (!item.taskResultSource || !["verified", "human_confirmed"].includes(item.taskResultSource)) return null;
  if (typeof item.completedAt !== "string" || !Array.isArray(item.checkReceiptIds) || !Array.isArray(item.reviewReceiptIds)) return null;
  return {
    taskResult: item.taskResult,
    taskResultSource: item.taskResultSource,
    completedAt: item.completedAt,
    ...(typeof item.note === "string" ? { note: item.note } : {}),
    checkReceiptIds: item.checkReceiptIds.filter((entry): entry is string => typeof entry === "string"),
    reviewReceiptIds: item.reviewReceiptIds.filter((entry): entry is string => typeof entry === "string"),
  };
}

function parseRuntimeSettings(value: unknown): Record<string, MissionRuntimeSettings> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const result: Record<string, MissionRuntimeSettings> = {};
  for (const [runtimeId, raw] of Object.entries(value)) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const item = raw as Partial<MissionRuntimeSettings>;
    if (item.permissionMode !== "safe" && item.permissionMode !== "standard" && item.permissionMode !== "unrestricted") continue;
    const effort = item.effort && ["low", "medium", "high", "xhigh", "max"].includes(item.effort) ? item.effort : undefined;
    result[runtimeId] = {
      permissionMode: item.permissionMode,
      ...(typeof item.model === "string" ? { model: item.model } : {}),
      ...(effort ? { effort } : {}),
    };
  }
  return result;
}

function validExecution(value: Partial<MissionCheckExecution>): boolean {
  return (value.exitCode === null || Number.isInteger(value.exitCode))
    && (value.signal === null || typeof value.signal === "string")
    && typeof value.stdout === "string" && typeof value.stderr === "string"
    && typeof value.timedOut === "boolean" && typeof value.truncated === "boolean"
    && typeof value.durationMs === "number" && Number.isFinite(value.durationMs) && value.durationMs >= 0;
}

function positiveInteger(value: unknown): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function nonnegativeInteger(value: unknown): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function boundedInteger(value: unknown, minimum: number, maximum: number): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}
