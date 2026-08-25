import type { ChildProcessWithoutNullStreams, SpawnOptionsWithoutStdio } from "node:child_process";
import { spawnRuntime } from "./process.js";

export interface JsonLineDecoderOptions {
  readonly maxLineBytes?: number;
  readonly onMalformed?: (line: string, error: unknown) => void;
}

export class JsonLineDecoder {
  readonly #decoder = new TextDecoder();
  readonly #maxLineBytes: number;
  readonly #onMalformed: ((line: string, error: unknown) => void) | undefined;
  #buffer = "";

  constructor(options: JsonLineDecoderOptions = {}) {
    this.#maxLineBytes = options.maxLineBytes ?? 4 * 1024 * 1024;
    this.#onMalformed = options.onMalformed;
  }

  push(chunk: Uint8Array | string): unknown[] {
    this.#buffer += typeof chunk === "string" ? chunk : this.#decoder.decode(chunk, { stream: true });
    if (Buffer.byteLength(this.#buffer, "utf8") > this.#maxLineBytes) {
      const error = new Error(`JSONL line exceeded ${this.#maxLineBytes} bytes`);
      this.#onMalformed?.(this.#buffer.slice(0, 1000), error);
      this.#buffer = "";
      return [];
    }
    const lines = this.#buffer.split("\n");
    this.#buffer = lines.pop() ?? "";
    const values: unknown[] = [];
    for (const raw of lines) {
      const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
      if (!line.trim()) continue;
      try {
        values.push(JSON.parse(line));
      } catch (error) {
        this.#onMalformed?.(line, error);
      }
    }
    return values;
  }
}

export interface JsonlTransport {
  write(line: string): void;
  end(line?: string): void;
  kill(): void;
  onStdout(listener: (chunk: Uint8Array | string) => void): () => void;
  onStderr(listener: (chunk: Uint8Array | string) => void): () => void;
  onExit(listener: (code: number | null, signal: NodeJS.Signals | null) => void): () => void;
  onError(listener: (error: Error) => void): () => void;
}

export interface JsonlRpcPeerOptions {
  readonly requestTimeoutMs?: number;
  readonly stderrLimit?: number;
  readonly onProtocolError?: (error: Error, raw: unknown) => void;
}

interface PendingRequest {
  readonly method: string;
  readonly resolve: (value: unknown) => void;
  readonly reject: (error: Error) => void;
  readonly timeout: ReturnType<typeof setTimeout>;
  readonly removeAbort?: () => void;
}

type NotificationHandler = (method: string, params: unknown, raw: unknown) => void;
type RequestHandler = (params: unknown, raw: unknown) => unknown | Promise<unknown>;

export interface RpcPeer {
  request<T = unknown>(method: string, params?: unknown, options?: { signal?: AbortSignal; timeoutMs?: number }): Promise<T>;
  notify(method: string, params?: unknown): void;
  onNotification(handler: NotificationHandler): () => void;
  onRequest(method: string, handler: RequestHandler): () => void;
  stderr(): string;
  close(): void;
}

export class JsonlRpcPeer implements RpcPeer {
  readonly #transport: JsonlTransport;
  readonly #requestTimeoutMs: number;
  readonly #stderrLimit: number;
  readonly #onProtocolError: ((error: Error, raw: unknown) => void) | undefined;
  readonly #pending = new Map<number, PendingRequest>();
  readonly #notifications = new Set<NotificationHandler>();
  readonly #requests = new Map<string, RequestHandler>();
  readonly #dispose: Array<() => void> = [];
  #nextId = 1;
  #stderr = "";
  #closed = false;

