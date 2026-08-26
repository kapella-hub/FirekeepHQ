import type { RuntimeRegistry } from "./runtime-registry.js";
import { DEFAULT_SESSION_COLOR, type SessionMetadataUpdate, type SessionStore, type StudioSessionSummary } from "./session-store.js";
import type { SettingsStore } from "./settings-store.js";
import {
  DEFAULT_MISSION_CHECK_TIMEOUT_MS,
  DEFAULT_MISSION_REPAIR_ATTEMPTS,
  DEFAULT_MISSION_TOKEN_BUDGET,
  MAX_MISSION_CHECKS,
  MAX_MISSION_REPAIR_ATTEMPTS,
  isMissionTerminal,
  missionOutcome,
  type MissionCheck,
  type MissionCheckExecution,
  type MissionCheckReceipt,
  type MissionCheckRunner,
  type MissionNextAction,
  type MissionReviewReceipt,
  type MissionRuntimeSettings,
  type MissionSnapshot,
  type MissionTaskResult,
} from "./mission.js";
import type {
  LoginRequest,
  LoginResult,
  RuntimeAuthStatus,
  RuntimeApprovalPrompt,
  RuntimeConnection,
  RuntimeEffort,
  RuntimeEvent,
  RuntimeEventPayload,
  RuntimeModel,
  RuntimePermissionMode,
  RuntimeUsage,
  RunRequest,
  RunResult,
} from "./runtime.js";
import { withStudioVisualHint } from "./visuals.js";
import { RUNTIME_EFFORTS } from "./runtime.js";
import { cachedUsageTokens, claudeUsageSample, freshUsageTokens, sumRuntimeUsage, usageTokens } from "./usage.js";

export type ReviewerMode = "off" | "manual" | "after-turn";
export type ThemeMode = "system" | "dark" | "light";

export interface StudioPersistedState {
  readonly version: 1;
  readonly activeSessionId: string;
  readonly workspacePath: string | null;
  readonly primaryRuntimeId: string | null;
  readonly reviewerRuntimeIds: readonly string[];
  readonly reviewerMode: ReviewerMode;
  readonly selectedModels: Readonly<Record<string, string>>;
  readonly selectedEfforts: Readonly<Record<string, RuntimeEffort>>;
  readonly permissionModes: Readonly<Record<string, RuntimePermissionMode>>;
  readonly nativeSessionIds: Readonly<Record<string, string>>;
  readonly tokenBudget: number | null;
  readonly voiceEnabled: boolean;
  readonly theme: ThemeMode;
}

export interface RuntimeUsageSummary {
  readonly tokens: number;
  readonly freshTokens: number;
  readonly cachedTokens: number;
  readonly costUsd: number;
  readonly runs: number;
}

export interface SessionUsageSummary extends RuntimeUsageSummary {
  readonly totalRuns: number;
  readonly measuredRuns: number;
  readonly byRuntime: Readonly<Record<string, RuntimeUsageSummary>>;
}

export interface StudioSnapshot extends StudioPersistedState {
  readonly activeRunId: string | null;
  readonly eventCount: number;
  readonly usage: SessionUsageSummary;
  readonly mission: MissionSnapshot | null;
}

export interface RuntimeDiagnostic {
  readonly runtimeId: string;
  readonly connection: RuntimeConnection;
  readonly auth: RuntimeAuthStatus;
}

interface StudioServiceOptions {
  readonly runtimes: RuntimeRegistry;
  readonly settings: SettingsStore<StudioPersistedState>;
  readonly idFactory?: (prefix: string) => string;
  readonly now?: () => string;
  readonly cwd?: () => string;
  readonly sessions?: SessionStore;
  readonly missionChecks?: MissionCheckRunner;
  readonly confirmMission?: (summary: string) => Promise<boolean>;
}

const DEFAULT_PERMISSION: RuntimePermissionMode = "standard";
const REVIEW_PACKET_LIMIT = 40_000;
const MISSION_EVIDENCE_LIMIT = 32_000;

