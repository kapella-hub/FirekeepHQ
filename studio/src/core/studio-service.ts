import { AsyncLocalStorage } from "node:async_hooks";
import type { RuntimeRegistry } from "./runtime-registry.js";
import { DEFAULT_SESSION_COLOR, type SessionMetadataUpdate, type SessionStore, type SessionWorktree, type StudioSessionSummary, type WorktreeManager } from "./session-store.js";
import type { SettingsStore } from "./settings-store.js";
import { MAX_SCHEDULES, cleanScheduleChecks, describeRepeat, nextOccurrence, parseSchedules, settleMissed, type MissionSchedule, type ScheduleRepeat, type ScheduleRun } from "./schedule.js";
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
  type MissionPhase,
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
import type { ReceiptTracker } from "./keep/receipt-tracker.js";
import { unavailableReceipt, type KeepReceipt } from "./keep/receipt.js";
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
  readonly theme: ThemeMode;
  /** Most recent first, at most MAX_RECENT_WORKSPACES; only these can be re-selected without the native dialog. */
  readonly recentWorkspaces: readonly string[];
  readonly keepPanelOpen: boolean;
  /** The one-time "how did setup go?" invite, shown on the first connected Keep until used or dismissed. */
  readonly feedbackInviteDismissed: boolean;
  /** Raise an OS notification when an agent waits or finishes while Studio is in the background. */
  readonly notifications: boolean;
  /** Missions approved in advance to start at a set time while Studio is open. */
  readonly schedules: readonly MissionSchedule[];
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
  /** Other sessions with work still running or an approval waiting. */
  readonly background: readonly BackgroundSession[];
  readonly activeRunId: string | null;
  readonly eventCount: number;
  readonly usage: SessionUsageSummary;
  readonly mission: MissionSnapshot | null;
  /** The open session's own git worktree, or null when it works in the shared workspace folder. `workspacePath` is then the worktree folder. */
  readonly worktree: SessionWorktree | null;
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
  readonly keep?: StudioKeepOptions;
  /** Creates and removes per-session git worktrees; without it, worktree sessions are unavailable. */
  readonly worktrees?: WorktreeManager;
}

/** Keep link (spec §7.2-7.3). Main owns the files; StudioService never reads or writes them itself. */
export interface StudioKeepOptions {
  readonly tracker: ReceiptTracker;
  /** One report file per run role, so an after-turn reviewer's Keep session cannot take over the primary's attribution. */
  readonly reportPath: (studioSessionId: string, role: "primary" | "review") => string;
  /** Removes every report file of a deleted Studio session. */
  readonly removeReport: (studioSessionId: string) => Promise<void>;
}

const DEFAULT_PERMISSION: RuntimePermissionMode = "standard";
const MAX_RECENT_WORKSPACES = 8;
const MAX_WORKSPACE_PATH = 4_096;
const REVIEW_PACKET_LIMIT = 40_000;
const MISSION_EVIDENCE_LIMIT = 32_000;
/** A slow Keep delays the next Send by at most this long (spec §7.3). */
const FINALIZE_BUDGET_MS = 2000;

interface KeepTurn {
  readonly studioSessionId: string;
  readonly runId: string;
  readonly runtimeId: string;
  readonly events: RuntimeEventPayload[];
  provisional?: KeepReceipt;
}

interface FinalizedKeepTurn extends Omit<KeepTurn, "provisional"> {
  readonly cursorsBefore: Readonly<Record<string, number>>;
}

interface ActiveRun {
  readonly id: string;
  readonly controller: AbortController;
  readonly settled: Promise<void>;
  readonly settle: () => void;
}

/**
 * Everything that belongs to ONE Studio session while it is loaded: its transcript, its
 * mission, and the work running in it. The foreground session always has a context; a
 * session the human switched away from keeps its context for as long as work is running
 * there ("parked"), then is released and lives only in the session store again.
 */
class SessionContext {
  readonly events: RuntimeEvent[] = [];
  mission: MissionSnapshot | null = null;
  missionLaunchPending = false;
  /** True from the moment a turn (prompt -> runs -> reviewers) is claimed until it has fully ended. */
  turn = false;
  /** The workspace the running turn started in; later runs of that turn do not follow a workspace change. */
  turnWorkspace: string | null | undefined = undefined;
  activeMission: { readonly id: string; readonly controller: AbortController } | null = null;
  activeRun: ActiveRun | null = null;
  keepTurn: KeepTurn | null = null;
  nativeSessionIds: Readonly<Record<string, string>> = {};
  /** Set when this session works in its own git worktree; that folder is then its workspace. */
  worktree: SessionWorktree | null = null;
  /** The folder a scheduled mission was approved for; it does not follow later workspace changes. */
  boundWorkspace: string | null = null;

  constructor(readonly id: string) {}

  get busy(): boolean {
    return this.turn || this.activeRun !== null || this.activeMission !== null || this.missionLaunchPending;
  }
}

/** A session other than the foreground one that still has work running or waiting in it. */
export interface BackgroundSession {
  readonly sessionId: string;
  readonly running: boolean;
  readonly missionPhase: MissionPhase | null;
  readonly approvals: readonly { readonly approvalId: string; readonly title: string }[];
}

