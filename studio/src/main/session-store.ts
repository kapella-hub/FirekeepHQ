import { appendFile, mkdir, readFile, unlink } from "node:fs/promises";
import { join } from "node:path";
import { parseMissionSnapshot, type MissionSnapshot } from "../core/mission.js";
import type { RuntimeEvent } from "../core/runtime.js";
import { DEFAULT_SESSION_COLOR, isSessionColor, normalizeSessionMetadata, type SessionMetadataUpdate, type SessionStore, type StudioSessionSummary } from "../core/session-store.js";
import { JsonSettingsStore } from "../core/settings-store.js";

interface SessionIndex { readonly version: 1; readonly sessions: Readonly<Record<string, StudioSessionSummary>> }

export class JsonlSessionStore implements SessionStore {
  readonly #root: string;
  readonly #index: JsonSettingsStore<SessionIndex>;
  readonly #warning: ((message: string, error: unknown) => void) | undefined;
  #pending: Promise<void> = Promise.resolve();

  constructor(root: string, warning?: (message: string, error: unknown) => void) {
    this.#root = root;
    this.#warning = warning;
    this.#index = new JsonSettingsStore(join(root, "index.json"), {
      validate: parseIndex,
      ...(warning ? { onWarning: warning } : {}),
    });
  }

  async ensure(id: string, createdAt: string): Promise<void> {
    validateId(id);
    await this.#mutateIndex((sessions) => sessions[id] ? sessions : {
      ...sessions,
      [id]: { id, name: defaultName(createdAt), color: DEFAULT_SESSION_COLOR, createdAt, updatedAt: createdAt, eventCount: 0, nativeSessionIds: {} },
    });
  }

  async append(event: RuntimeEvent): Promise<void> {
    validateId(event.studioSessionId);
    await this.#enqueue(async () => {
      await mkdir(this.#root, { recursive: true });
      await appendFile(this.#eventsPath(event.studioSessionId), `${JSON.stringify(event)}\n`, { encoding: "utf8", mode: 0o600 });
      await this.#writeIndex((sessions) => {
        const current = sessions[event.studioSessionId] ?? {
          id: event.studioSessionId,
          name: defaultName(event.timestamp),
          color: DEFAULT_SESSION_COLOR,
          createdAt: event.timestamp,
          updatedAt: event.timestamp,
          eventCount: 0,
          nativeSessionIds: {},
        };
        return { ...sessions, [event.studioSessionId]: { ...current, updatedAt: event.timestamp, eventCount: current.eventCount + 1 } };
      });
    });
  }

  async load(id: string): Promise<RuntimeEvent[]> {
    validateId(id);
    await this.#pending;
    let raw: string;
    try { raw = await readFile(this.#eventsPath(id), "utf8"); }
    catch (error) {
      if (isMissing(error)) return [];
      throw error;
    }
    const events: RuntimeEvent[] = [];
    for (const line of raw.split(/\r?\n/)) {
      if (!line) continue;
      try { events.push(JSON.parse(line) as RuntimeEvent); }
      catch (error) { this.#warning?.(`Ignoring malformed event in session ${id}`, error); }
    }
    return events;
  }

  async list(): Promise<StudioSessionSummary[]> {
    await this.#pending;
    const index = await this.#index.load();
    return Object.values(index?.sessions ?? {}).sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  }

  async updateMetadata(id: string, update: SessionMetadataUpdate): Promise<void> {
    validateId(id);
    const clean = normalizeSessionMetadata(update);
    await this.#mutateIndex((sessions) => {
      const current = sessions[id];
      if (!current) throw new Error(`unknown session: ${id}`);
      return { ...sessions, [id]: { ...current, ...clean } };
    });
  }

  async remove(id: string): Promise<void> {
    validateId(id);
    await this.#enqueue(async () => {
      const current = await this.#index.load();
      if (!current?.sessions[id]) throw new Error(`unknown session: ${id}`);
      try { await unlink(this.#eventsPath(id)); }
      catch (error) { if (!isMissing(error)) throw error; }
      await this.#writeIndex((sessions) => {
        const next = { ...sessions };
        delete next[id];
        return next;
      });
    });
  }

  async setNativeSessionIds(id: string, values: Readonly<Record<string, string>>): Promise<void> {
    validateId(id);
    await this.#mutateIndex((sessions) => {
      const current = sessions[id];
      if (!current) throw new Error(`unknown session: ${id}`);
      return { ...sessions, [id]: { ...current, nativeSessionIds: { ...values } } };
    });
  }

  async setMission(id: string, mission: MissionSnapshot | null): Promise<void> {
    validateId(id);
    await this.#mutateIndex((sessions) => {
      const current = sessions[id];
      if (!current) throw new Error(`unknown session: ${id}`);
      if (mission) return { ...sessions, [id]: { ...current, updatedAt: mission.updatedAt, mission: structuredClone(mission) } };
      const { mission: _removed, ...next } = current;
      return { ...sessions, [id]: next };
    });
  }

  async flush(): Promise<void> { await this.#pending; }

  async #mutateIndex(update: (sessions: Readonly<Record<string, StudioSessionSummary>>) => Readonly<Record<string, StudioSessionSummary>>): Promise<void> {
    await this.#enqueue(() => this.#writeIndex(update));
  }

  async #writeIndex(update: (sessions: Readonly<Record<string, StudioSessionSummary>>) => Readonly<Record<string, StudioSessionSummary>>): Promise<void> {
    const current = await this.#index.load();
    await this.#index.save({ version: 1, sessions: update(current?.sessions ?? {}) });
  }

  async #enqueue(operation: () => Promise<void>): Promise<void> {
    const next = this.#pending.then(operation);
    this.#pending = next.catch(() => undefined);
    await next;
  }

  #eventsPath(id: string): string { return join(this.#root, `${id}.jsonl`); }
}

function parseIndex(value: unknown): SessionIndex {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("session index must be an object");
  const source = value as { version?: unknown; sessions?: unknown };
  if (source.version !== 1 || !source.sessions || typeof source.sessions !== "object" || Array.isArray(source.sessions)) throw new Error("unsupported session index");
  const sessions: Record<string, StudioSessionSummary> = {};
  for (const [id, raw] of Object.entries(source.sessions)) {
    validateId(id);
    if (!raw || typeof raw !== "object") continue;
    const item = raw as Partial<StudioSessionSummary>;
    if (typeof item.name !== "string" || typeof item.createdAt !== "string" || typeof item.updatedAt !== "string" || typeof item.eventCount !== "number") continue;
    const nativeSessionIds = item.nativeSessionIds && typeof item.nativeSessionIds === "object" ? Object.fromEntries(Object.entries(item.nativeSessionIds).filter((entry): entry is [string, string] => typeof entry[1] === "string")) : {};
    const mission = parseMissionSnapshot(item.mission);
    sessions[id] = {
      id,
      name: item.name,
      color: isSessionColor(item.color) ? item.color : DEFAULT_SESSION_COLOR,
      createdAt: item.createdAt,
      updatedAt: item.updatedAt,
      eventCount: item.eventCount,
      nativeSessionIds,
      ...(mission ? { mission } : {}),
    };
  }
  return { version: 1, sessions };
}

function validateId(id: string): void { if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(id)) throw new Error(`invalid session id: ${id}`); }
function defaultName(timestamp: string): string { const date = new Date(timestamp); return Number.isNaN(date.getTime()) ? "New session" : `Session ${date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`; }
function isMissing(error: unknown): boolean { return error instanceof Error && "code" in error && error.code === "ENOENT"; }