export class StudioService {
  readonly runtimes: RuntimeRegistry;
  readonly #settings: SettingsStore<StudioPersistedState>;
  readonly #idFactory: (prefix: string) => string;
  readonly #now: () => string;
  readonly #cwd: () => string;
  readonly #sessions: SessionStore | undefined;
  readonly #missionChecks: MissionCheckRunner;
  readonly #confirmMission: (summary: string) => Promise<boolean>;
  readonly #events: RuntimeEvent[] = [];
  readonly #listeners = new Set<(event: RuntimeEvent) => void>();
  #state: StudioPersistedState | null = null;
  #mission: MissionSnapshot | null = null;
  #missionLaunchPending = false;
  #activeMission: { readonly id: string; readonly controller: AbortController } | null = null;
  #activeRun: {
    readonly id: string;
    readonly controller: AbortController;
    readonly settled: Promise<void>;
    readonly settle: () => void;
  } | null = null;
  readonly #pendingApprovals = new Map<string, {
    readonly options: readonly string[];
    readonly resolve: (decision: string) => void;
    readonly reject: (error: Error) => void;
  }>();

  constructor(options: StudioServiceOptions) {
    this.runtimes = options.runtimes;
    this.#settings = options.settings;
    this.#idFactory = options.idFactory ?? ((prefix) => `${prefix}-${crypto.randomUUID()}`);
    this.#now = options.now ?? (() => new Date().toISOString());
    this.#cwd = options.cwd ?? (() => process.cwd());
    this.#sessions = options.sessions;
    this.#missionChecks = options.missionChecks ?? {
      run: async () => { throw new Error("local mission verification is unavailable"); },
    };
    this.#confirmMission = options.confirmMission ?? (async () => false);
  }

  async initialize(): Promise<void> {
    if (this.#state) return;
    const stored = await this.#settings.load();
    this.#state = this.#normalizeState(stored);
    if (this.#sessions) {
      await this.#sessions.ensure(this.#state.activeSessionId, this.#now());
      const summary = (await this.#sessions.list()).find((item) => item.id === this.#state?.activeSessionId);
      if (summary) {
        this.#replaceState({ nativeSessionIds: { ...summary.nativeSessionIds } });
        this.#mission = summary.mission ? structuredClone(summary.mission) : null;
        if (this.#mission && missionWasInterrupted(this.#mission.phase)) {
          const timestamp = this.#now();
          this.#mission = {
            ...this.#mission,
            phase: "paused",
            updatedAt: timestamp,
            blockReason: "Studio closed while this mission step was active. Inspect the workspace, then continue to retry that step.",
          };
          await this.#sessions.setMission(this.#state.activeSessionId, this.#mission);
        }
      }
      this.#events.push(...await this.#sessions.load(this.#state.activeSessionId));
    }
    await this.#persist();
  }

  snapshot(): StudioSnapshot {
    const state = this.#requireState();
    return structuredClone({
      ...state,
      activeRunId: this.#activeRun?.id ?? null,
      eventCount: this.#events.length,
      usage: this.usageSummary(),
      mission: this.#mission ? structuredClone(this.#mission) : null,
    });
  }

  events(): readonly RuntimeEvent[] {
    return structuredClone(this.#events);
  }

  subscribe(listener: (event: RuntimeEvent) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  async setPrimary(runtimeId: string): Promise<void> {
    if (this.#missionLaunchPending) throw new Error("cannot change primary while mission approval is open");
    this.runtimes.requireCapability(runtimeId, "chat");
    this.#replaceState({ primaryRuntimeId: runtimeId });
    await this.#persist();
  }

  async setWorkspace(path: string | null): Promise<void> {
    if (this.#activeRun) throw new Error("cannot change workspace while a run is active");
    if (this.#missionLaunchPending) throw new Error("cannot change workspace while mission approval is open");
    const normalized = path?.trim() || null;
    if (normalized && normalized.length > 4_096) throw new Error("workspace path is too long");
    this.#replaceState({ workspacePath: normalized });
    await this.#persist();
  }

  async addReviewer(runtimeId: string): Promise<void> {
    this.runtimes.requireCapability(runtimeId, "review");
    const state = this.#requireState();
    if (state.reviewerRuntimeIds.includes(runtimeId)) return;
    this.#replaceState({ reviewerRuntimeIds: [...state.reviewerRuntimeIds, runtimeId] });
    await this.#persist();
  }

  async removeReviewer(runtimeId: string): Promise<void> {
    const state = this.#requireState();
    this.#replaceState({
      reviewerRuntimeIds: state.reviewerRuntimeIds.filter((id) => id !== runtimeId),
    });
    await this.#persist();
  }

  async clearReviewers(): Promise<void> {
    this.#replaceState({ reviewerRuntimeIds: [] });
    await this.#persist();
  }

  async setReviewerMode(mode: ReviewerMode): Promise<void> {
    this.#replaceState({ reviewerMode: mode });
    await this.#persist();
  }

  async setModel(runtimeId: string, modelId: string): Promise<void> {
    this.runtimes.requireCapability(runtimeId, "models");
    const state = this.#requireState();
    const selectedModels = { ...state.selectedModels };
    const selectedEfforts = { ...state.selectedEfforts };
    if (modelId) selectedModels[runtimeId] = modelId;
    else delete selectedModels[runtimeId];
    delete selectedEfforts[runtimeId];
    this.#replaceState({ selectedModels, selectedEfforts });
    await this.#persist();
  }

  async listEfforts(runtimeId: string): Promise<RuntimeEffort[]> {
    const state = this.#requireState();
    const models = await this.listModels(runtimeId);
    const selectedModel = state.selectedModels[runtimeId];
    const active = selectedModel
      ? models.find((model) => model.id === selectedModel)
      : models.find((model) => model.isDefault);
    const reported = active?.efforts ?? models.flatMap((model) => model.efforts ?? []);
    return RUNTIME_EFFORTS.filter((effort) => reported.includes(effort));
  }

  async setEffort(runtimeId: string, effort: RuntimeEffort | null): Promise<void> {
    this.runtimes.require(runtimeId);
    const state = this.#requireState();
    const selectedEfforts = { ...state.selectedEfforts };
    if (effort) {
      const available = await this.listEfforts(runtimeId);
      if (!available.includes(effort)) {
        throw new Error(available.length
          ? `${runtimeId} does not advertise ${effort} reasoning for the selected model; available: ${available.join(", ")}`
          : `${runtimeId} does not advertise configurable reasoning for the selected model`);
      }
      selectedEfforts[runtimeId] = effort;
    } else delete selectedEfforts[runtimeId];
    this.#replaceState({ selectedEfforts });
    await this.#persist();
  }

  async setPermissionMode(runtimeId: string, mode: RuntimePermissionMode): Promise<void> {
    this.runtimes.require(runtimeId);
    const state = this.#requireState();
    this.#replaceState({ permissionModes: { ...state.permissionModes, [runtimeId]: mode } });
    await this.#persist();
  }

  async setTokenBudget(tokens: number | null): Promise<void> {
    if (tokens !== null && (!Number.isSafeInteger(tokens) || tokens < 1)) {
      throw new Error("token budget must be a positive whole number");
    }
    this.#replaceState({ tokenBudget: tokens });
    await this.#persist();
  }

  usageSummary(): SessionUsageSummary {
    const latestByRun = new Map<string, { readonly runtimeId: string; readonly usage: RuntimeUsage }>();
    const legacyClaudeByRun = new Map<string, Map<string, RuntimeUsage>>();
    const runIds = new Set<string>();
    for (const event of this.#events) {
      if (event.payload.kind === "run.started") runIds.add(event.runId);
      if (event.payload.kind === "usage.updated") latestByRun.set(event.runId, { runtimeId: event.runtimeId, usage: event.payload.usage });
      if (event.runtimeId === "claude") {
        const sample = claudeUsageSample(event.raw);
        if (sample) {
          const messages = legacyClaudeByRun.get(event.runId) ?? new Map<string, RuntimeUsage>();
          messages.set(sample.messageId, sample.usage);
          legacyClaudeByRun.set(event.runId, messages);
        }
      }
    }
    for (const [runId, messages] of legacyClaudeByRun) {
      if (!latestByRun.has(runId)) latestByRun.set(runId, { runtimeId: "claude", usage: sumRuntimeUsage(messages.values()) });
    }
    const byRuntime: Record<string, RuntimeUsageSummary> = {};
    for (const { runtimeId, usage } of latestByRun.values()) {
      const current = byRuntime[runtimeId] ?? { tokens: 0, freshTokens: 0, cachedTokens: 0, costUsd: 0, runs: 0 };
      byRuntime[runtimeId] = {
        tokens: current.tokens + usageTokens(usage),
        freshTokens: current.freshTokens + freshUsageTokens(usage),
        cachedTokens: current.cachedTokens + cachedUsageTokens(usage),
        costUsd: current.costUsd + (usage.costUsd ?? 0),
        runs: current.runs + 1,
      };
    }
    const totals = Object.values(byRuntime).reduce<RuntimeUsageSummary>((sum, item) => ({
      tokens: sum.tokens + item.tokens,
      freshTokens: sum.freshTokens + item.freshTokens,
      cachedTokens: sum.cachedTokens + item.cachedTokens,
      costUsd: sum.costUsd + item.costUsd,
      runs: sum.runs + item.runs,
    }), { tokens: 0, freshTokens: 0, cachedTokens: 0, costUsd: 0, runs: 0 });
    return { ...totals, totalRuns: runIds.size, measuredRuns: latestByRun.size, byRuntime };
  }

  async setVoice(enabled: boolean): Promise<void> {
    this.#replaceState({ voiceEnabled: enabled });
    await this.#persist();
  }

  async setTheme(theme: ThemeMode): Promise<void> {
    this.#replaceState({ theme });
    await this.#persist();
  }

  async startNewSession(): Promise<void> {
    if (this.#activeRun) throw new Error("cannot change sessions while a run is active");
    if (this.#activeMission) throw new Error("cannot change sessions while a mission is active");
    if (this.#missionLaunchPending) throw new Error("cannot change sessions while mission approval is open");
    const state = this.#requireState();
    this.#events.length = 0;
    this.#mission = null;
    this.#state = {
      ...state,
      activeSessionId: this.#idFactory("session"),
      nativeSessionIds: {},
    };
    await this.#sessions?.ensure(this.#state.activeSessionId, this.#now());
    await this.#persist();
  }

  async listSessions(): Promise<StudioSessionSummary[]> {
    if (this.#sessions) return this.#sessions.list();
    const state = this.#requireState();
    return [{
      id: state.activeSessionId,
      name: "Current session",
      color: DEFAULT_SESSION_COLOR,
      createdAt: this.#now(),
      updatedAt: this.#now(),
      eventCount: this.#events.length,
      nativeSessionIds: state.nativeSessionIds,
      ...(this.#mission ? { mission: structuredClone(this.#mission) } : {}),
    }];
  }

  async renameSession(name: string): Promise<void> {
    await this.updateSession(this.#requireState().activeSessionId, { name });
  }

  async updateSession(sessionId: string, update: SessionMetadataUpdate): Promise<void> {
    if (!this.#sessions) throw new Error("persistent sessions are unavailable");
    await this.#sessions.updateMetadata(sessionId, update);
  }

  async deleteSession(sessionId: string, confirmation: string): Promise<void> {
    if (this.#activeRun) throw new Error("cannot delete a session while a run is active");
    if (!this.#sessions) throw new Error("persistent sessions are unavailable");
    if (confirmation !== sessionId) throw new Error("session deletion requires --confirm with the exact session id");
    if (sessionId === this.#requireState().activeSessionId) await this.startNewSession();
    await this.#sessions.remove(sessionId);
  }

  async resumeSession(sessionId: string): Promise<void> {
    if (this.#activeRun) throw new Error("cannot change sessions while a run is active");
    if (this.#activeMission) throw new Error("cannot change sessions while a mission is active");
    if (!this.#sessions) throw new Error("persistent sessions are unavailable");
    const summary = (await this.#sessions.list()).find((item) => item.id === sessionId);
    if (!summary) throw new Error(`unknown session: ${sessionId}`);
    this.#events.length = 0;
    this.#events.push(...await this.#sessions.load(sessionId));
    this.#mission = summary.mission ? structuredClone(summary.mission) : null;
    this.#replaceState({ activeSessionId: sessionId, nativeSessionIds: { ...summary.nativeSessionIds } });
    await this.#persist();
  }

  exportSession(format: "markdown" | "json"): string {
    if (format === "json") return `${JSON.stringify({ session: this.snapshot(), events: this.#events }, null, 2)}\n`;
    const lines = [`# Firekeep Studio session`, ""];
    if (this.#mission) {
      lines.push(
        "## Mission",
        "",
        `**Goal:** ${this.#mission.goal}`,
        `**Phase:** ${this.#mission.phase}`,
        this.#mission.outcome
          ? `**Task result:** ${this.#mission.outcome.taskResult} · source: \`${this.#mission.outcome.taskResultSource}\``
          : "**Task result:** unknown",
        "",
      );
    }
    for (const event of this.#events) {
      const payload = event.payload;
      if (payload.kind === "message.completed") lines.push(`## ${payload.role === "reviewer" ? "Review" : "Assistant"} · ${event.runtimeId}`, "", payload.text, "");
      else if (payload.kind === "notice") lines.push(`> ${payload.level.toUpperCase()}: ${payload.message}${payload.detail ? ` — ${payload.detail}` : ""}`, "");
      else if (payload.kind === "diff.updated") lines.push("```diff", payload.diff, "```", "");
    }
    return `${lines.join("\n").trim()}\n`;
  }

  async probeRuntime(runtimeId: string): Promise<RuntimeDiagnostic> {
    const runtime = this.runtimes.require(runtimeId);
    const [connection, auth] = await Promise.all([runtime.probe(), runtime.authStatus()]);
    return { runtimeId, connection, auth };
  }

  async probeAll(): Promise<RuntimeDiagnostic[]> {
    return Promise.all(this.runtimes.list().map((runtime) => this.probeRuntime(runtime.descriptor.id)));
  }

  async authStatus(runtimeId: string): Promise<RuntimeAuthStatus> {
    return this.runtimes.require(runtimeId).authStatus();
  }

  async login(runtimeId: string, request: LoginRequest = {}): Promise<LoginResult> {
    return this.runtimes.require(runtimeId).login(request);
  }

  async logout(runtimeId: string): Promise<void> {
    await this.runtimes.require(runtimeId).logout();
  }

  async listModels(runtimeId: string): Promise<RuntimeModel[]> {
    return this.runtimes.requireCapability(runtimeId, "models").listModels();
  }

  mission(): MissionSnapshot | null {
    return this.#mission ? structuredClone(this.#mission) : null;
  }

  async createMission(goal: string): Promise<MissionSnapshot> {
    if (this.#activeRun || this.#activeMission) throw new Error("cannot create a mission while work is active");
    const cleanGoal = goal.trim();
    if (!cleanGoal) throw new Error("mission goal cannot be empty");
    if (cleanGoal.length > 20_000) throw new Error("mission goal is too long");
    if (this.#mission) await this.startNewSession();
    const state = this.#requireState();
    const timestamp = this.#now();
    const mission: MissionSnapshot = {
      version: 1,
      id: this.#idFactory("mission"),
      goal: cleanGoal,
      phase: "draft",
      createdAt: timestamp,
      updatedAt: timestamp,
      startedAt: null,
      completedAt: null,
      workspacePath: state.workspacePath,
      primaryRuntimeId: state.primaryRuntimeId,
      reviewerRuntimeIds: [...state.reviewerRuntimeIds],
      runtimeSettings: {},
      checks: [],
      tokenBudget: DEFAULT_MISSION_TOKEN_BUDGET,
      maxRepairAttempts: DEFAULT_MISSION_REPAIR_ATTEMPTS,
      attempt: 0,
      nextAction: null,
      nextReviewerIndex: 0,
      measuredTokensAtStart: 0,
      measuredTokens: 0,
      lastPrimaryText: "",
      manualRepairNote: null,
      verificationPassed: null,
      checkReceipts: [],
      reviewReceipts: [],
      outcome: null,
      blockReason: null,
      executionApprovedAt: null,
    };
    await this.#storeMission(mission);
    if (this.#sessions) await this.#sessions.updateMetadata(state.activeSessionId, { name: cleanGoal.slice(0, 120) });
    return this.mission() as MissionSnapshot;
  }

  async addMissionCheck(
    command: string,
    options: { readonly name?: string; readonly timeoutMs?: number } = {},
  ): Promise<MissionSnapshot> {
    const mission = this.#requireDraftMission();
    if (mission.checks.length >= MAX_MISSION_CHECKS) throw new Error(`missions support at most ${MAX_MISSION_CHECKS} checks`);
    const cleanCommand = command.trim();
    if (!cleanCommand) throw new Error("mission check command cannot be empty");
    if (cleanCommand.length > 2_000 || /[\r\n\0]/.test(cleanCommand)) throw new Error("mission check command is invalid or too long");
    const timeoutMs = options.timeoutMs ?? DEFAULT_MISSION_CHECK_TIMEOUT_MS;
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 30 * 60_000) {
      throw new Error("mission check timeout must be between 1 second and 30 minutes");
    }
    const fallbackName = cleanCommand.split(/\s+/, 1)[0] ?? `Check ${mission.checks.length + 1}`;
    const name = (options.name?.trim() || fallbackName).slice(0, 120);
    const check: MissionCheck = { id: this.#idFactory("check"), name, command: cleanCommand, timeoutMs };
    await this.#updateMission({ checks: [...mission.checks, check], executionApprovedAt: null });
    return this.mission() as MissionSnapshot;
  }

  async removeMissionCheck(checkId: string): Promise<MissionSnapshot> {
    const mission = this.#requireDraftMission();
    if (!mission.checks.some((check) => check.id === checkId)) throw new Error(`unknown mission check: ${checkId}`);
    await this.#updateMission({ checks: mission.checks.filter((check) => check.id !== checkId), executionApprovedAt: null });
    return this.mission() as MissionSnapshot;
  }

  async setMissionPrimary(runtimeId: string): Promise<MissionSnapshot> {
    this.runtimes.requireCapability(runtimeId, "chat");
    this.#requireDraftMission();
    await this.#updateMission({ primaryRuntimeId: runtimeId, executionApprovedAt: null });
    return this.mission() as MissionSnapshot;
  }

  async addMissionReviewer(runtimeId: string): Promise<MissionSnapshot> {
    this.runtimes.requireCapability(runtimeId, "review");
    const mission = this.#requireDraftMission();
    if (!mission.reviewerRuntimeIds.includes(runtimeId)) {
      await this.#updateMission({ reviewerRuntimeIds: [...mission.reviewerRuntimeIds, runtimeId], executionApprovedAt: null });
    }
    return this.mission() as MissionSnapshot;
  }

  async removeMissionReviewer(runtimeId: string): Promise<MissionSnapshot> {
    const mission = this.#requireDraftMission();
    await this.#updateMission({ reviewerRuntimeIds: mission.reviewerRuntimeIds.filter((id) => id !== runtimeId), executionApprovedAt: null });
    return this.mission() as MissionSnapshot;
  }

  async setMissionTokenBudget(tokens: number | null): Promise<MissionSnapshot> {
    const mission = this.#requireMission();
    if (mission.phase !== "draft" && mission.phase !== "paused") throw new Error("mission token guard can change only while draft or paused");
    if (tokens !== null && (!Number.isSafeInteger(tokens) || tokens < 1)) throw new Error("mission token guard must be a positive whole number");
    await this.#updateMission({ tokenBudget: tokens, executionApprovedAt: mission.phase === "draft" ? null : mission.executionApprovedAt });
    return this.mission() as MissionSnapshot;
  }

  async setMissionRepairLimit(attempts: number): Promise<MissionSnapshot> {
    this.#requireDraftMission();
    if (!Number.isSafeInteger(attempts) || attempts < 0 || attempts > MAX_MISSION_REPAIR_ATTEMPTS) {
      throw new Error(`mission repair attempts must be between 0 and ${MAX_MISSION_REPAIR_ATTEMPTS}`);
    }
    await this.#updateMission({ maxRepairAttempts: attempts, executionApprovedAt: null });
    return this.mission() as MissionSnapshot;
  }

  async runMission(): Promise<MissionSnapshot> {
    const mission = this.#requireDraftMission();
    this.#missionLaunchPending = true;
    try {
      const state = this.#requireState();
      const bound: MissionSnapshot = {
        ...mission,
        workspacePath: state.workspacePath,
        primaryRuntimeId: mission.primaryRuntimeId ?? state.primaryRuntimeId,
      };
      if (!bound.workspacePath) throw new Error("choose an explicit workspace before running a mission");
      if (!bound.primaryRuntimeId) throw new Error("choose a mission primary before running a mission");
      const runtimeSettings = captureMissionRuntimeSettings(state, [bound.primaryRuntimeId, ...bound.reviewerRuntimeIds]);
      if (mission.checks.length === 0) throw new Error("add at least one deterministic mission check before running");
      const approved = await this.#confirmMission(missionApprovalSummary({ ...bound, runtimeSettings }));
      if (!approved) return this.mission() as MissionSnapshot;
      const timestamp = this.#now();
      await this.#updateMission({
        phase: "running",
        startedAt: timestamp,
        completedAt: null,
        attempt: 1,
        nextAction: "primary",
        nextReviewerIndex: 0,
        measuredTokensAtStart: this.usageSummary().tokens,
        measuredTokens: 0,
        blockReason: null,
        executionApprovedAt: timestamp,
        workspacePath: bound.workspacePath,
        primaryRuntimeId: bound.primaryRuntimeId,
        runtimeSettings,
      });
      return await this.#driveMission();
    } finally {
      this.#missionLaunchPending = false;
    }
  }

  async continueMission(): Promise<MissionSnapshot> {
    const mission = this.#requireMission();
    if (mission.phase !== "paused" || !mission.nextAction) throw new Error("mission is not paused at a resumable step");
    await this.#updateMission({ blockReason: null });
    return this.#driveMission();
  }

  async repairMission(note: string): Promise<MissionSnapshot> {
    const mission = this.#requireMission();
    if (mission.phase !== "awaiting-approval") throw new Error("mission is not waiting for a review decision");
    if (mission.attempt - 1 >= mission.maxRepairAttempts) throw new Error("mission repair allowance is exhausted");
    const cleanNote = note.trim();
    if (!cleanNote) throw new Error("human-directed repair requires a note");
    if (cleanNote.length > 4_000) throw new Error("mission repair note is too long");
    await this.#updateMission({
      phase: "repairing",
      attempt: mission.attempt + 1,
      nextAction: "primary",
      nextReviewerIndex: 0,
      manualRepairNote: cleanNote,
      verificationPassed: null,
      outcome: null,
      blockReason: null,
      completedAt: null,
    });
    return this.#driveMission();
  }

  async completeMission(taskResult: MissionTaskResult, note?: string): Promise<MissionSnapshot> {
    const mission = this.#requireMission();
    if (mission.phase !== "awaiting-approval") throw new Error("mission is not waiting for human confirmation");
    if (!["success", "partial", "failure"].includes(taskResult)) throw new Error(`invalid mission result: ${taskResult}`);
    const timestamp = this.#now();
    await this.#updateMission({
      phase: taskResult === "success" ? "succeeded" : taskResult === "partial" ? "partial" : "failed",
      completedAt: timestamp,
      nextAction: null,
      outcome: missionOutcome(mission, taskResult, "human_confirmed", timestamp, note),
      blockReason: null,
    });
    return this.mission() as MissionSnapshot;
  }

  cancelMission(): boolean {
    const mission = this.#mission;
    if (!mission || isMissionTerminal(mission.phase)) return false;
    if (this.#activeMission) {
      this.#activeMission.controller.abort();
      this.cancel();
      return true;
    }
    const timestamp = this.#now();
    const cancelled: MissionSnapshot = {
      ...mission,
      phase: "cancelled",
      updatedAt: timestamp,
      completedAt: timestamp,
      nextAction: null,
      blockReason: null,
    };
    this.#mission = cancelled;
    void this.#sessions?.setMission(this.#requireState().activeSessionId, cancelled).catch(() => undefined);
    return true;
  }

  missionReport(): string {
    const mission = this.#requireMission();
    const lines = [
      `# Firekeep Mission`,
      "",
      `**Goal:** ${mission.goal}`,
      `**Phase:** ${mission.phase}`,
      `**Primary:** ${mission.primaryRuntimeId ?? "not selected"}`,
      `**Workspace:** ${mission.workspacePath ?? "not selected"}`,
      `**Attempts:** ${mission.attempt} (${mission.maxRepairAttempts} repairs allowed)`,
      `**Measured tokens:** ${mission.measuredTokens.toLocaleString()}${mission.tokenBudget === null ? "" : ` / ${mission.tokenBudget.toLocaleString()}`}`,
      "",
      "## Checks",
      "",
      ...mission.checks.map((check) => `- **${check.name}:** \`${check.command}\``),
      "",
      "## Evidence",
      "",
      ...mission.checkReceipts.map((receipt) => `- Attempt ${receipt.attempt} · ${receipt.name}: **${receipt.passed ? "pass" : "fail"}** (${receipt.durationMs}ms)`),
      ...mission.reviewReceipts.map((receipt) => `- Attempt ${receipt.attempt} · ${receipt.runtimeId} review: **${receipt.status}**`),
      "",
      "## Task result",
      "",
      mission.outcome
        ? `**${mission.outcome.taskResult}** · source: \`${mission.outcome.taskResultSource}\`${mission.outcome.note ? ` · ${mission.outcome.note}` : ""}`
        : "Unknown — no task result has been recorded.",
    ];
    return `${lines.join("\n").trim()}\n`;
  }

  async sendMessage(prompt: string): Promise<RunResult> {
    const text = prompt.trim();
    if (!text) throw new Error("message cannot be empty");
    const state = this.#requireState();
    if (!state.primaryRuntimeId) throw new Error("choose a primary runtime first");
    if (this.#activeRun) throw new Error("a run is already active");

    const result = await this.#run(state.primaryRuntimeId, withStudioVisualHint(text), "primary", { displayPrompt: text });
    if (this.#requireState().reviewerMode === "after-turn") {
      await this.#runConfiguredReviewers(result.finalText);
    }
    return result;
  }

  async sendMessageTo(runtimeId: string, prompt: string): Promise<RunResult> {
    const text = prompt.trim();
    if (!text) throw new Error("message cannot be empty");
    this.runtimes.requireCapability(runtimeId, "chat");
    if (this.#activeRun) throw new Error("a run is already active");
    return this.#run(runtimeId, withStudioVisualHint(text), "primary", { displayPrompt: text });
  }

  async runReview(runtimeId?: string, focus?: string): Promise<RunResult[]> {
    const state = this.#requireState();
    const reviewers = runtimeId ? [runtimeId] : [...state.reviewerRuntimeIds];
    if (reviewers.length === 0) throw new Error("no reviewers configured");
    const answer = this.#lastCompletedMessage("assistant");
    if (!answer) throw new Error("there is no primary response to review");
    const results: RunResult[] = [];
    const failures: string[] = [];
    for (const id of reviewers) {
      try { results.push(await this.#runReview(id, answer, focus)); }
      catch (error) { failures.push(`${id}: ${errorMessage(error)}`); }
    }
    if (results.length === 0 && failures.length) throw new Error(`all reviewers failed: ${failures.join("; ")}`);
    return results;
  }

  async compare(prompt: string, runtimeIds?: readonly string[]): Promise<RunResult[]> {
    const text = prompt.trim();
    if (!text) throw new Error("comparison prompt cannot be empty");
    const state = this.#requireState();
    const selected = runtimeIds?.length
      ? runtimeIds
      : [state.primaryRuntimeId, ...state.reviewerRuntimeIds].filter((value): value is string => value !== null);
    const ids = [...new Set(selected)];
    if (ids.length < 2) throw new Error("compare needs at least two distinct runtimes");
    for (const id of ids) this.runtimes.requireCapability(id, "chat");
    if (this.#activeRun) throw new Error("a run is already active");
    this.#emit(ids[0] as string, this.#idFactory("compare"), {
      kind: "message.completed",
      messageId: this.#idFactory("message"),
      role: "user",
      text,
    });
    const results: RunResult[] = [];
    const failures: string[] = [];
    for (const id of ids) {
      try { results.push(await this.#run(id, text, "compare")); }
      catch (error) { failures.push(`${id}: ${errorMessage(error)}`); }
    }
    if (results.length === 0) throw new Error(`all comparison runtimes failed: ${failures.join("; ")}`);
    return results;
  }

  async synthesize(runtimeId?: string, focus?: string): Promise<RunResult> {
    const selected = runtimeId ?? this.#requireState().primaryRuntimeId;
    if (!selected) throw new Error("choose a synthesis runtime first");
    this.runtimes.requireCapability(selected, "chat");
    const candidates: Array<{ readonly runtimeId: string; readonly text: string }> = [];
    const seen = new Set<string>();
    for (let index = this.#events.length - 1; index >= 0 && candidates.length < 8; index -= 1) {
      const event = this.#events[index];
      if (!event || event.payload.kind !== "message.completed" || event.payload.role === "user") continue;
      const key = `${event.runtimeId}:${event.payload.role}`;
      if (seen.has(key)) continue;
      seen.add(key);
      candidates.push({ runtimeId: event.runtimeId, text: event.payload.text });
    }
    if (candidates.length < 2) throw new Error("consensus needs at least two completed agent responses");
    const packet = [
      "Synthesize the independent candidate responses below into one decision-quality answer.",
      "State agreements, resolve disagreements using evidence, preserve material uncertainty, and do not claim unanimity where none exists.",
      focus ? `User focus: ${focus}` : "",
      ...candidates.reverse().map((candidate) => `## ${candidate.runtimeId}\n${candidate.text}`),
    ].filter(Boolean).join("\n\n").slice(0, REVIEW_PACKET_LIMIT);
    return this.#run(selected, packet, "compare");
  }

  async handoff(runtimeId: string, note?: string): Promise<RunResult> {
    this.runtimes.requireCapability(runtimeId, "chat");
    const transcript = this.#events
      .filter((event) => event.payload.kind === "message.completed")
      .map((event) => {
        const payload = event.payload;
        return payload.kind === "message.completed" ? `${payload.role}: ${payload.text}` : "";
      })
      .join("\n\n")
      .slice(-REVIEW_PACKET_LIMIT);
    const packet = [
      "You are receiving an explicit Firekeep Studio handoff from another runtime.",
      note ? `Handoff note: ${note}` : "",
      "Continue from the evidence below without pretending you share the original native session.",
      transcript,
    ].filter(Boolean).join("\n\n");
    await this.setPrimary(runtimeId);
    return this.#run(runtimeId, packet, "handoff");
  }

  cancel(runId?: string): boolean {
    if (!this.#activeRun) return false;
    if (runId && this.#activeRun.id !== runId) return false;
    this.#activeRun.controller.abort();
    return true;
  }

  resolveApproval(approvalId: string, decision: string): boolean {
    const pending = this.#pendingApprovals.get(approvalId);
    if (!pending || !pending.options.includes(decision)) return false;
    this.#pendingApprovals.delete(approvalId);
    pending.resolve(decision);
    return true;
  }

  async shutdown(): Promise<void> {
    const active = this.#activeRun;
    this.#activeMission?.controller.abort();
    this.cancel();
    if (active) await waitForSettlement(active.settled, 5_000);
    await this.#sessions?.flush();
  }

  async #driveMission(): Promise<MissionSnapshot> {
    const initial = this.#requireMission();
    if (this.#activeMission) throw new Error("a mission is already active");
    if (this.#activeRun) throw new Error("an agent run is already active");
    const controller = new AbortController();
    const active = { id: initial.id, controller };
    this.#activeMission = active;
    try {
      while (true) {
        const mission = this.#requireMission();
        if (!mission.nextAction || isMissionTerminal(mission.phase) || mission.phase === "awaiting-approval") break;
        if (mission.nextAction === "primary") {
          if (await this.#pauseMissionAtBudget("primary")) break;
          const primaryRuntimeId = mission.primaryRuntimeId;
          if (!primaryRuntimeId || !mission.workspacePath) throw new Error("mission lost its bound primary or workspace");
          await this.#updateMission({ phase: mission.attempt === 1 ? "running" : "repairing", blockReason: null });
          const result = await this.#run(
            primaryRuntimeId,
            mission.attempt === 1 ? primaryMissionPrompt(mission) : repairMissionPrompt(mission),
            "primary",
            {
              signal: controller.signal,
              cwd: mission.workspacePath,
              ...(mission.runtimeSettings[primaryRuntimeId] ? { runtimeSettings: mission.runtimeSettings[primaryRuntimeId] } : {}),
            },
          );
          await this.#refreshMissionUsage();
          await this.#updateMission({
            lastPrimaryText: result.finalText.slice(-REVIEW_PACKET_LIMIT),
            nextAction: "verify",
            manualRepairNote: null,
          });
          continue;
        }
        if (mission.nextAction === "verify") {
          await this.#updateMission({ phase: "verifying", blockReason: null });
          const receipts = await this.#runMissionChecks(controller.signal);
          const passed = receipts.every((receipt) => receipt.passed);
          const latest = this.#requireMission();
          if (!passed && latest.attempt <= latest.maxRepairAttempts) {
            await this.#updateMission({
              verificationPassed: false,
              phase: "repairing",
              attempt: latest.attempt + 1,
              nextAction: "primary",
              nextReviewerIndex: 0,
            });
          } else {
            await this.#updateMission({
              verificationPassed: passed,
              phase: "reviewing",
              nextAction: "review",
              nextReviewerIndex: 0,
            });
          }
          continue;
        }
        if (mission.nextAction === "review") {
          await this.#updateMission({ phase: "reviewing", blockReason: null });
          const latest = this.#requireMission();
          if (latest.nextReviewerIndex >= latest.reviewerRuntimeIds.length) {
            await this.#updateMission({ nextAction: "finish" });
            continue;
          }
          if (await this.#pauseMissionAtBudget("review")) break;
          await this.#runMissionReviewer(latest.reviewerRuntimeIds[latest.nextReviewerIndex] as string, controller.signal);
          continue;
        }
        if (mission.nextAction === "finish") {
          await this.#finishMission();
          break;
        }
      }
    } catch (error) {
      const current = this.#requireMission();
      if (controller.signal.aborted) {
        const timestamp = this.#now();
        await this.#updateMission({ phase: "cancelled", completedAt: timestamp, nextAction: null, outcome: null, blockReason: null });
      } else {
        await this.#refreshMissionUsage();
        await this.#updateMission({ phase: "paused", blockReason: errorMessage(error) });
        this.#emit(current.primaryRuntimeId ?? "studio", current.id, {
          kind: "notice",
          level: "error",
          message: "Mission paused",
          detail: errorMessage(error),
        });
      }
    } finally {
      if (this.#activeMission === active) this.#activeMission = null;
    }
    return this.mission() as MissionSnapshot;
  }

  async #runMissionChecks(signal: AbortSignal): Promise<MissionCheckReceipt[]> {
    const mission = this.#requireMission();
    const receipts: MissionCheckReceipt[] = [];
    for (const check of mission.checks) {
      if (signal.aborted) throw new Error("mission cancelled");
      const startedAt = this.#now();
      let execution: MissionCheckExecution;
      try {
        execution = await this.#missionChecks.run(check, mission.workspacePath as string, signal);
        if (signal.aborted) throw new Error("mission cancelled");
      } catch (error) {
        if (signal.aborted) throw error;
        execution = {
          exitCode: null,
          signal: null,
          stdout: "",
          stderr: errorMessage(error).slice(-16_000),
          timedOut: false,
          truncated: false,
          durationMs: 0,
        };
      }
      const receipt: MissionCheckReceipt = {
        id: this.#idFactory("receipt"),
        checkId: check.id,
        name: check.name,
        command: check.command,
        attempt: mission.attempt,
        startedAt,
        completedAt: this.#now(),
        ...execution,
        stdout: execution.stdout.slice(-16_000),
        stderr: execution.stderr.slice(-16_000),
        passed: execution.exitCode === 0 && !execution.timedOut,
      };
      receipts.push(receipt);
      const latest = this.#requireMission();
      await this.#updateMission({ checkReceipts: [...latest.checkReceipts, receipt] });
      this.#emit("studio", mission.id, {
        kind: "notice",
        level: receipt.passed ? "info" : "error",
        message: `Mission check ${receipt.passed ? "passed" : "failed"}: ${receipt.name}`,
        detail: `${receipt.command} · ${receipt.durationMs}ms`,
      });
    }
    return receipts;
  }

  async #runMissionReviewer(runtimeId: string, signal: AbortSignal): Promise<void> {
    const mission = this.#requireMission();
    let receipt: MissionReviewReceipt;
    try {
      const result = await this.#run(runtimeId, missionReviewPrompt(mission), "review", {
        signal,
        ...(mission.workspacePath ? { cwd: mission.workspacePath } : {}),
        ...(mission.runtimeSettings[runtimeId] ? { runtimeSettings: mission.runtimeSettings[runtimeId] } : {}),
      });
      receipt = {
        id: this.#idFactory("review"),
        runtimeId,
        attempt: mission.attempt,
        completedAt: this.#now(),
        status: "completed",
        text: result.finalText.slice(-REVIEW_PACKET_LIMIT),
      };
    } catch (error) {
      if (signal.aborted) throw error;
      receipt = {
        id: this.#idFactory("review"),
        runtimeId,
        attempt: mission.attempt,
        completedAt: this.#now(),
        status: "failed",
        text: errorMessage(error).slice(-4_000),
      };
    }
    await this.#refreshMissionUsage();
    const latest = this.#requireMission();
    await this.#updateMission({
      reviewReceipts: [...latest.reviewReceipts, receipt],
      nextReviewerIndex: latest.nextReviewerIndex + 1,
    });
  }

  async #finishMission(): Promise<void> {
    const mission = this.#requireMission();
    if (mission.verificationPassed === null) throw new Error("mission cannot finish without verification evidence");
    if (mission.verificationPassed && mission.reviewerRuntimeIds.length > 0) {
      await this.#updateMission({ phase: "awaiting-approval", nextAction: null, blockReason: null });
      this.#emit("studio", mission.id, {
        kind: "notice",
        level: "info",
        message: "Mission verified and reviewed",
        detail: "Task result remains unknown until you approve it or request a repair.",
      });
      return;
    }
    const timestamp = this.#now();
    const taskResult = mission.verificationPassed ? "success" : "failure";
    await this.#updateMission({
      phase: mission.verificationPassed ? "succeeded" : "failed",
      completedAt: timestamp,
      nextAction: null,
      blockReason: null,
      outcome: missionOutcome(mission, taskResult, "verified", timestamp),
    });
  }

  async #pauseMissionAtBudget(nextAction: MissionNextAction): Promise<boolean> {
    await this.#refreshMissionUsage();
    const mission = this.#requireMission();
    if (mission.tokenBudget === null || mission.measuredTokens < mission.tokenBudget) return false;
    await this.#updateMission({
      phase: "paused",
      nextAction,
      blockReason: `Mission token guard reached (${mission.measuredTokens.toLocaleString()} / ${mission.tokenBudget.toLocaleString()} measured tokens). Increase it or turn it off before continuing.`,
    });
    return true;
  }

  async #refreshMissionUsage(): Promise<void> {
    const mission = this.#requireMission();
    const measuredTokens = Math.max(0, this.usageSummary().tokens - mission.measuredTokensAtStart);
    if (measuredTokens !== mission.measuredTokens) await this.#updateMission({ measuredTokens });
  }

  async #storeMission(mission: MissionSnapshot): Promise<void> {
    this.#mission = structuredClone(mission);
    await this.#sessions?.setMission(this.#requireState().activeSessionId, this.#mission);
  }

  async #updateMission(patch: Partial<MissionSnapshot>): Promise<void> {
    const current = this.#requireMission();
    await this.#storeMission({ ...current, ...patch, updatedAt: this.#now() });
  }

  #requireMission(): MissionSnapshot {
    if (!this.#mission) throw new Error("create a mission first");
    return this.#mission;
  }

  #requireDraftMission(): MissionSnapshot {
    if (this.#missionLaunchPending) throw new Error("mission execution approval is already in progress");
    const mission = this.#requireMission();
    if (mission.phase !== "draft") throw new Error("mission configuration is locked after execution starts");
    return mission;
  }

  async #runConfiguredReviewers(primaryAnswer: string): Promise<void> {
    const reviewers = [...this.#requireState().reviewerRuntimeIds];
    for (const runtimeId of reviewers) {
      try { await this.#runReview(runtimeId, primaryAnswer); }
      catch { /* The failed reviewer already emitted an actionable notice; the primary result remains valid. */ }
    }
  }

  #runReview(runtimeId: string, answer: string, focus?: string): Promise<RunResult> {
    this.runtimes.requireCapability(runtimeId, "review");
    const prompt = [
      "Act as an independent reviewer. You are in a fresh read-only review context.",
      "Identify concrete defects, missing evidence, regressions, and unsafe assumptions. Prioritize findings by severity.",
      focus ? `Focus requested by the user: ${focus}` : "",
      "Primary response to review:",
      answer.slice(0, REVIEW_PACKET_LIMIT),
    ].filter(Boolean).join("\n\n");
    return this.#run(runtimeId, prompt, "review");
  }

  async #run(
    runtimeId: string,
    prompt: string,
    mode: RunRequest["mode"],
    options: { readonly signal?: AbortSignal; readonly cwd?: string; readonly runtimeSettings?: MissionRuntimeSettings; readonly displayPrompt?: string } = {},
  ): Promise<RunResult> {
    if (this.#activeRun) throw new Error("a run is already active");
    const runtime = this.runtimes.requireCapability(runtimeId, mode === "review" ? "review" : "chat");
    const state = this.#requireState();
    if (state.tokenBudget !== null && this.usageSummary().tokens >= state.tokenBudget) {
      throw new Error(`session token budget reached (${state.tokenBudget.toLocaleString()} tokens); use /budget set or /budget off to continue`);
    }
    const runId = this.#idFactory("run");
    const started = performance.now();
    const controller = new AbortController();
    const abortFromParent = (): void => controller.abort();
    options.signal?.addEventListener("abort", abortFromParent, { once: true });
    if (options.signal?.aborted) controller.abort();
    let settleRun = (): void => undefined;
    const settled = new Promise<void>((resolve) => { settleRun = resolve; });
    const previousActive = this.#activeRun;
    const activeRun = { id: runId, controller, settled, settle: settleRun };
    this.#activeRun = activeRun;
    if (mode === "primary") {
      this.#emit(runtimeId, runId, { kind: "message.completed", messageId: this.#idFactory("message"), role: "user", text: options.displayPrompt ?? prompt });
    }
    const selectedModel = options.runtimeSettings?.model ?? state.selectedModels[runtimeId];
    const selectedEffort = options.runtimeSettings?.effort ?? state.selectedEfforts[runtimeId];
    const permissionMode = mode === "review" || mode === "compare"
      ? "safe"
      : (options.runtimeSettings?.permissionMode ?? state.permissionModes[runtimeId] ?? DEFAULT_PERMISSION);
    const request: RunRequest = {
      runId,
      studioSessionId: state.activeSessionId,
      prompt,
      mode,
      cwd: options.cwd ?? state.workspacePath ?? this.#cwd(),
      permissionMode,
      requestApproval: (prompt) => this.#requestApproval(runtimeId, runId, prompt, controller.signal, permissionMode),
      ...(state.nativeSessionIds[runtimeId] && mode === "primary"
        ? { nativeSessionId: state.nativeSessionIds[runtimeId] }
        : {}),
      ...(selectedModel ? { model: selectedModel } : {}),
      ...(selectedEffort ? { effort: selectedEffort } : {}),
    };
    this.#emit(runtimeId, runId, {
      kind: "run.started",
      mode,
      permissionMode: request.permissionMode,
      ...(request.model ? { model: request.model } : {}),
    });

    try {
      let usageEmitted = false;
      const result = await runtime.run(
        request,
        (payload, raw) => {
          if (payload.kind === "usage.updated") usageEmitted = true;
          this.#emit(runtimeId, runId, payload, raw);
        },
        controller.signal,
      );
      if (result.nativeSessionId && (mode === "primary" || mode === "handoff")) {
        const latest = this.#requireState();
        this.#replaceState({
          nativeSessionIds: { ...latest.nativeSessionIds, [runtimeId]: result.nativeSessionId },
        });
        await this.#persist();
        await this.#sessions?.setNativeSessionIds(latest.activeSessionId, this.#requireState().nativeSessionIds);
      }
      if (result.usage && !usageEmitted) this.#emit(runtimeId, runId, { kind: "usage.updated", usage: result.usage });
      this.#emit(runtimeId, runId, { kind: "run.completed", durationMs: Math.round(performance.now() - started) });
      return result;
    } catch (error) {
      const cancelled = controller.signal.aborted;
      this.#emit(runtimeId, runId, {
        kind: "run.failed",
        cancelled,
        error: errorMessage(error),
        durationMs: Math.round(performance.now() - started),
      });
      throw error;
    } finally {
      options.signal?.removeEventListener("abort", abortFromParent);
      activeRun.settle();
      if (this.#activeRun === activeRun) this.#activeRun = previousActive;
    }
  }

  #requestApproval(
    runtimeId: string,
    runId: string,
    prompt: RuntimeApprovalPrompt,
    signal: AbortSignal,
    permission: RuntimePermissionMode,
  ): Promise<string> {
    if (permission === "safe") {
      const decision = prompt.options.find((option) => /decline|deny|cancel|reject/i.test(option)) ?? prompt.options.at(-1);
      if (!decision) return Promise.reject(new Error("runtime supplied an approval with no options"));
      this.#emit(runtimeId, runId, { kind: "approval.resolved", approvalId: prompt.id, decision });
      return Promise.resolve(decision);
    }
    if (this.#pendingApprovals.has(prompt.id)) return Promise.reject(new Error(`duplicate approval id: ${prompt.id}`));
    const waiting = new Promise<string>((resolve, reject) => {
      const abort = (): void => {
        this.#pendingApprovals.delete(prompt.id);
        reject(new Error("approval cancelled with the active run"));
      };
      signal.addEventListener("abort", abort, { once: true });
      this.#pendingApprovals.set(prompt.id, {
        options: [...prompt.options],
        resolve: (decision) => {
          signal.removeEventListener("abort", abort);
          this.#emit(runtimeId, runId, { kind: "approval.resolved", approvalId: prompt.id, decision });
          resolve(decision);
        },
        reject,
      });
      if (signal.aborted) abort();
    });
    if (!signal.aborted) {
      this.#emit(runtimeId, runId, {
        kind: "approval.requested",
        approvalId: prompt.id,
        title: prompt.title,
        detail: prompt.detail,
        options: prompt.options,
      });
    }
    return waiting;
  }

  #emit(runtimeId: string, runId: string, payload: RuntimeEventPayload, raw?: unknown): void {
    const state = this.#requireState();
    const event: RuntimeEvent = {
      id: this.#idFactory("event"),
      runId,
      studioSessionId: state.activeSessionId,
      runtimeId,
      timestamp: this.#now(),
      payload,
      ...(raw === undefined ? {} : { raw }),
    };
    this.#events.push(event);
    if (this.#sessions) void this.#sessions.append(event).catch(() => undefined);
    for (const listener of this.#listeners) listener(structuredClone(event));
  }

  #lastCompletedMessage(role: "assistant" | "reviewer"): string | null {
    for (let index = this.#events.length - 1; index >= 0; index -= 1) {
      const payload = this.#events[index]?.payload;
      if (payload?.kind === "message.completed" && payload.role === role) return payload.text;
    }
    return null;
  }

  #normalizeState(stored: StudioPersistedState | null): StudioPersistedState {
    const knownIds = new Set(this.runtimes.list().map((runtime) => runtime.descriptor.id));
    if (!stored || typeof stored !== "object" || stored.version !== 1) return this.#defaultState();
    const defaults = this.#defaultState();
    const primary = typeof stored.primaryRuntimeId === "string" && knownIds.has(stored.primaryRuntimeId)
      ? stored.primaryRuntimeId
      : null;
    return {
      ...defaults,
      activeSessionId: typeof stored.activeSessionId === "string" ? stored.activeSessionId : defaults.activeSessionId,
      workspacePath: typeof stored.workspacePath === "string" && stored.workspacePath.trim() ? stored.workspacePath.trim() : null,
      primaryRuntimeId: primary,
      reviewerRuntimeIds: Array.isArray(stored.reviewerRuntimeIds) ? stored.reviewerRuntimeIds.filter((id): id is string => typeof id === "string" && knownIds.has(id)) : [],
      reviewerMode: ["off", "manual", "after-turn"].includes(stored.reviewerMode) ? stored.reviewerMode : defaults.reviewerMode,
      selectedModels: stringRecord(stored.selectedModels),
      selectedEfforts: effortRecord(stored.selectedEfforts),
      permissionModes: permissionRecord(stored.permissionModes),
      nativeSessionIds: stringRecord(stored.nativeSessionIds),
      tokenBudget: typeof stored.tokenBudget === "number" && Number.isSafeInteger(stored.tokenBudget) && stored.tokenBudget > 0 ? stored.tokenBudget : null,
      voiceEnabled: stored.voiceEnabled === true,
      theme: stored.theme === "dark" || stored.theme === "light" ? stored.theme : "system",
    };
  }

  #defaultState(): StudioPersistedState {
    return {
      version: 1,
      activeSessionId: this.#idFactory("session"),
      workspacePath: null,
      primaryRuntimeId: null,
      reviewerRuntimeIds: [],
      reviewerMode: "off",
      selectedModels: {},
      selectedEfforts: {},
      permissionModes: {},
      nativeSessionIds: {},
      tokenBudget: null,
      voiceEnabled: false,
      theme: "system",
    };
  }

  #replaceState(patch: Partial<StudioPersistedState>): void {
    this.#state = { ...this.#requireState(), ...patch };
  }

  #requireState(): StudioPersistedState {
    if (!this.#state) throw new Error("StudioService.initialize() must be called first");
    return this.#state;
  }

  async #persist(): Promise<void> {
    await this.#settings.save(structuredClone(this.#requireState()));
  }
}

