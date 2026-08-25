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

export interface SecretStore {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  delete(key: string): Promise<void>;
  list(): Promise<string[]>;
}

export interface SecretCodec {
  available(): boolean;
  encrypt(value: string): Uint8Array;
  decrypt(value: Uint8Array): string;
}

interface SecretFile {
  readonly version: 1;
  readonly values: Readonly<Record<string, string>>;
}

export class EncryptedFileSecretStore implements SecretStore {
  readonly #file: JsonSettingsStore<SecretFile>;
  readonly #codec: SecretCodec;
  #pending: Promise<void> = Promise.resolve();

  constructor(path: string, codec: SecretCodec, onWarning?: (message: string, error: unknown) => void) {
    this.#codec = codec;
    this.#file = new JsonSettingsStore(path, {
      validate: parseSecretFile,
      ...(onWarning ? { onWarning } : {}),
    });
  }

  async get(key: string): Promise<string | null> {
    validateSecretKey(key);
    await this.#pending;
    const value = (await this.#file.load())?.values[key];
    if (!value) return null;
    this.#assertAvailable();
    return this.#codec.decrypt(Buffer.from(value, "base64"));
  }

  async set(key: string, value: string): Promise<void> {
    validateSecretKey(key);
    if (!value) throw new Error("secret value cannot be empty");
    this.#assertAvailable();
    await this.#mutate((current) => ({
      ...current,
      [key]: Buffer.from(this.#codec.encrypt(value)).toString("base64"),
    }));
  }

  async delete(key: string): Promise<void> {
    validateSecretKey(key);
    await this.#mutate((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  async list(): Promise<string[]> {
    await this.#pending;
    return Object.keys((await this.#file.load())?.values ?? {}).sort();
  }

  async #mutate(update: (values: Readonly<Record<string, string>>) => Record<string, string>): Promise<void> {
    const operation = this.#pending.then(async () => {
      const current = (await this.#file.load())?.values ?? {};
      await this.#file.save({ version: 1, values: update(current) });
    });
    this.#pending = operation.catch(() => undefined);
    await operation;
  }

  #assertAvailable(): void {
    if (!this.#codec.available()) {
      throw new Error("Operating-system credential encryption is unavailable; Studio will not store this secret");
    }
  }
}

export class MemorySecretStore implements SecretStore {
  readonly #values = new Map<string, string>();
  async get(key: string): Promise<string | null> { return this.#values.get(key) ?? null; }
  async set(key: string, value: string): Promise<void> { this.#values.set(key, value); }
  async delete(key: string): Promise<void> { this.#values.delete(key); }
  async list(): Promise<string[]> { return [...this.#values.keys()].sort(); }
}

function parseSecretFile(value: unknown): SecretFile {
  if (!value || typeof value !== "object") throw new Error("secret store must be an object");
  const candidate = value as { version?: unknown; values?: unknown };
  if (candidate.version !== 1 || !candidate.values || typeof candidate.values !== "object" || Array.isArray(candidate.values)) {
    throw new Error("unsupported secret store format");
  }
  const values: Record<string, string> = {};
  for (const [key, encrypted] of Object.entries(candidate.values)) {
    validateSecretKey(key);
    if (typeof encrypted !== "string") throw new Error(`invalid encrypted secret: ${key}`);
    values[key] = encrypted;
  }
  return { version: 1, values };
}

function validateSecretKey(key: string): void {
  if (!/^[a-z0-9][a-z0-9._-]{0,127}$/i.test(key)) throw new Error(`invalid secret key: ${key}`);
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
