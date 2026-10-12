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
  readonly whoami: () => Promise<{ readonly ok: boolean; readonly output: string; readonly missing: boolean }>;
  /** Spawn the gateway. The wrapper must call `gateway.handleExit(client)` (with the client it returned) from McpStdioClient's onExit. */
  readonly connect: (env: Readonly<Record<string, string>>) => Promise<McpLike>;
  readonly now?: () => number;
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

/** Gateway must stay up this long before the consecutive-failure counter resets. */
export const STABLE_MS = 60_000;
/** Consecutive failed tool calls after which a "connected" Keep is reported unreachable and reconnected. */
export const CALL_FAILURE_LIMIT = 3;
/** A pre-v1.6.2 server rejects the new replay_timeline arguments: the server answered, the feature is missing. */
const OLD_TIMELINE = /validation errors? for call\[replay_timeline\]/i;

export class KeepGateway implements KeepTimelineSource {
  readonly #deps: KeepGatewayDeps;
  #status: KeepStatus = { state: "starting" };
  #mcp: McpLike | null = null;
  #label = "";
  #env: Record<string, string> = {};
  #stopped = false;
  #starting: Promise<KeepStatus> | null = null;
  #failures = 0;
  #connectedAt = 0;
  #callFailures = 0;

  constructor(deps: KeepGatewayDeps) {
    this.#deps = deps;
  }