function primaryMissionPrompt(mission: MissionSnapshot): string {
  return [
    "You are the sole primary writer for a Firekeep Studio mission.",
    "Make the requested change in the bound workspace and leave it in a verifiable state. Do not merely describe what should be done.",
    "Studio records the task result separately from your prose, so report evidence and remaining uncertainty but do not self-grade the mission.",
    "",
    "## Goal",
    mission.goal,
    "",
    "## Acceptance checks",
    ...mission.checks.map((check) => `- ${check.name}: ${check.command}`),
  ].join("\n");
}

function repairMissionPrompt(mission: MissionSnapshot): string {
  const failed = mission.checkReceipts.filter((receipt) => receipt.attempt === mission.attempt - 1 && !receipt.passed);
  const reviews = mission.reviewReceipts.filter((receipt) => receipt.attempt === mission.attempt - 1 && receipt.status === "completed");
  return [
    `Continue the Firekeep Studio mission with repair attempt ${mission.attempt - 1} of ${mission.maxRepairAttempts}.`,
    "Fix the concrete evidence below in the workspace, preserve already-correct behavior, and do not self-grade the mission.",
    "",
    "## Goal",
    mission.goal,
    mission.manualRepairNote ? `\n## Human repair direction\n${mission.manualRepairNote}` : "",
    failed.length ? "\n## Failed checks" : "",
    ...failed.map((receipt) => [
      `### ${receipt.name}`,
      `$ ${receipt.command}`,
      receipt.stdout ? `stdout:\n${receipt.stdout}` : "",
      receipt.stderr ? `stderr:\n${receipt.stderr}` : "",
    ].filter(Boolean).join("\n")),
    reviews.length ? "\n## Advisory review evidence" : "",
    ...reviews.map((review) => `### ${review.runtimeId}\n${review.text}`),
  ].filter(Boolean).join("\n").slice(-MISSION_EVIDENCE_LIMIT);
}

