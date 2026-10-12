import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { JsonLineDecoder } from "../runtime/jsonl-rpc.js";
import { spawnRuntime, terminateProcessTree } from "../runtime/process.js";

export interface McpToolResult { readonly text: string; readonly isError: boolean }
export interface McpStdioClientOptions {
  readonly command: string;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
  readonly timeoutMs?: number;
  readonly onExit?: (code: number | null, stderrTail: string) => void;
}

interface Pending { resolve(value: unknown): void; reject(error: Error): void; timer: NodeJS.Timeout }

const PROTOCOL_VERSION = "2025-06-18";
const STDERR_TAIL = 4096;

/**
 * Just enough MCP (JSON-RPC 2.0 over newline-delimited stdio) to talk to
 * `firekeep gateway`. Hand-written rather than the SDK so the child is spawned
 * through spawnRuntime and killed with terminateProcessTree — the gateway's
 * shim grandchildren must die with it on Windows.
 */
export class McpStdioClient {
  readonly #child: ChildProcessWithoutNullStreams;
  readonly #timeoutMs: number;
  readonly #pending = new Map<number, Pending>();
  #nextId = 1;
  #stderr = "";
  #closed = false;
  #ready = false;   // onExit fires only for a gateway that finished initialize

  private constructor(child: ChildProcessWithoutNullStreams, options: McpStdioClientOptions) {
    this.#child = child;
    this.#timeoutMs = options.timeoutMs ?? 10_000;
    const decoder = new JsonLineDecoder();
    child.stdout.on("data", (chunk: Buffer) => { for (const message of decoder.push(chunk)) this.#receive(message); });
    child.stderr.on("data", (chunk: Buffer) => { this.#stderr = (this.#stderr + chunk.toString("utf8")).slice(-STDERR_TAIL); });
    child.on("close", (code) => {
      this.#closed = true;
      const tail = this.#stderr.trim().slice(-500);
      const error = new Error(`firekeep gateway exited (${code ?? "signal"})${tail ? `: ${tail}` : ""}`);
      for (const pending of this.#pending.values()) { clearTimeout(pending.timer); pending.reject(error); }
      this.#pending.clear();
      if (this.#ready) options.onExit?.(code, this.#stderr);
    });
    child.on("error", () => undefined);
    child.stdin.on("error", () => undefined);  // EPIPE after the child died; close handles the rejection
  }

  get closed(): boolean { return this.#closed; }

  static async connect(options: McpStdioClientOptions): Promise<McpStdioClient> {
    const child = spawnRuntime(options.command, options.args, { env: { ...process.env, ...options.env } });
    const client = new McpStdioClient(child, options);
    try {
      await client.#request("initialize", { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: { name: "firekeep-studio", version: "0.5.0" } });
      client.#notify("notifications/initialized");
    } catch (error) {
      await client.close();
      throw error;
    }
    client.#ready = true;
    return client;
  }

  async callTool(name: string, args: Readonly<Record<string, unknown>>): Promise<McpToolResult> {
    const result = await this.#request("tools/call", { name, arguments: args }) as { content?: Array<{ type?: string; text?: string }>; isError?: boolean };
    const text = (result.content ?? []).filter((part) => part.type === "text").map((part) => part.text ?? "").join("\n");
    return { text, isError: result.isError === true };
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    await terminateProcessTree(this.#child);
    this.#closed = true;
  }

  #request(method: string, params: unknown): Promise<unknown> {
    if (this.#closed) return Promise.reject(new Error("firekeep gateway exited"));
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error(`firekeep gateway call ${method} timed out after ${this.#timeoutMs} ms`));
      }, this.#timeoutMs);
      this.#pending.set(id, { resolve, reject, timer });
      this.#child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  #notify(method: string): void {
    this.#child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method })}\n`);
  }

  #receive(message: unknown): void {
    if (!message || typeof message !== "object") return;
    const { id, result, error } = message as { id?: unknown; result?: unknown; error?: { message?: string } };
    if (typeof id !== "number") return;  // notifications and server requests are ignored
    const pending = this.#pending.get(id);
    if (!pending) return;
    this.#pending.delete(id);
    clearTimeout(pending.timer);
    if (error) pending.reject(new Error(error.message ?? "MCP error"));
    else pending.resolve(result ?? {});
  }
}
