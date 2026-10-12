import { PANEL_AGENT_SUFFIX, parseTimelineReply, type TimelinePage } from "../../core/keep/receipt.js";
import type { KeepTimelineSource } from "../../core/keep/receipt-tracker.js";
import type { KeepHighlights, KeepItem, KeepStatus } from "../../shared/keep.js";
import type { McpToolResult } from "./mcp-stdio-client.js";

export interface McpLike {
  callTool(name: string, args: Readonly<Record<string, unknown>>): Promise<McpToolResult>;
  close(): Promise<void>;
  readonly closed: boolean;
}

export interface KeepGatewayDeps {
  readonly command: string;
  readonly whoami: () => Promise<{ readonly ok: boolean; readonly output: string; readonly missing: boolean }>;
  readonly connect: (env: Readonly<Record<string, string>>) => Promise<McpLike>;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly onStatus?: (status: KeepStatus) => void;
}

/** Exactly the kit's `studio` preset (client 1.8.0). */
export const STUDIO_TOOLS: readonly string[] = [
  "memory_recall", "memory_feedback", "skill_list", "skill_recall",
  "relay_who_is_online", "relay_read", "ctx_list_sessions", "replay_timeline", "corpus_sources",
];
export const BACKOFF_MS: readonly number[] = [1000, 5000, 30_000];
/** Tools whose `agent_id` is an attribution label. ctx_list_sessions treats it as a FILTER; others lack it. */
const TAGGED_TOOLS = new Set(["memory_recall", "memory_feedback"]);

export class KeepGateway implements KeepTimelineSource {
  readonly #deps: KeepGatewayDeps;
  #status: KeepStatus = { state: "starting" };
  #mcp: McpLike | null = null;
  #label = "";
  #env: Record<string, string> = {};
  #stopped = false;

  constructor(deps: KeepGatewayDeps) {
    this.#deps = deps;
  }