function missionReviewPrompt(mission: MissionSnapshot): string {
  const checks = mission.checkReceipts.filter((receipt) => receipt.attempt === mission.attempt);
  return [
    "Act as an independent, read-only reviewer for a Firekeep Studio mission.",
    "Inspect the bound workspace and the evidence below. Identify concrete defects, missing evidence, regressions, and unsafe assumptions, prioritized by severity.",
    "Your prose is advisory evidence only. Firekeep will not parse it into a task result or use it to trigger repairs automatically.",
    "",
    "## Goal",
    mission.goal,
    "",
    "## Primary report",
    mission.lastPrimaryText,
    "",
    "## Deterministic checks",
    ...checks.map((receipt) => `- ${receipt.name}: ${receipt.passed ? "PASS" : "FAIL"} · ${receipt.command}`),
  ].join("\n").slice(-MISSION_EVIDENCE_LIMIT);
}

function missionApprovalSummary(mission: MissionSnapshot): string {
  return [
    `Goal: ${mission.goal}`,
    `Workspace: ${mission.workspacePath ?? "not selected"}`,
    `Primary: ${mission.primaryRuntimeId ?? "not selected"}`,
    `Primary permission: ${mission.primaryRuntimeId ? mission.runtimeSettings[mission.primaryRuntimeId]?.permissionMode ?? DEFAULT_PERMISSION : "not selected"}`,
    `Reviewers: ${mission.reviewerRuntimeIds.join(", ") || "none"}`,
    `Repairs: ${mission.maxRepairAttempts}`,
    `Measured token guard: ${mission.tokenBudget === null ? "off" : mission.tokenBudget.toLocaleString()}`,
    "Checks executed locally:",
    ...mission.checks.map((check) => `  • ${check.name}: ${check.command}`),
  ].join("\n");
}