export class StudioService {
  readonly runtimes: RuntimeRegistry;
  readonly #settings: SettingsStore<StudioPersistedState>;
  readonly #idFactory: (prefix: string) => string;
  readonly #now: () => string;
  readonly #cwd: () => string;
  readonly #sessions: SessionStore | undefined;
  readonly #missionChecks: MissionCheckRunner;
  readonly #confirmMission: (summary: string) => Promise<boolean>;
  readonly #keep: StudioKeepOptions | undefined;
  readonly #worktrees: WorktreeManager | undefined;
  /** The most recently finalized turn of any session; it names its own session. */
  #lastFinalized: FinalizedKeepTurn | null = null;
  /** Serializes the operations that commit Keep cursors (finalize, retry). */
  #keepSerial: Promise<void> = Promise.resolve();
  readonly #deletedSessions = new Set<string>();
  readonly #listeners = new Set<(event: RuntimeEvent) => void>();
  readonly #backgroundListeners = new Set<(event: RuntimeEvent | null) => void>();
  #state: StudioPersistedState | null = null;
  /**
   * Sessions run in parallel. A turn is bound to the context of the session it started in
   * through this scope, so everything it does after an await (emit events, update its mission,
   * read "the active session") still lands in ITS session when the human has switched to
   * another one. Code that runs outside any turn (IPC handlers) sees the foreground session.
   */
  readonly #scope = new AsyncLocalStorage<SessionContext>();
  #foreground = new SessionContext("uninitialized");
  /** The foreground context plus every parked one, by session id. */
  readonly #live = new Map<string, SessionContext>();
  /** One session change (new, resume, delete) at a time. */
  #sessionChanging = false;
  readonly #pendingApprovals = new Map<string, {
    readonly sessionId: string;
    readonly title: string;
    readonly options: readonly string[];
    readonly resolve: (decision: string) => void;
    readonly reject: (error: Error) => void;
  }>();

  get #ctx(): SessionContext { return this.#scope.getStore() ?? this.#foreground; }
  get #events(): RuntimeEvent[] { return this.#ctx.events; }
  get #mission(): MissionSnapshot | null { return this.#ctx.mission; }
  set #mission(value: MissionSnapshot | null) { this.#ctx.mission = value; }
  get #missionLaunchPending(): boolean { return this.#ctx.missionLaunchPending; }
  set #missionLaunchPending(value: boolean) { this.#ctx.missionLaunchPending = value; }
  get #activeMission(): SessionContext["activeMission"] { return this.#ctx.activeMission; }
  set #activeMission(value: SessionContext["activeMission"]) { this.#ctx.activeMission = value; }
  get #activeRun(): ActiveRun | null { return this.#ctx.activeRun; }
  set #activeRun(value: ActiveRun | null) { this.#ctx.activeRun = value; }
  get #keepTurn(): KeepTurn | null { return this.#ctx.keepTurn; }
  set #keepTurn(value: KeepTurn | null) { this.#ctx.keepTurn = value; }

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
    this.#keep = options.keep;
    this.#worktrees = options.worktrees;
  }

  async initialize(): Promise<void> {
    if (this.#state) return;
    const stored = await this.#settings.load();
    this.#state = this.#normalizeState(stored);
    // Nothing fires late: a time that passed while Studio was closed is noted and moved on.
    this.#state = { ...this.#state, schedules: settleMissed(this.#state.schedules, this.#now()) };
    this.#foreground = new SessionContext(this.#state.activeSessionId);
    this.#foreground.nativeSessionIds = this.#state.nativeSessionIds;
    this.#live.set(this.#foreground.id, this.#foreground);
    if (this.#sessions) {
      await this.#sessions.ensure(this.#state.activeSessionId, this.#now());
      const summary = (await this.#sessions.list()).find((item) => item.id === this.#state?.activeSessionId);
      if (summary) {
        this.#replaceState({ nativeSessionIds: { ...summary.nativeSessionIds } });
        this.#foreground.worktree = summary.worktree ?? null;
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
      this.#restoreKeepCursors(this.#state.activeSessionId, this.#events);
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
      background: this.#backgroundSessions(),
      worktree: this.#ctx.worktree,
    });
  }

  /** Sessions other than the one the caller is in that still have work running or waiting. */
  #backgroundSessions(): BackgroundSession[] {
    const current = this.#ctx;
    const sessions: BackgroundSession[] = [];
    for (const context of this.#live.values()) {
      if (context === current) continue;
      const approvals = [...this.#pendingApprovals].filter(([, pending]) => pending.sessionId === context.id).map(([approvalId, pending]) => ({ approvalId, title: pending.title }));
      if (!context.busy && approvals.length === 0) continue;
      sessions.push({ sessionId: context.id, running: context.busy, missionPhase: context.mission?.phase ?? null, approvals });
    }
    return sessions;
  }

  /**
   * Events of sessions that are NOT in the foreground, and `null` whenever the set of
   * background sessions changes. `subscribe` keeps delivering the foreground transcript only.
   */
  subscribeBackground(listener: (event: RuntimeEvent | null) => void): () => void {
    this.#backgroundListeners.add(listener);
    return () => this.#backgroundListeners.delete(listener);
  }

  #backgroundChanged(): void {
    this.#scope.exit(() => {
      for (const listener of this.#backgroundListeners) {
        try { listener(null); } catch { /* a throwing listener never affects a session */ }
      }
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
    if (this.#ctx.worktree) throw new Error("this session works in its own worktree; start a new session to use another folder");
    if (this.#activeRun) throw new Error("cannot change workspace while a run is active");
    if (this.#missionLaunchPending) throw new Error("cannot change workspace while mission approval is open");
    const normalized = path?.trim() || null;
    if (normalized && normalized.length > MAX_WORKSPACE_PATH) throw new Error("workspace path is too long");
    const state = this.#requireState();
    this.#replaceState({
      workspacePath: normalized,
      ...(normalized
        ? { recentWorkspaces: [normalized, ...state.recentWorkspaces.filter((item) => item !== normalized)].slice(0, MAX_RECENT_WORKSPACES) }
        : {}),
    });
    await this.#persist();
  }

  /** Re-selects a workspace the user already chose through the native dialog; any other path is refused. */
  async switchToRecentWorkspace(path: string): Promise<void> {
    if (!this.#requireState().recentWorkspaces.includes(path)) throw new Error("not a recent workspace");
    await this.setWorkspace(path);
  }

  async setKeepPanelOpen(open: boolean): Promise<void> {
    this.#replaceState({ keepPanelOpen: open });
    await this.#persist();
  }

  async setNotifications(enabled: boolean): Promise<void> {
    this.#replaceState({ notifications: enabled });
    await this.#persist();
  }

  async dismissFeedbackInvite(): Promise<void> {
    this.#replaceState({ feedbackInviteDismissed: true });
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

  async setTheme(theme: ThemeMode): Promise<void> {
    this.#replaceState({ theme });
    await this.#persist();
  }

  /**
   * Starts a fresh session in the foreground. Work running in the session being left keeps
   * running there; only an open mission confirmation dialog blocks the switch.
   */
  async startNewSession(options: { readonly worktree?: boolean } = {}): Promise<void> {
    if (this.#foreground.missionLaunchPending) throw new Error("cannot change sessions while mission approval is open");
    const isolated = options.worktree === true;
    if (isolated) {
      if (!this.#worktrees || !this.#sessions) throw new Error("worktree sessions are unavailable");
      if (!this.#rawState().workspacePath) throw new Error("choose a workspace folder before starting a worktree session");
    }
    await this.#exclusively("session", () => this.#switchToNewSession(isolated));
  }

  /**
   * `isolated` gives the new session its own git worktree of the chosen workspace. The worktree
   * is created first, so a repository that cannot have one leaves the open session untouched.
   */
  async #switchToNewSession(isolated = false): Promise<void> {
    const id = this.#idFactory("session");
    const worktree = isolated ? await (this.#worktrees as WorktreeManager).create(id, this.#rawState().workspacePath as string) : null;
    await this.#leaveForeground();
    const next = new SessionContext(id);
    next.worktree = worktree;
    this.#enterForeground(next);
    await this.#sessions?.ensure(next.id, this.#now());
    if (worktree) await this.#sessions?.setWorktree(next.id, worktree);
    await this.#persist();
  }

  /** The foreground session steps back: parked while work runs in it, otherwise finalized and released. */
  async #leaveForeground(): Promise<void> {
    const current = this.#foreground;
    if (current.busy) return;   // parked: its turn finalizes its own Keep receipt when it ends
    await this.#finalizeKeepTurn();
    if (!current.busy) this.#live.delete(current.id);
  }

  #enterForeground(context: SessionContext): void {
    this.#live.set(context.id, context);
    this.#foreground = context;
    this.#state = { ...this.#rawState(), activeSessionId: context.id, nativeSessionIds: context.nativeSessionIds };
    this.#backgroundChanged();
  }

  /** A parked session whose work has ended goes back to living only in the session store. */
  async #release(context: SessionContext): Promise<void> {
    if (context === this.#foreground || context.busy) { this.#backgroundChanged(); return; }
    await this.#scope.run(context, () => this.#finalizeKeepTurn()).catch(() => undefined);
    if (context !== this.#foreground && !context.busy) this.#live.delete(context.id);
    this.#backgroundChanged();
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

  /**
   * Deletes a session. Its worktree, if it has one, is removed only when clean; the returned
   * sentence says what was left behind and where, or is null when nothing was.
   */
  async deleteSession(sessionId: string, confirmation: string): Promise<string | null> {
    if (this.#live.get(sessionId)?.busy) throw new Error("cannot delete a session while a run is active");
    if (!this.#sessions) throw new Error("persistent sessions are unavailable");
    if (confirmation !== sessionId) throw new Error("session deletion requires --confirm with the exact session id");
    const sessions = this.#sessions;
    return this.#exclusively("session", async () => {
      if (this.#live.get(sessionId)?.busy) throw new Error("cannot delete a session while a run is active");
      const worktree = (await sessions.list()).find((item) => item.id === sessionId)?.worktree ?? null;
      await this.#finalizeKeepTurn();
      if (sessionId === this.#foreground.id) await this.#switchToNewSession();
      this.#live.delete(sessionId);
      // Both session stores recreate a session on append, so no late receipt may write here again.
      this.#deletedSessions.add(sessionId);
      await sessions.remove(sessionId);
      if (this.#keep) {
        await this.#keep.removeReport(sessionId).catch(() => undefined);
        this.#keep.tracker.forget(sessionId);
        if (this.#lastFinalized?.studioSessionId === sessionId) this.#lastFinalized = null;
      }
      if (!worktree) return null;
      if (!this.#worktrees) return `Its worktree was left at ${worktree.root}.`;
      return this.#worktrees.remove(worktree).catch((error: unknown) => `Its worktree at ${worktree.root} could not be removed: ${errorMessage(error)}`);
    });
  }

  /**
   * Brings a session to the foreground. A session with work still running is picked up live,
   * exactly where it is; any other is loaded from the store. Work in the session being left
   * keeps running.
   */
  async resumeSession(sessionId: string): Promise<void> {
    if (this.#foreground.missionLaunchPending) throw new Error("cannot change sessions while mission approval is open");
    if (!this.#sessions) throw new Error("persistent sessions are unavailable");
    const sessions = this.#sessions;
    await this.#exclusively("session", async () => {
      const summary = (await sessions.list()).find((item) => item.id === sessionId);
      if (!summary) throw new Error(`unknown session: ${sessionId}`);
      const live = this.#live.get(sessionId);
      if (live?.busy) {
        if (live === this.#foreground) return;
        await this.#leaveForeground();
        this.#enterForeground(live);
      } else {
        await this.#leaveForeground();
        this.#live.delete(sessionId);
        const next = new SessionContext(sessionId);
        next.events.push(...await sessions.load(sessionId));
        this.#restoreKeepCursors(sessionId, next.events);
        next.mission = summary.mission ? structuredClone(summary.mission) : null;
        next.nativeSessionIds = { ...summary.nativeSessionIds };
        next.worktree = summary.worktree ?? null;
        this.#enterForeground(next);
      }
      await this.#persist();
    });
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
    const cleanGoal = cleanMissionGoal(goal);
    if (this.#mission) await this.startNewSession();
    return this.#createDraftMission(cleanGoal);
  }

  /** Builds and stores a draft in the active session; callers own any session switch and locking. */
  async #createDraftMission(cleanGoal: string): Promise<MissionSnapshot> {
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
    this.#requireDraftMission();
    return this.#exclusively("turn", () => this.#launchMission());
  }

  /**
   * Draft, checks and run in one step, all under one turn lock: no session change can land
   * between the steps, so the checks (and a cancel on failure) always hit this quick mission's
   * own draft. The run still needs the native confirmation.
   */
  async quickMission(goal: string, checks: readonly string[]): Promise<MissionSnapshot> {
    if (this.#activeRun || this.#activeMission) throw new Error("cannot create a mission while work is active");
    const cleanGoal = cleanMissionGoal(goal);
    if (this.#ctx.turn) throw new Error("a run is already active");
    // A session that already holds a mission keeps it: the quick mission gets a session of its
    // own, entered before the turn is claimed so the whole turn belongs to the new session.
    if (this.#mission) await this.#exclusively("session", () => this.#switchToNewSession());
    return this.#exclusively("turn", async () => {
      await this.#createDraftMission(cleanGoal);
      try {
        for (const check of checks) await this.addMissionCheck(check);
      } catch (error) {
        this.cancelMission();
        throw error;
      }
      return this.#launchMission();
    });
  }

  /**
   * Approves a mission now so it can start later without asking again. The native confirmation
   * shows everything the approval freezes: goal, check commands, folder, agents and their
   * permission modes, and when it runs. Returns null when the human declines.
   */
  async scheduleMission(input: { readonly goal: string; readonly checks: readonly string[]; readonly firstRunAt: string; readonly repeat: ScheduleRepeat; readonly worktree?: boolean }): Promise<MissionSchedule | null> {
    const state = this.#rawState();
    const goal = cleanMissionGoal(input.goal);
    const checks = cleanScheduleChecks(input.checks);
    if (state.schedules.length >= MAX_SCHEDULES) throw new Error(`Studio keeps at most ${MAX_SCHEDULES} scheduled missions`);
    if (!state.workspacePath) throw new Error("choose a workspace folder before scheduling a mission");
    if (!state.primaryRuntimeId) throw new Error("choose an agent before scheduling a mission");
    const first = Date.parse(input.firstRunAt);
    const now = this.#now();
    if (Number.isNaN(first)) throw new Error("the scheduled time is not a valid date");
    if (first <= Date.parse(now)) throw new Error("the scheduled time must be in the future");
    const worktree = input.worktree === true;
    if (worktree && (!this.#worktrees || !this.#sessions)) throw new Error("worktree sessions are unavailable");
    const reviewerRuntimeIds = [...state.reviewerRuntimeIds];
    const schedule: MissionSchedule = {
      id: this.#idFactory("schedule"),
      goal,
      checks,
      repeat: input.repeat,
      nextRunAt: new Date(first).toISOString(),
      workspacePath: state.workspacePath,
      primaryRuntimeId: state.primaryRuntimeId,
      reviewerRuntimeIds,
      runtimeSettings: captureMissionRuntimeSettings(state, [state.primaryRuntimeId, ...reviewerRuntimeIds]),
      worktree,
      createdAt: now,
      approvedAt: now,
      lastRun: null,
    };
    if (!(await this.#confirmMission(scheduleApprovalSummary(schedule)))) return null;
    this.#replaceState({ schedules: [...this.#rawState().schedules, schedule] });
    await this.#persist();
    return schedule;
  }

  async deleteSchedule(scheduleId: string): Promise<void> {
    const schedules = this.#rawState().schedules;
    if (!schedules.some((schedule) => schedule.id === scheduleId)) throw new Error("unknown scheduled mission");
    this.#replaceState({ schedules: schedules.filter((schedule) => schedule.id !== scheduleId) });
    await this.#persist();
  }

  /**
   * Starts every scheduled mission whose time has come, each in a session of its own behind
   * the open one. Resolves when those missions have stopped (finished, paused or waiting).
   */
  async runDueSchedules(): Promise<void> {
    const moment = Date.parse(this.#now());
    const due = this.#rawState().schedules.filter((schedule) => schedule.nextRunAt !== null && Date.parse(schedule.nextRunAt) <= moment);
    if (due.length === 0) return;
    // The next occurrence is stored before anything starts, so a schedule can never fire twice for one time.
    this.#replaceState({ schedules: this.#rawState().schedules.map((schedule) => (due.includes(schedule) ? { ...schedule, nextRunAt: nextOccurrence(schedule.nextRunAt as string, schedule.repeat, moment) } : schedule)) });
    await this.#persist();
    await Promise.all(due.map((schedule) => this.#runSchedule(schedule)));
  }

  async #runSchedule(schedule: MissionSchedule): Promise<void> {
    const at = this.#now();
    const context = new SessionContext(this.#idFactory("session"));
    context.boundWorkspace = schedule.workspacePath;
    let sessionId: string | null = null;
    try {
      if (schedule.worktree) {
        if (!this.#worktrees || !this.#sessions) throw new Error("worktree sessions are unavailable");
        context.worktree = await this.#worktrees.create(context.id, schedule.workspacePath);
      }
      await this.#sessions?.ensure(context.id, at);
      if (context.worktree) await this.#sessions?.setWorktree(context.id, context.worktree);
      this.#live.set(context.id, context);
      sessionId = context.id;
      await this.#recordScheduleRun(schedule.id, { at, sessionId, error: null });
      await this.#scope.run(context, () => this.#exclusively("turn", async () => {
        await this.#createDraftMission(schedule.goal);
        await this.#updateMission({ primaryRuntimeId: schedule.primaryRuntimeId, reviewerRuntimeIds: [...schedule.reviewerRuntimeIds] });
        for (const check of schedule.checks) await this.addMissionCheck(check);
        return this.#launchMission(schedule);
      }));
    } catch (error) {
      await this.#recordScheduleRun(schedule.id, { at, sessionId, error: errorMessage(error) }).catch(() => undefined);
    } finally {
      if (context !== this.#foreground && !context.busy) this.#live.delete(context.id);
      this.#backgroundChanged();
    }
  }

  async #recordScheduleRun(scheduleId: string, run: ScheduleRun): Promise<void> {
    this.#replaceState({ schedules: this.#rawState().schedules.map((schedule) => (schedule.id === scheduleId ? { ...schedule, lastRun: run } : schedule)) });
    await this.#persist();
    this.#backgroundChanged();
  }

  /** `approved` is a schedule the human already confirmed: its frozen settings are used and no dialog opens. */
  async #launchMission(approved?: MissionSchedule): Promise<MissionSnapshot> {
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
      const runtimeSettings = approved
        ? Object.fromEntries([bound.primaryRuntimeId, ...bound.reviewerRuntimeIds].map((runtimeId) => [runtimeId, approved.runtimeSettings[runtimeId] ?? { permissionMode: "safe" as const }]))
        : captureMissionRuntimeSettings(state, [bound.primaryRuntimeId, ...bound.reviewerRuntimeIds]);
      if (mission.checks.length === 0) throw new Error("add at least one deterministic mission check before running");
      this.runtimes.require(bound.primaryRuntimeId);
      if (!approved && !(await this.#confirmMission(missionApprovalSummary({ ...bound, runtimeSettings })))) return this.mission() as MissionSnapshot;
      await this.#finalizeKeepTurn();
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
    return this.#exclusively("turn", async () => {
      await this.#finalizeKeepTurn();
      await this.#updateMission({ blockReason: null });
      return this.#driveMission();
    });
  }

  async repairMission(note: string): Promise<MissionSnapshot> {
    const mission = this.#requireMission();
    if (mission.phase !== "awaiting-approval") throw new Error("mission is not waiting for a review decision");
    if (mission.attempt - 1 >= mission.maxRepairAttempts) throw new Error("mission repair allowance is exhausted");
    const cleanNote = note.trim();
    if (!cleanNote) throw new Error("human-directed repair requires a note");
    if (cleanNote.length > 4_000) throw new Error("mission repair note is too long");
    return this.#exclusively("turn", async () => {
      await this.#finalizeKeepTurn();
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
    });
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
    const primaryRuntimeId = state.primaryRuntimeId;

    return this.#exclusively("turn", async () => {
      await this.#finalizeKeepTurn();
      try {
        const result = await this.#run(primaryRuntimeId, withStudioVisualHint(text), "primary", { displayPrompt: text });
        if (this.#requireState().reviewerMode === "after-turn") {
          await this.#runConfiguredReviewers(result.finalText);
        }
        return result;
      } finally {
        void this.#provisionalKeepReceipt();
      }
    });
  }

  async runReview(runtimeId?: string, focus?: string): Promise<RunResult[]> {
    const state = this.#requireState();
    const reviewers = runtimeId ? [runtimeId] : [...state.reviewerRuntimeIds];
    if (reviewers.length === 0) throw new Error("no reviewers configured");
    const answer = this.#lastCompletedMessage("assistant");
    if (!answer) throw new Error("there is no primary response to review");
    if (this.#activeRun) throw new Error("a run is already active");
    return this.#exclusively("turn", async () => {
      // A Second opinion is its own turn: the answer's receipt is finalized first.
      await this.#finalizeKeepTurn();
      const results: RunResult[] = [];
      const failures: string[] = [];
      try {
        for (const id of reviewers) {
          try { results.push(await this.#runReview(id, answer, focus)); }
          catch (error) { failures.push(`${id}: ${errorMessage(error)}`); }
        }
      } finally {
        void this.#provisionalKeepReceipt();
      }
      if (results.length === 0 && failures.length) throw new Error(`all reviewers failed: ${failures.join("; ")}`);
      return results;
    });
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
    return this.#exclusively("turn", async () => {
      await this.setPrimary(runtimeId);
      await this.#finalizeKeepTurn();
      try {
        return await this.#run(runtimeId, packet, "handoff");
      } finally {
        void this.#provisionalKeepReceipt();
      }
    });
  }

  /**
   * Re-collect a receipt the Keep could not answer. Only the pending turn or,
   * with no turn pending, the most recently finalized one: an older turn (or a
   * finalized one once a newer turn is open) would take later turns' events.
   */
  async retryReceipt(runId: string): Promise<void> {
    if (!this.#keep) throw new Error("the Keep link is unavailable");
    if (this.#keepTurn?.runId === runId) {
      await this.#provisionalKeepReceipt();
      return;
    }
    const last = this.#lastFinalized;
    if (!last || last.runId !== runId || this.#keepTurn) throw new Error("only the latest receipt can be retried");
    const tracker = this.#keep.tracker;
    await this.#withKeepLock(async () => {
      if (this.#keepTurn || this.#lastFinalized !== last) throw new Error("only the latest receipt can be retried");
      const committed = tracker.cursors(last.studioSessionId);
      tracker.restore(last.studioSessionId, last.cursorsBefore);
      let collected: KeepReceipt | null;
      try {
        // Non-committing collect: one that answers after the budget can never move cursors.
        collected = await withinBudget(tracker.collect(last.studioSessionId, [...last.events], false).catch(() => null), FINALIZE_BUDGET_MS);
      } finally {
        tracker.restore(last.studioSessionId, committed);
      }
      if (collected?.status === "measured") tracker.commit(last.studioSessionId, collected.cursorsAfter);
      this.#emitInto(last, collected ? { ...collected, final: true } : unavailableReceipt("Keep did not answer in time", true, committed));
    });
  }

  /** Cancels the named run in whichever session it is running, or the current session's run. */
  cancel(runId?: string): boolean {
    const run = runId
      ? [...this.#live.values()].find((context) => context.activeRun?.id === runId)?.activeRun
      : this.#activeRun;
    if (!run) return false;
    run.controller.abort();
    return true;
  }

  /** Answers an approval in whichever session asked for it. */
  resolveApproval(approvalId: string, decision: string): boolean {
    const pending = this.#pendingApprovals.get(approvalId);
    if (!pending || !pending.options.includes(decision)) return false;
    this.#pendingApprovals.delete(approvalId);
    pending.resolve(decision);
    return true;
  }

  async shutdown(): Promise<void> {
    const running: ActiveRun[] = [];
    for (const context of this.#live.values()) {
      context.activeMission?.controller.abort();
      if (context.activeRun) {
        running.push(context.activeRun);
        context.activeRun.controller.abort();
      }
    }
    await Promise.all(running.map((run) => waitForSettlement(run.settled, 5_000)));
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
      void this.#provisionalKeepReceipt();
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
    if (this.#keep) await this.#keepSerial; // a run never starts while a receipt retry has cursors rolled back
    if (this.#activeRun) throw new Error("a run is already active");
    const runtime = this.runtimes.requireCapability(runtimeId, mode === "review" ? "review" : "chat");
    // A runtime calls back from its own process streams, outside any turn: every callback is
    // re-entered into this run's session so it can never land in whichever session is in front.
    const context = this.#ctx;
    const state = this.#requireState();
    if (state.tokenBudget !== null && this.usageSummary().tokens >= state.tokenBudget) {
      throw new Error(`session token budget reached (${state.tokenBudget.toLocaleString()} tokens); use /budget set or /budget off to continue`);
    }
    const runId = this.#idFactory("run");
    // The first run after a finalize opens the turn; after-turn and mission reviewers join it.
    if (this.#keep && !this.#keepTurn) this.#keepTurn = { studioSessionId: state.activeSessionId, runId, runtimeId, events: [] };
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
    const permissionMode = mode === "review"
      ? "safe"
      : (options.runtimeSettings?.permissionMode ?? state.permissionModes[runtimeId] ?? DEFAULT_PERMISSION);
    const request: RunRequest = {
      runId,
      studioSessionId: state.activeSessionId,
      prompt,
      mode,
      cwd: options.cwd ?? state.workspacePath ?? this.#cwd(),
      permissionMode,
      requestApproval: (prompt) => this.#scope.run(context, () => this.#requestApproval(runtimeId, runId, prompt, controller.signal, permissionMode)),
      ...(state.nativeSessionIds[runtimeId] && mode === "primary"
        ? { nativeSessionId: state.nativeSessionIds[runtimeId] }
        : {}),
      ...(selectedModel ? { model: selectedModel } : {}),
      ...(selectedEffort ? { effort: selectedEffort } : {}),
      ...(this.#keep
        ? { env: { FIREKEEP_SESSION_REPORT_FILE: this.#keep.reportPath(state.activeSessionId, mode === "review" ? "review" : "primary") } }
        : {}),
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
        (payload, raw) => this.#scope.run(context, () => {
          if (payload.kind === "usage.updated") usageEmitted = true;
          this.#keepTurn?.events.push(payload);
          this.#emit(runtimeId, runId, payload, raw);
        }),
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
    const context = this.#ctx;
    const waiting = new Promise<string>((resolve, reject) => {
      const abort = (): void => {
        this.#pendingApprovals.delete(prompt.id);
        reject(new Error("approval cancelled with the active run"));
      };
      signal.addEventListener("abort", abort, { once: true });
      this.#pendingApprovals.set(prompt.id, {
        sessionId: context.id,
        title: prompt.title,
        options: [...prompt.options],
        // The answer arrives from an IPC handler; it is recorded in the session that asked.
        resolve: (decision) => this.#scope.run(context, () => {
          signal.removeEventListener("abort", abort);
          this.#emit(runtimeId, runId, { kind: "approval.resolved", approvalId: prompt.id, decision });
          resolve(decision);
        }),
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

  /**
   * Appends to `studioSessionId` (default: the session of the turn that is emitting). The
   * event is held in memory when that session is loaded, and always stored. Foreground
   * events go to `subscribe` listeners (the renderer shows one transcript and does not
   * filter by session); a parked session's events go to `subscribeBackground` listeners.
   */
  #emit(runtimeId: string, runId: string, payload: RuntimeEventPayload, raw?: unknown, studioSessionId?: string): void {
    this.#requireState();
    const owner = studioSessionId === undefined ? this.#ctx : this.#live.get(studioSessionId) ?? null;
    const sessionId = studioSessionId ?? this.#ctx.id;
    const event: RuntimeEvent = {
      id: this.#idFactory("event"),
      runId,
      studioSessionId: sessionId,
      runtimeId,
      timestamp: this.#now(),
      payload,
      ...(raw === undefined ? {} : { raw }),
    };
    if (owner) owner.events.push(event);
    if (this.#sessions) void this.#sessions.append(event).catch(() => undefined);
    if (!owner) return;
    // Listeners run outside the turn's scope: they describe the foreground (snapshot()), not this session.
    this.#scope.exit(() => {
      if (owner === this.#foreground) for (const listener of this.#listeners) listener(structuredClone(event));
      else for (const listener of this.#backgroundListeners) listener(structuredClone(event));
    });
  }

  /**
   * Finalize the pending turn's receipt before a new turn's prompt leaves or the
   * active session changes (spec §7.3). Bounded: a slow Keep delays Send by at
   * most FINALIZE_BUDGET_MS. A turn whose work is still running is never finalized.
   */
  async #finalizeKeepTurn(): Promise<void> {
    if (!this.#keep) return;
    const turn = this.#activeRun || this.#activeMission ? null : this.#keepTurn;
    if (turn) this.#keepTurn = null;
    await this.#withKeepLock(async () => {
      if (!turn || !this.#keep) return;
      const tracker = this.#keep.tracker;
      const before = tracker.cursors(turn.studioSessionId);
      // Non-committing collect, committed here only if it answered within the budget: a collect
      // that answers later would commit offsets that already include the next turn's events.
      const collected = await withinBudget(tracker.collect(turn.studioSessionId, [...turn.events], false).catch(() => null), FINALIZE_BUDGET_MS);
      let receipt: KeepReceipt;
      if (collected?.status === "measured") {
        tracker.commit(turn.studioSessionId, collected.cursorsAfter);
        receipt = { ...collected, final: true };
      } else if (turn.provisional?.status === "measured") {
        // The Keep did not measure the final (timeout, error, gateway gone): the receipt measured
        // when the turn ended becomes final, its cursors commit, and later events roll into the next turn.
        receipt = { ...turn.provisional, final: true };
        tracker.commit(turn.studioSessionId, turn.provisional.cursorsAfter);
      } else receipt = collected ? { ...collected, final: true } : unavailableReceipt("Keep did not answer in time", true, before);
      this.#lastFinalized = { studioSessionId: turn.studioSessionId, runId: turn.runId, runtimeId: turn.runtimeId, events: turn.events, cursorsBefore: before };
      this.#emitInto(turn, receipt);
    });
  }

  /** Fire-and-forget after a turn: never awaited by the turn, the receipt arrives by push event. Never rejects. */
  #provisionalKeepReceipt(): Promise<void> {
    const turn = this.#keepTurn;
    if (!turn || !this.#keep) return Promise.resolve();
    return this.#keep.tracker.collect(turn.studioSessionId, [...turn.events], false)
      .then((receipt) => {
        if (this.#keepTurn !== turn) return; // already finalized; the final receipt wins
        turn.provisional = receipt;
        this.#emitInto(turn, receipt);
      })
      .catch(() => undefined); // a Keep failure never fails or blocks a turn (spec §8)
  }

  /** Append to the session that produced the turn, even if another session is active now. */
  #emitInto(turn: Pick<KeepTurn, "studioSessionId" | "runId" | "runtimeId">, receipt: KeepReceipt): void {
    if (this.#deletedSessions.has(turn.studioSessionId)) return;
    try {
      this.#emit(turn.runtimeId, turn.runId, { kind: "keep.receipt", receipt }, undefined, turn.studioSessionId);
    } catch {
      // A throwing listener must never fail or block a turn through its receipt (spec §8).
    }
  }

  /**
   * Claims an exclusion synchronously (callers invoke it before their first await).
   *
   * "turn": one turn at a time PER SESSION. The whole turn, including every await and the
   * Keep finalize, runs bound to the session it was claimed in, so it can never start in one
   * session and emit into another, however often the foreground changes meanwhile.
   * "session": one session change at a time. It does not wait for running turns; the session
   * being left is parked instead. No turn may start in the foreground while it is changing.
   */
  async #exclusively<T>(kind: "turn" | "session", work: () => Promise<T>): Promise<T> {
    if (kind === "session") {
      if (this.#sessionChanging) throw new Error("another session change is in progress");
      this.#sessionChanging = true;
      try {
        return await this.#scope.exit(work);
      } finally {
        this.#sessionChanging = false;
      }
    }
    const context = this.#ctx;
    if (context.turn) throw new Error("a run is already active");
    if (this.#sessionChanging && context === this.#foreground) throw new Error("cannot start work while the session is changing");
    context.turn = true;
    context.turnWorkspace = this.#rawState().workspacePath;
    try {
      return await this.#scope.run(context, async () => {
        const worktree = context.worktree;
        if (worktree && this.#worktrees && !(await this.#worktrees.exists(worktree))) throw new Error(`this session's worktree is gone (${worktree.root}); start a new session to keep working`);
        return work();
      });
    } finally {
      context.turn = false;
      context.turnWorkspace = undefined;
      void this.#release(context);
    }
  }

  #withKeepLock(operation: () => Promise<void>): Promise<void> {
    const next = this.#keepSerial.then(operation);
    this.#keepSerial = next.catch(() => undefined);
    return next;
  }

  /** Cursors only move forward: a resumed session never re-counts events its last final receipt covered. */
  #restoreKeepCursors(sessionId: string, events: readonly RuntimeEvent[]): void {
    if (!this.#keep) return;
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const payload = events[index]?.payload;
      if (payload?.kind === "keep.receipt" && payload.receipt.final) {
        this.#keep.tracker.commit(sessionId, payload.receipt.cursorsAfter);
        return;
      }
    }
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
    const workspacePath = typeof stored.workspacePath === "string" && stored.workspacePath.trim() ? stored.workspacePath.trim() : null;
    const primary = typeof stored.primaryRuntimeId === "string" && knownIds.has(stored.primaryRuntimeId)
      ? stored.primaryRuntimeId
      : null;
    return {
      ...defaults,
      activeSessionId: typeof stored.activeSessionId === "string" ? stored.activeSessionId : defaults.activeSessionId,
      workspacePath,
      primaryRuntimeId: primary,
      reviewerRuntimeIds: Array.isArray(stored.reviewerRuntimeIds) ? stored.reviewerRuntimeIds.filter((id): id is string => typeof id === "string" && knownIds.has(id)) : [],
      reviewerMode: ["off", "manual", "after-turn"].includes(stored.reviewerMode) ? stored.reviewerMode : defaults.reviewerMode,
      selectedModels: stringRecord(stored.selectedModels),
      selectedEfforts: effortRecord(stored.selectedEfforts),
      permissionModes: permissionRecord(stored.permissionModes),
      nativeSessionIds: stringRecord(stored.nativeSessionIds),
      tokenBudget: typeof stored.tokenBudget === "number" && Number.isSafeInteger(stored.tokenBudget) && stored.tokenBudget > 0 ? stored.tokenBudget : null,
      theme: stored.theme === "dark" || stored.theme === "light" ? stored.theme : "system",
      recentWorkspaces: normalizeRecentWorkspaces(stored.recentWorkspaces, workspacePath),
      keepPanelOpen: stored.keepPanelOpen !== false,
      feedbackInviteDismissed: stored.feedbackInviteDismissed === true,
      notifications: stored.notifications !== false,
      schedules: parseSchedules(stored.schedules),
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
      theme: "system",
      recentWorkspaces: [],
      keepPanelOpen: true,
      feedbackInviteDismissed: false,
      notifications: true,
      schedules: [],
    };
  }

  /**
   * Shared settings are written to the persisted state from any session. The session-specific
   * parts (which session is active, its native session ids) belong to the context: a parked
   * session updates its own and never the foreground's.
   */
  #replaceState(patch: Partial<StudioPersistedState>): void {
    const raw = this.#rawState();
    const context = this.#ctx;
    if (patch.nativeSessionIds) context.nativeSessionIds = patch.nativeSessionIds;
    if (context === this.#foreground) {
      this.#state = { ...raw, ...patch };
      return;
    }
    const { nativeSessionIds: _native, activeSessionId: _active, ...shared } = patch;
    this.#state = { ...raw, ...shared };
  }

  /** The persisted state as the calling session sees it: a parked session is its own "active session". */
  #requireState(): StudioPersistedState {
    const raw = this.#rawState();
    const context = this.#ctx;
    const workspacePath = context.worktree ? context.worktree.path : context.boundWorkspace ? context.boundWorkspace : context.turnWorkspace === undefined ? raw.workspacePath : context.turnWorkspace;
    if (context === this.#foreground && workspacePath === raw.workspacePath) return raw;
    return { ...raw, activeSessionId: context.id, nativeSessionIds: context.nativeSessionIds, workspacePath };
  }

  #rawState(): StudioPersistedState {
    if (!this.#state) throw new Error("StudioService.initialize() must be called first");
    return this.#state;
  }

  async #persist(): Promise<void> {
    await this.#settings.save(structuredClone(this.#rawState()));
  }
}

/**
 * Trimmed (setWorkspace stores trimmed paths, so membership checks stay exact), de-duplicated
 * keeping the first occurrence, capped. Settings from before recents existed seed the list with
 * the current workspace so it is selectable again without the dialog.
 */
function normalizeRecentWorkspaces(stored: unknown, workspacePath: string | null): string[] {
  const recents: string[] = [];
  if (Array.isArray(stored)) {
    for (const value of stored) {
      if (typeof value !== "string" || value.length > MAX_WORKSPACE_PATH) continue;
      const path = value.trim();
      if (path && !recents.includes(path)) recents.push(path);
      if (recents.length === MAX_RECENT_WORKSPACES) break;
    }
  }
  return recents.length === 0 && workspacePath ? [workspacePath] : recents;
}

function cleanMissionGoal(goal: string): string {
  const cleanGoal = goal.trim();
  if (!cleanGoal) throw new Error("mission goal cannot be empty");
  if (cleanGoal.length > 20_000) throw new Error("mission goal is too long");
  return cleanGoal;
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

function scheduleApprovalSummary(schedule: MissionSchedule): string {
  const when = new Date(schedule.nextRunAt as string).toLocaleString(undefined, { dateStyle: "full", timeStyle: "short" });
  return [
    `SCHEDULED MISSION: starts ${when}, ${describeRepeat(schedule.repeat)}.`,
    "It starts without asking again, while Studio is open. Approvals an agent requests during the run still wait for you.",
    "",
    `Goal: ${schedule.goal}`,
    `Workspace: ${schedule.workspacePath}${schedule.worktree ? " (each run in its own git worktree)" : ""}`,
    `Primary: ${schedule.primaryRuntimeId}`,
    `Primary permission: ${schedule.runtimeSettings[schedule.primaryRuntimeId]?.permissionMode ?? DEFAULT_PERMISSION}`,
    `Reviewers: ${schedule.reviewerRuntimeIds.join(", ") || "none"}`,
    "Checks executed locally:",
    ...schedule.checks.map((check) => `  • ${check}`),
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

/** `work` must not reject; resolves null once `ms` passes. The timer is always cleared. */
async function withinBudget<T>(work: Promise<T>, ms: number): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([work, new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), ms); })]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

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