  status(): KeepStatus { return this.#status; }

  async start(): Promise<KeepStatus> {
    if (this.#stopped) return this.#status;
    const who = await this.#deps.whoami().catch(() => ({ ok: false, output: "", missing: true }));
    if (who.missing) return this.#set({ state: "missing" });
    const facts = parseWhoami(who.output);
    if (!who.ok || !facts?.agent_id) {
      const detail = facts ? "This machine has no Firekeep agent identity yet. Run firekeep doctor." : "Update the Firekeep kit (firekeep update) to connect Studio.";
      return this.#set({ state: "unconfigured", detail });
    }
    this.#label = `${facts.agent_id}${PANEL_AGENT_SUFFIX}`;
    this.#env = { FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: this.#label };
    return this.#connectWithBackoff({ agentId: facts.agent_id, personalMode: facts.personal_mode === true, kitVersion: String(facts.kit_version ?? "") });
  }

  async retry(): Promise<KeepStatus> { return this.start(); }

  handleExit(): void {
    this.#mcp = null;
    if (!this.#stopped) void this.start();
  }

  async stop(): Promise<void> {
    this.#stopped = true;   // start()/handleExit() become no-ops; quit cannot respawn the gateway
    await this.#mcp?.close().catch(() => undefined);
    this.#mcp = null;
  }

  async search(query: string, kind: "memory" | "skill"): Promise<readonly KeepItem[]> {
    const text = kind === "memory"
      ? await this.#call("memory_recall", { task: query, format: "raw", top_k: 8 })
      : await this.#call("skill_recall", { task: query });
    return splitNumberedItems(text);
  }

  async skills(): Promise<readonly KeepItem[]> {
    return splitNumberedItems(await this.#call("skill_list", { status: "active", limit: 25 }));
  }

  async team(): Promise<string> { return this.#call("relay_who_is_online", { include_idle: true }); }

  async highlights(): Promise<KeepHighlights> {
    const [online, bulletin, sessions] = await Promise.all([
      this.#call("relay_who_is_online", { include_idle: false }),
      this.#call("relay_read", { limit: 3 }),
      this.#call("ctx_list_sessions", { status: "paused", limit: 5 }),
    ]);
    return { online, bulletin, paused: parsePausedSessions(sessions) };
  }

  async feedback(memoryIds: readonly string[], useful: boolean, comment?: string): Promise<void> {
    await this.#call("memory_feedback", { memory_ids: [...memoryIds], useful, ...(comment ? { comment } : {}) });
  }

  async timeline(keepSessionId: string, offset: number, limit: number): Promise<TimelinePage> {
    try {
      return parseTimelineReply(await this.#call("replay_timeline", { session_id: keepSessionId, offset, limit, format: "json" }));
    } catch (error) {
      return { kind: "error", message: error instanceof Error ? error.message : String(error) };
    }
  }

  async #call(name: string, args: Record<string, unknown>): Promise<string> {
    if (!STUDIO_TOOLS.includes(name)) throw new Error(`tool outside the studio preset: ${name}`);
    if (!this.#mcp || this.#mcp.closed) throw new Error("Keep not connected");
    const result = await this.#mcp.callTool(name, TAGGED_TOOLS.has(name) ? { ...args, agent_id: this.#label } : args);
    if (result.isError) throw new Error(result.text.split("\n")[0] || `${name} failed`);
    return result.text;
  }

  async #connectWithBackoff(facts: { agentId: string; personalMode: boolean; kitVersion: string }): Promise<KeepStatus> {
    const sleep = this.#deps.sleep ?? ((ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    let lastError = "";
    for (let attempt = 0; attempt <= BACKOFF_MS.length; attempt += 1) {
      if (this.#stopped) break;
      try {
        this.#mcp = await this.#connectOnce();
        return this.#set({ state: "connected", ...facts });
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error);
        const delay = BACKOFF_MS[attempt];
        this.#set({ state: "unreachable", detail: firstLine(lastError), retryInMs: delay ?? null });
        if (delay === undefined) break;
        await sleep(delay);
      }
    }
    return this.#set({ state: "unreachable", detail: firstLine(lastError), retryInMs: null });
  }

  async #connectOnce(): Promise<McpLike> {
    try {
      return await this.#deps.connect(this.#env);
    } catch (error) {
      if (!/unknown FIREKEEP_TOOLSET/.test(String(error))) throw error;
      // Kit older than 1.8.0: same tools, named explicitly.
      this.#env = { FIREKEEP_TOOLS_ALLOW: STUDIO_TOOLS.join(","), FIREKEEP_AGENT_ID: this.#label };
      return this.#deps.connect(this.#env);
    }
  }

  #set(status: KeepStatus): KeepStatus {
    this.#status = status;
    this.#deps.onStatus?.(status);
    return status;
  }
}

export function splitNumberedItems(text: string): KeepItem[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  const items: KeepItem[] = [];
  let current: string[] | null = null;
  for (const line of trimmed.split(/\r?\n/)) {
    const match = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (match) { if (current) items.push(toItem(current)); current = [match[1] ?? ""]; }
    else if (current) current.push(line);
  }
  if (current) items.push(toItem(current));
  return items.length ? items : [{ title: firstLine(trimmed), body: trimmed }];
}

function toItem(lines: readonly string[]): KeepItem {
  return { title: firstLine(lines[0] ?? ""), body: lines.join("\n").trim() };
}

function firstLine(text: string): string {
  return text.trim().split(/\r?\n/)[0]?.slice(0, 160) ?? "";
}

function parseWhoami(output: string): { agent_id?: string | null; kit_version?: unknown; personal_mode?: unknown } | null {
  try {
    const line = output.split(/\r?\n/).map((l) => l.trim()).reverse().find((l) => l.startsWith("{"));
    const value = JSON.parse(line ?? "");
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

/** ctx_list_sessions returns {"sessions": [{session_id, goal, status, ...}]} (bridge/app/mcp_server.py), serialized as JSON text. */
export function parsePausedSessions(text: string): KeepHighlights["paused"] {
  try {
    const value: unknown = JSON.parse(text);
    const sessions = value && typeof value === "object" ? (value as { sessions?: unknown }).sessions : undefined;
    if (!Array.isArray(sessions)) return [];
    const rows: Array<{ sessionId: string; goal: string }> = [];
    for (const entry of sessions) {
      if (!entry || typeof entry !== "object") continue;
      const { session_id: id, goal } = entry as { session_id?: unknown; goal?: unknown };
      if (typeof id === "string" && id) rows.push({ sessionId: id, goal: typeof goal === "string" ? goal : "" });
    }
    return rows.slice(0, 5);
  } catch {
    return [];
  }
}