function captureMissionRuntimeSettings(
  state: StudioPersistedState,
  runtimeIds: readonly string[],
): Record<string, MissionRuntimeSettings> {
  return Object.fromEntries([...new Set(runtimeIds)].map((runtimeId) => [runtimeId, {
    permissionMode: state.permissionModes[runtimeId] ?? DEFAULT_PERMISSION,
    ...(state.selectedModels[runtimeId] ? { model: state.selectedModels[runtimeId] } : {}),
    ...(state.selectedEfforts[runtimeId] ? { effort: state.selectedEfforts[runtimeId] } : {}),
  }]));
}

function missionWasInterrupted(phase: MissionSnapshot["phase"]): boolean {
  return phase === "running" || phase === "verifying" || phase === "repairing" || phase === "reviewing";
}

function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }

async function waitForSettlement(settled: Promise<void>, timeoutMs: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      settled,
      new Promise<void>((resolve) => { timer = setTimeout(resolve, timeoutMs); }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function stringRecord(value: unknown): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
}

function effortRecord(value: unknown): Record<string, RuntimeEffort> {
  const allowed = new Set<unknown>(["low", "medium", "high", "xhigh", "max"]);
  return Object.fromEntries(Object.entries(stringRecord(value)).filter(([, item]) => allowed.has(item))) as Record<string, RuntimeEffort>;
}

function permissionRecord(value: unknown): Record<string, RuntimePermissionMode> {
  const allowed = new Set<unknown>(["safe", "standard", "unrestricted"]);
  return Object.fromEntries(Object.entries(stringRecord(value)).filter(([, item]) => allowed.has(item))) as Record<string, RuntimePermissionMode>;
}
