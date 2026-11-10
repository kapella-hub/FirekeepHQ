import type { RuntimeEvent } from "./runtime.js";
import type { MissionSnapshot } from "./mission.js";

export const SESSION_COLORS = ["ember", "gold", "moss", "teal", "ocean", "violet", "rose", "slate"] as const;
export type SessionColor = typeof SESSION_COLORS[number];
export const DEFAULT_SESSION_COLOR: SessionColor = "ember";

export interface SessionMetadataUpdate {
  readonly name?: string;
  readonly color?: SessionColor;
}

/** A git worktree Studio created for one session, so its agent edits files no other session touches. */
export interface SessionWorktree {
  /** The folder the session's agents work in: the workspace's own subfolder inside the worktree. */
  readonly path: string;
  /** The worktree's root folder. */
  readonly root: string;
  readonly branch: string;
  /** The repository the worktree was created from. */
  readonly source: string;
}

export interface WorktreeManager {
  create(sessionId: string, workspacePath: string): Promise<SessionWorktree>;
  /** Removes a clean worktree. Returns null when nothing is left behind, otherwise one sentence saying what stays and where. */
  remove(worktree: SessionWorktree): Promise<string | null>;
  exists(worktree: SessionWorktree): Promise<boolean>;
}

export function parseSessionWorktree(value: unknown): SessionWorktree | null {
  if (!value || typeof value !== "object") return null;
  const { path, root, branch, source } = value as Record<string, unknown>;
  if (typeof path !== "string" || typeof root !== "string" || typeof branch !== "string" || typeof source !== "string") return null;
  if (!path || !root || !branch || !source) return null;
  return { path, root, branch, source };
}

export interface StudioSessionSummary {
  readonly id: string;
  readonly name: string;
  readonly color: SessionColor;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly eventCount: number;
  readonly nativeSessionIds: Readonly<Record<string, string>>;
  readonly mission?: MissionSnapshot;
  /** Present when the session works in its own git worktree instead of the shared workspace folder. */
  readonly worktree?: SessionWorktree;
}

export interface SessionStore {
  ensure(id: string, createdAt: string): Promise<void>;
  append(event: RuntimeEvent): Promise<void>;
  load(id: string): Promise<RuntimeEvent[]>;
  list(): Promise<StudioSessionSummary[]>;
  updateMetadata(id: string, update: SessionMetadataUpdate): Promise<void>;
  remove(id: string): Promise<void>;
  setNativeSessionIds(id: string, values: Readonly<Record<string, string>>): Promise<void>;
  setMission(id: string, mission: MissionSnapshot | null): Promise<void>;
  setWorktree(id: string, worktree: SessionWorktree): Promise<void>;
  flush(): Promise<void>;
}

export class MemorySessionStore implements SessionStore {
  readonly #summaries = new Map<string, StudioSessionSummary>();
  readonly #events = new Map<string, RuntimeEvent[]>();

  async ensure(id: string, createdAt: string): Promise<void> {
    if (this.#summaries.has(id)) return;
    this.#summaries.set(id, { id, name: "New session", color: DEFAULT_SESSION_COLOR, createdAt, updatedAt: createdAt, eventCount: 0, nativeSessionIds: {} });
    this.#events.set(id, []);
  }
  async append(event: RuntimeEvent): Promise<void> {
    await this.ensure(event.studioSessionId, event.timestamp);
    const values = this.#events.get(event.studioSessionId) as RuntimeEvent[];
    values.push(structuredClone(event));
    const current = this.#summaries.get(event.studioSessionId) as StudioSessionSummary;
    this.#summaries.set(event.studioSessionId, { ...current, updatedAt: event.timestamp, eventCount: values.length });
  }
  async load(id: string): Promise<RuntimeEvent[]> { return structuredClone(this.#events.get(id) ?? []); }
  async list(): Promise<StudioSessionSummary[]> { return structuredClone([...this.#summaries.values()].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))); }
  async updateMetadata(id: string, update: SessionMetadataUpdate): Promise<void> {
    const current = this.#require(id);
    this.#summaries.set(id, { ...current, ...normalizeSessionMetadata(update) });
  }
  async remove(id: string): Promise<void> { this.#require(id); this.#summaries.delete(id); this.#events.delete(id); }
  async setNativeSessionIds(id: string, values: Readonly<Record<string, string>>): Promise<void> { const current = this.#require(id); this.#summaries.set(id, { ...current, nativeSessionIds: { ...values } }); }
  async setMission(id: string, mission: MissionSnapshot | null): Promise<void> {
    const current = this.#require(id);
    if (mission) this.#summaries.set(id, { ...current, updatedAt: mission.updatedAt, mission: structuredClone(mission) });
    else {
      const { mission: _removed, ...next } = current;
      this.#summaries.set(id, next);
    }
  }
  async setWorktree(id: string, worktree: SessionWorktree): Promise<void> { const current = this.#require(id); this.#summaries.set(id, { ...current, worktree: { ...worktree } }); }
  async flush(): Promise<void> {}
  #require(id: string): StudioSessionSummary { const value = this.#summaries.get(id); if (!value) throw new Error(`unknown session: ${id}`); return value; }
}

export function isSessionColor(value: unknown): value is SessionColor {
  return typeof value === "string" && (SESSION_COLORS as readonly string[]).includes(value);
}

export function normalizeSessionMetadata(update: SessionMetadataUpdate): SessionMetadataUpdate {
  const next: { name?: string; color?: SessionColor } = {};
  if (update.name !== undefined) {
    const name = update.name.trim().slice(0, 120);
    if (!name) throw new Error("session name cannot be empty");
    next.name = name;
  }
  if (update.color !== undefined) {
    if (!isSessionColor(update.color)) throw new Error(`invalid session color: ${String(update.color)}`);
    next.color = update.color;
  }
  if (next.name === undefined && next.color === undefined) throw new Error("session metadata update is empty");
  return next;
}