  constructor(transport: JsonlTransport, options: JsonlRpcPeerOptions = {}) {
    this.#transport = transport;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? 30_000;
    this.#stderrLimit = options.stderrLimit ?? 64 * 1024;
    this.#onProtocolError = options.onProtocolError;
    const lines = new JsonLineDecoder({
      onMalformed: (line, cause) => this.#protocolError("Malformed JSONL message", line, cause),
    });
    this.#dispose.push(
      transport.onStdout((chunk) => {
        for (const message of lines.push(chunk)) this.#receive(message);
      }),
      transport.onStderr((chunk) => {
        this.#stderr = (this.#stderr + decodeChunk(chunk)).slice(-this.#stderrLimit);
      }),
      transport.onExit((code, signal) => this.#terminate(new Error(`Runtime process exited with code ${code ?? "null"}${signal ? ` (${signal})` : ""}`))),
      transport.onError((error) => this.#terminate(error)),
    );
  }

  request<T = unknown>(method: string, params?: unknown, options: { signal?: AbortSignal; timeoutMs?: number } = {}): Promise<T> {
    if (this.#closed) return Promise.reject(new Error("JSONL peer is closed"));
    const id = this.#nextId++;
    const timeoutMs = options.timeoutMs ?? this.#requestTimeoutMs;
    return new Promise<T>((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error(`${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      const onAbort = (): void => {
        const pending = this.#pending.get(id);
        if (!pending) return;
        clearTimeout(pending.timeout);
        this.#pending.delete(id);
        reject(new Error(`${method} was aborted`));
      };
      options.signal?.addEventListener("abort", onAbort, { once: true });
      this.#pending.set(id, {
        method,
        resolve: (value) => resolve(value as T),
        reject,
        timeout,
        ...(options.signal ? { removeAbort: () => options.signal?.removeEventListener("abort", onAbort) } : {}),
      });
      if (options.signal?.aborted) onAbort();
      else this.#send({ id, method, ...(params === undefined ? {} : { params }) });
    });
  }

  notify(method: string, params?: unknown): void {
    if (this.#closed) throw new Error("JSONL peer is closed");
    this.#send({ method, ...(params === undefined ? {} : { params }) });
  }

  onNotification(handler: NotificationHandler): () => void {
    this.#notifications.add(handler);
    return () => this.#notifications.delete(handler);
  }

  onRequest(method: string, handler: RequestHandler): () => void {
    if (this.#requests.has(method)) throw new Error(`request handler already registered: ${method}`);
    this.#requests.set(method, handler);
    return () => this.#requests.delete(method);
  }

  stderr(): string { return this.#stderr; }

  close(): void {
    if (this.#closed) return;
    this.#transport.kill();
    this.#terminate(new Error("JSONL peer closed"));
  }

  #receive(raw: unknown): void {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      this.#protocolError("JSONL message must be an object", raw);
      return;
    }
    const message = raw as { id?: unknown; method?: unknown; params?: unknown; result?: unknown; error?: unknown };
    if ((typeof message.id === "number") && !("method" in message)) {
      const pending = this.#pending.get(message.id);
      if (!pending) {
        this.#protocolError(`Response for unknown request ${message.id}`, raw);
        return;
      }
      this.#pending.delete(message.id);
      clearTimeout(pending.timeout);
      pending.removeAbort?.();
      if (message.error !== undefined) pending.reject(rpcError(pending.method, message.error));
      else pending.resolve(message.result);
      return;
    }
    if (typeof message.method !== "string") {
      this.#protocolError("JSONL message has no method or correlated response id", raw);
      return;
    }
    if (message.id !== undefined) {
      void this.#handleServerRequest(message.id, message.method, message.params, raw);
      return;
    }
    for (const handler of this.#notifications) handler(message.method, message.params, raw);
  }

  async #handleServerRequest(id: unknown, method: string, params: unknown, raw: unknown): Promise<void> {
    if (typeof id !== "number" && typeof id !== "string") {
      this.#protocolError("Server request id must be a string or number", raw);
      return;
    }
    const handler = this.#requests.get(method);
    if (!handler) {
      this.#send({ id, error: { code: -32601, message: `Method not supported by Firekeep Studio: ${method}` } });
      return;
    }
    try {
      this.#send({ id, result: await handler(params, raw) });
    } catch (error) {
      this.#send({ id, error: { code: -32000, message: error instanceof Error ? error.message : String(error) } });
    }
  }

  #send(message: unknown): void {
    this.#transport.write(`${JSON.stringify(message)}\n`);
  }

  #protocolError(message: string, raw: unknown, cause?: unknown): void {
    const error = new Error(message, cause === undefined ? undefined : { cause });
    this.#onProtocolError?.(error, raw);
  }

  #terminate(error: Error): void {
    if (this.#closed) return;
    this.#closed = true;
    for (const dispose of this.#dispose.splice(0)) dispose();
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timeout);
      pending.removeAbort?.();
      pending.reject(error);
    }
    this.#pending.clear();
  }
}

export class ChildProcessJsonlTransport implements JsonlTransport {
  readonly #child: ChildProcessWithoutNullStreams;

  constructor(command: string, args: readonly string[], options: SpawnOptionsWithoutStdio = {}) {
    this.#child = spawnRuntime(command, args, options);
  }

  write(line: string): void { this.#child.stdin.write(line); }
  end(line?: string): void { this.#child.stdin.end(line); }
  kill(): void { this.#child.kill(); }
  onStdout(listener: (chunk: Uint8Array | string) => void): () => void { this.#child.stdout.on("data", listener); return () => this.#child.stdout.off("data", listener); }
  onStderr(listener: (chunk: Uint8Array | string) => void): () => void { this.#child.stderr.on("data", listener); return () => this.#child.stderr.off("data", listener); }
  onExit(listener: (code: number | null, signal: NodeJS.Signals | null) => void): () => void { this.#child.on("exit", listener); return () => this.#child.off("exit", listener); }
  onError(listener: (error: Error) => void): () => void { this.#child.on("error", listener); return () => this.#child.off("error", listener); }
}

function decodeChunk(chunk: Uint8Array | string): string {
  return typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8");
}

function rpcError(method: string, value: unknown): Error {
  if (value && typeof value === "object" && "message" in value && typeof value.message === "string") {
    return new Error(`${method} failed: ${value.message}`);
  }
  return new Error(`${method} failed: ${JSON.stringify(value)}`);
}