  status(): KeepStatus { return this.#status; }

  /** Single-flight: a call while another start/retry/reconnect runs shares its promise. */
  start(): Promise<KeepStatus> {
    if (this.#stopped) return Promise.resolve(this.#status);
    if (this.#starting) return this.#starting;
    this.#failures = 0;
    return this.#track(this.#run());
  }

  retry(): Promise<KeepStatus> { return this.start(); }

  /**
   * Respawns the gateway after a kit command re-pointed it (`/firekeep connect`): the agent label is a
   * spawn-time env var, so refreshIdentity() is not enough. Waits for any in-flight start, then re-runs
   * whoami and connect (#run closes the old child). A no-op once stop() has run (app quit).
   */
  async restart(): Promise<KeepStatus> {
    if (this.#stopped) return this.#status;
    await this.#starting?.catch(() => undefined);
    if (this.#stopped) return this.#status;
    this.#set({ state: "starting" });
    return this.start();
  }

  /**
   * Re-reads the kit identity (personal mode, kit version) after a kit command changed it, and pushes the new
   * status. The gateway child is never restarted. A probe that fails, or an identity that no longer parses,
   * keeps the current status: a flaky probe must not flip a working connection to an error.
   */
  async refreshIdentity(): Promise<KeepStatus> {
    const current = this.#status;
    if (current.state !== "connected" || this.#stopped) return current;
    const who = await this.#deps.whoami().catch(() => null);
    const facts = who?.ok ? parseWhoami(who.output) : null;
    if (!facts?.agent_id || this.#stopped || this.#status !== current) return this.#status;
    const personalMode = facts.personal_mode === true;
    const kitVersion = String(facts.kit_version ?? "");
    if (personalMode === current.personalMode && kitVersion === current.kitVersion && facts.agent_id === current.agentId) return current;
    return this.#set({ ...current, agentId: facts.agent_id, personalMode, kitVersion });
  }

  /**
   * The gateway process behind `client` exited. Wiring: pass the client whose onExit fired;
   * an exit from a client that is no longer current is ignored.
   */
  handleExit(client: McpLike): void {
    if (client !== this.#mcp) return;
    this.#mcp = null;
    if (this.#starting) return;   // stop() nulls #mcp first, so a stopped gateway never gets here
    if (this.#now() - this.#connectedAt >= STABLE_MS) this.#failures = 0;
    this.#track(this.#reconnect());
  }

  async stop(): Promise<void> {
    this.#stopped = true;   // start()/handleExit() become no-ops; quit cannot respawn the gateway
    const mcp = this.#mcp;
    this.#mcp = null;
    await mcp?.close().catch(() => undefined);
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
      const message = error instanceof Error ? error.message : String(error);
      // A pre-v1.6.2 server rejects the new offset/format arguments with a pydantic validation error
      // instead of replying; that means "timeline not supported here", not a failure.
      if (OLD_TIMELINE.test(message)) return { kind: "unsupported" };
      return { kind: "error", message };
    }
  }

  async #call(name: string, args: Record<string, unknown>): Promise<string> {
    if (!STUDIO_TOOLS.includes(name)) throw new Error(`tool outside the studio preset: ${name}`);
    const mcp = this.#mcp;
    if (!mcp || mcp.closed) throw new Error("Keep not connected");
    let result: McpToolResult;
    try {
      result = await mcp.callTool(name, TAGGED_TOOLS.has(name) ? { ...args, agent_id: this.#label } : args);
    } catch (error) {
      this.#callFailed(mcp, error instanceof Error ? error.message : String(error));
      throw error;
    }
    if (result.isError) {
      // A tool error (a refused feedback, a bad argument) proves the server answered: it never counts toward demotion.
      this.#callFailures = 0;
      throw new Error(result.text.split("\n")[0] || `${name} failed`);
    }
    this.#callFailures = 0;
    return result.text;
  }

  /**
   * The kit gateway answers locally, so a server that dies after connect shows up only as failing calls: the kit
   * turns an unreachable backend into a JSON-RPC error, which rejects callTool (as do timeouts and a dead child).
   * After CALL_FAILURE_LIMIT such rejections in a row, report unreachable and reconnect through the normal backoff.
   */
  #callFailed(mcp: McpLike, message: string): void {
    if (mcp !== this.#mcp) return;   // a call on a client that has since been replaced
    this.#callFailures += 1;
    if (this.#callFailures < CALL_FAILURE_LIMIT || this.#status.state !== "connected" || this.#starting || this.#stopped) return;
    this.#callFailures = 0;
    if (this.#now() - this.#connectedAt >= STABLE_MS) this.#failures = 0;
    this.#track(this.#reconnect(firstLine(message) || "Keep calls are failing"));
  }

  /** One cheap call proves the server behind the kit answers; the kit itself answers initialize locally. */
  async #probe(client: McpLike): Promise<void> {
    const result = await client.callTool("relay_who_is_online", { include_idle: false });
    if (result.isError) throw new Error(firstLine(result.text) || "relay_who_is_online failed");
  }

  #now(): number { return (this.#deps.now ?? Date.now)(); }

  #track(promise: Promise<KeepStatus>): Promise<KeepStatus> {
    const tracked = promise.finally(() => { if (this.#starting === tracked) this.#starting = null; });
    this.#starting = tracked;
    return tracked;
  }

  async #sleep(ms: number): Promise<void> {
    await (this.#deps.sleep ?? ((n) => new Promise<void>((resolve) => setTimeout(resolve, n))))(ms);
  }

  /** Unexpected exit or failing calls: report, wait the next backoff step, then reconnect (or give up to manual retry). */
  async #reconnect(detail = "Keep gateway exited"): Promise<KeepStatus> {
    const delay = BACKOFF_MS[this.#failures];
    this.#failures += 1;
    if (delay === undefined) return this.#set({ state: "unreachable", detail, retryInMs: null });
    this.#set({ state: "unreachable", detail, retryInMs: delay });
    await this.#sleep(delay);
    return this.#run();
  }

  async #run(): Promise<KeepStatus> {
    if (this.#stopped) return this.#status;
    const who = await this.#deps.whoami().catch(() => ({ ok: false, output: "", missing: true }));
    if (this.#stopped) return this.#status;
    if (who.missing) return this.#set({ state: "missing" });
    const facts = parseWhoami(who.output);
    if (!who.ok || !facts?.agent_id) {
      const detail = facts
        ? "This machine has no Firekeep agent identity yet. Run firekeep doctor."
        : /invalid choice/i.test(who.output)
          ? "Update the Firekeep kit (firekeep update) to connect Studio."
          // A non-zero exit: show the kit's own first error line (e.g. a broken venv), else the generic hint.
          : (!who.ok && firstLine(who.output)) || "The Firekeep kit config needs attention. Run firekeep doctor.";
      return this.#set({ state: "unconfigured", detail });
    }
    this.#label = `${facts.agent_id}${PANEL_AGENT_SUFFIX}`;
    this.#env = { FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: this.#label };
    const info = { agentId: facts.agent_id, personalMode: facts.personal_mode === true, kitVersion: String(facts.kit_version ?? "") };
    let lastError = "";
    for (;;) {
      if (this.#stopped) return this.#status;
      const previous = this.#mcp;
      this.#mcp = null;
      await previous?.close().catch(() => undefined);
      try {
        const client = await this.#connectOnce();
        if (this.#stopped) {
          await client.close().catch(() => undefined);
          return this.#status;
        }
        if (client.closed) throw new Error("Keep gateway exited");   // died before we could adopt it
        // Probe before adopting: an exit the probe causes reaches handleExit with a client that is not
        // current yet, so it is ignored and this loop's backoff handles it.
        try {
          await this.#probe(client);
        } catch (error) {
          await client.close().catch(() => undefined);
          throw error;
        }
        if (this.#stopped) {
          await client.close().catch(() => undefined);
          return this.#status;
        }
        this.#mcp = client;
        this.#connectedAt = this.#now();
        this.#callFailures = 0;
        return this.#set({ state: "connected", ...info });
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error);
        const delay = BACKOFF_MS[this.#failures];
        this.#failures += 1;
        if (delay === undefined) break;
        this.#set({ state: "unreachable", detail: firstLine(lastError), retryInMs: delay });
        await this.#sleep(delay);
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

function parseWhoami(output: string): { agent_id: string | null; kit_version?: unknown; personal_mode?: unknown } | null {
  // Last JSON line that carries an agent_id key; the id itself may be null (kit reports CHANGEME that way).
  for (const line of output.split(/\r?\n/).map((l) => l.trim()).reverse()) {
    if (!line.startsWith("{")) continue;
    try {
      const value = JSON.parse(line) as Record<string, unknown>;
      if (value && typeof value === "object" && "agent_id" in value) {
        const id = typeof value.agent_id === "string" && value.agent_id ? value.agent_id : null;
        return { ...value, agent_id: id };
      }
    } catch { /* not JSON */ }
  }
  return null;
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
