import type { RuntimeEvent } from "./runtime.js";
import type { MissionSnapshot } from "./mission.js";

export interface StudioSessionSummary {
  readonly id: string;
  readonly name: string;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly eventCount: number;
  readonly nativeSessionIds: Readonly<Record<string, string>>;
  readonly mission?: MissionSnapshot;
}

export interface SessionStore {
  ensure(id: string, createdAt: string): Promise<void>;
  append(event: RuntimeEvent): Promise<void>;
  load(id: string): Promise<RuntimeEvent[]>;
  list(): Promise<StudioSessionSummary[]>;
  rename(id: string, name: string): Promise<void>;
  remove(id: string): Promise<void>;
  setNativeSessionIds(id: string, values: Readonly<Record<string, string>>): Promise<void>;
  setMission(id: string, mission: MissionSnapshot | null): Promise<void>;
  flush(): Promise<void>;
}

export class MemorySessionStore implements SessionStore {
  readonly #summaries = new Map<string, StudioSessionSummary>();
  readonly #events = new Map<string, RuntimeEvent[]>();

  async ensure(id: string, createdAt: string): Promise<void> {
    if (this.#summaries.has(id)) return;
    this.#summaries.set(id, { id, name: "New session", createdAt, updatedAt: createdAt, eventCount: 0, nativeSessionIds: {} });
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
  async rename(id: string, name: string): Promise<void> { const current = this.#require(id); this.#summaries.set(id, { ...current, name }); }
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
  async flush(): Promise<void> {}
  #require(id: string): StudioSessionSummary { const value = this.#summaries.get(id); if (!value) throw new Error(`unknown session: ${id}`); return value; }
}
