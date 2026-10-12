import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

export interface SettingsStore<T> {
  load(): Promise<T | null>;
  save(value: T): Promise<void>;
}

export interface JsonSettingsStoreOptions<T> {
  readonly validate?: (value: unknown) => T;
  readonly onWarning?: (message: string, error: unknown) => void;
}

/** A serialized, atomic JSON file store. Corrupt input is preserved and ignored. */
export class JsonSettingsStore<T> implements SettingsStore<T> {
  readonly #path: string;
  readonly #options: JsonSettingsStoreOptions<T>;
  #pending: Promise<void> = Promise.resolve();

  constructor(path: string, options: JsonSettingsStoreOptions<T> = {}) {
    this.#path = path;
    this.#options = options;
  }

  async load(): Promise<T | null> {
    await this.#pending;
    try {
      const parsed: unknown = JSON.parse(await readFile(this.#path, "utf8"));
      return this.#options.validate ? this.#options.validate(parsed) : parsed as T;
    } catch (error) {
      if (isMissingFile(error)) return null;
      this.#options.onWarning?.(`Ignoring unreadable Studio state at ${this.#path}`, error);
      return null;
    }
  }

  async save(value: T): Promise<void> {
    const snapshot = structuredClone(value);
    const operation = this.#pending.then(async () => {
      await mkdir(dirname(this.#path), { recursive: true });
      const temporary = `${this.#path}.${process.pid}.${crypto.randomUUID()}.tmp`;
      await writeFile(temporary, `${JSON.stringify(snapshot, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
      await rename(temporary, this.#path);
    });
    this.#pending = operation.catch(() => undefined);
    await operation;
  }
}

function isMissingFile(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "ENOENT";
}

export class MemorySettingsStore<T = unknown> implements SettingsStore<T> {
  value: T | null = null;

  async load(): Promise<T | null> {
    return this.value === null ? null : structuredClone(this.value);
  }

  async save(value: T): Promise<void> {
    this.value = structuredClone(value);
  }
}
